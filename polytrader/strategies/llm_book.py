"""LLM 盘口策略：用 LLM 评估盘口概率进行下单。

对每个活跃二元市场，把订单簿上下文（bid/ask/spread/depth/流动性）与
市场问题/描述一起交给 LLM，让其估计 P(YES)；edge = p_llm - ask 价 ≥
min_edge 且流动性达标 → BUY 信号。

纯 LLM 策略（不依赖训练模型）；与 AIProbabilityStrategy 的区别是
盘口信息进入 prompt，且无模型融合。
"""
from __future__ import annotations

import logging

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.logging_setup import get_logger
from polytrader.models import Market, OrderBook, Side, Signal, SignalType
from polytrader.strategies.base import Strategy

log = get_logger("strategies.llm_book")


def build_book_prompt(market: Market, book: OrderBook) -> str:
    """构造带盘口上下文的 LLM 输入。"""
    bid = book.best_bid()
    ask = book.best_ask()
    mid = book.mid_price()
    spread = (ask.price - bid.price) / mid if (bid and ask and mid) else None
    lines = [
        f"Market: {market.question}",
        f"Category: {market.category}",
        f"Resolution: {market.end_date}",
        f"Description: {(market.description or '')[:1200]}",
        "--- Order book (current) ---",
        f"YES best bid: {bid.price if bid else 'n/a'} (size {bid.size if bid else 0:,.0f})",
        f"YES best ask: {ask.price if ask else 'n/a'} (size {ask.size if ask else 0:,.0f})",
        f"spread: {spread:.4f}" if spread is not None else "spread: n/a",
        f"depth(3): ${book.depth_usd(3):,.0f}",
        f"market liquidity: ${market.liquidity:,.0f}",
    ]
    return "\n".join(lines)


class LLMBookStrategy(Strategy):
    name = "llm_book"

    def __init__(
        self,
        scorer: LLMScorer,
        min_edge: float = 0.05,
        min_liquidity_usd: float = 500.0,
        min_price: float = 0.03,
        max_price: float = 0.97,
        max_markets: int = 20,
    ):
        self.scorer = scorer
        self.min_edge = min_edge
        self.min_liquidity_usd = min_liquidity_usd
        self.min_price = min_price
        self.max_price = max_price
        self.max_markets = max_markets

    @property
    def enabled(self) -> bool:
        return self.scorer.enabled

    def scan(self, markets: list[Market],
             books: dict[str, OrderBook] | None = None) -> list[Signal]:
        if not self.enabled:
            log.warning("LLMBookStrategy disabled: no LLM_API_KEY")
            return []
        books = books or {}
        signals: list[Signal] = []
        scanned = 0
        for market in markets:
            if scanned >= self.max_markets:
                break
            if market.closed or not market.active or not market.is_binary:
                continue
            if market.liquidity < self.min_liquidity_usd:
                continue
            yes = market.outcomes[0]
            no = market.outcomes[1]
            book_yes = books.get(yes.token_id)
            if book_yes is None or book_yes.best_ask() is None:
                continue
            # 参考价用 Gamma outcomePrices（市场共识价）：盘口在无合理
            # 流动性时只有 0.001/0.999 极端挂单，book mid=0.5 是垃圾值
            ref_yes = _outcome_price(yes) or (book_yes.mid_price() or book_yes.best_ask().price)
            ref_no = _outcome_price(no) or 1.0 - ref_yes
            if not (self.min_price <= ref_yes <= self.max_price):
                continue

            scanned += 1
            prompt = build_book_prompt(market, book_yes)
            p = self.scorer.score(market.question, prompt, market.category)
            if p is None:
                log.warning("LLM score failed for %s", market.slug)
                continue
            # 双侧评估：LLM 概率 vs 两侧参考价，取正 edge 侧
            yes_edge = p - ref_yes
            no_edge = (1.0 - p) - ref_no
            signal = None
            if yes_edge >= self.min_edge and yes_edge >= no_edge:
                signal = Signal(
                    type=SignalType.AI_PROBABILITY,
                    market=market, outcome=yes,
                    side=Side.BUY, probability=p,
                    fair_price=p, edge=yes_edge, market_price=ref_yes,
                    reason=f"llm_book: p={p:.3f} yes_ref={ref_yes:.3f} edge={yes_edge:+.3f}",
                    extra={"llm_p": p, "side": "YES", "model": self.scorer.model},
                )
            elif no_edge >= self.min_edge:
                signal = Signal(
                    type=SignalType.AI_PROBABILITY,
                    market=market, outcome=no,
                    side=Side.BUY, probability=1.0 - p,
                    fair_price=1.0 - p, edge=no_edge, market_price=ref_no,
                    reason=f"llm_book: p_no={1.0 - p:.3f} no_ref={ref_no:.3f} edge={no_edge:+.3f}",
                    extra={"llm_p": p, "side": "NO", "model": self.scorer.model},
                )
            if signal is not None:
                signals.append(signal)
        return signals


def _outcome_price(outcome) -> float | None:
    """Gamma outcome price 字符串 → float；失败返回 None。"""
    try:
        return float(outcome.price)
    except (TypeError, ValueError):
        return None
