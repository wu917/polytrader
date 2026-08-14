"""通用事件盘 LLM 评估策略：任意二元市场（选举/宏观/地缘/商业等）。

复用 LLMBookStrategy 的评估骨架（question+description+盘口 → LLM P(YES)
→ 双侧 edge），在此基础上增加：
- 收益比 RR = (1 - P_buy) / P_buy（二元盘任意一侧：赢赚 1-p，输亏 p）
- 期望值 EV = P_llm_win × (1 - P_buy) - (1 - P_llm_win) × P_buy
- 开单条件可配置：edge 阈值 + RR 下限 + EV > 0

与 equity_updown 的区别：不绑定股票/商品，不拉行情上下文，
纯靠市场 question/description/结算规则 + LLM 世界知识判断事件概率。
"""
from __future__ import annotations

import logging

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.logging_setup import get_logger
from polytrader.models import Market, OrderBook, Signal
from polytrader.strategies.llm_book import LLMBookStrategy

log = get_logger("strategies.event_market")


def calc_rr_ev(win_prob: float, buy_price: float) -> tuple[float, float]:
    """二元盘收益比与期望值。

    win_prob: LLM 判断的该侧胜率 P_llm
    buy_price: 买入价（成交价）
    返回 (RR, EV)：RR = (1-p)/p；EV = P×(1-p) - (1-P)×p
    """
    if buy_price <= 0 or buy_price >= 1:
        return 0.0, 0.0
    rr = (1.0 - buy_price) / buy_price
    ev = win_prob * (1.0 - buy_price) - (1.0 - win_prob) * buy_price
    return round(rr, 3), round(ev, 4)


class EventMarketStrategy(LLMBookStrategy):
    """通用事件盘策略：LLM 评估 + RR/EV 过滤。

    继承 LLMBookStrategy：scan() 复用父类评估流程（min_price/max_price、
    双侧 edge），额外参数：
    - min_rr: 收益比下限（默认 1.5，即买入价 ≤0.40 或 ≥0.60 侧）
    - require_ev: EV > 0 才出信号（默认 True）
    """

    name = "event_market"

    def __init__(
        self,
        scorer: LLMScorer,
        min_edge: float = 0.05,
        min_liquidity_usd: float = 0.0,
        min_price: float = 0.05,
        max_price: float = 0.95,
        max_markets: int = 50,
        min_rr: float = 1.5,
        require_ev: bool = True,
        max_edge: float = 0.30,
    ):
        super().__init__(scorer, min_edge=min_edge,
                         min_liquidity_usd=min_liquidity_usd,
                         min_price=min_price, max_price=max_price,
                         max_markets=max_markets)
        self.min_rr = min_rr
        self.require_ev = require_ev
        self.max_edge = max_edge

    def scan(self, markets: list[Market],
             books: dict[str, OrderBook] | None = None) -> list[Signal]:
        """复用父类 scan 后为每个信号附加 RR/EV，并做 RR/EV 过滤。"""
        signals = super().scan(markets, books)
        kept: list[Signal] = []
        for s in signals:
            if abs(s.edge) > self.max_edge:
                # LLM 与市场共识偏差过大（>30%）→ 大概率 LLM 幻觉/信息过时，
                # 而非真实错误定价，跳过避免重仓误判
                log.info("skip %s: edge %.3f 超 max_edge %.2f（疑似幻觉）",
                         s.market.slug, s.edge, self.max_edge)
                continue
            p = float(s.extra.get("llm_p", s.probability))
            side = s.extra.get("side", "YES")
            # 买入价 = 信号侧市场价（ref）；胜率 = LLM 该侧概率
            buy_price = float(s.market_price)
            win_prob = p if side == "YES" else 1.0 - p
            rr, ev = calc_rr_ev(win_prob, buy_price)
            s.extra["rr"] = rr
            s.extra["ev"] = ev
            s.extra["buy_price"] = round(buy_price, 4)
            if rr < self.min_rr:
                log.info("skip %s: RR %.2f < %.2f", s.market.slug, rr, self.min_rr)
                continue
            if self.require_ev and ev <= 0:
                log.info("skip %s: EV %.4f <= 0", s.market.slug, ev)
                continue
            s.reason = (f"{s.reason} rr={rr:.2f} ev={ev:+.4f}")
            kept.append(s)
        return kept
