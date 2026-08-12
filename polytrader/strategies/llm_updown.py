"""LLM updown 策略：LLM 判断 5/15 分钟窗口方向 + 盘口定价对比。

与 LLMBookStrategy 的区别：
- 输入实时行情上下文（Binance 价格/动量/波动率）——LLM 没有实时感知，
  必须喂数据才能判断短期方向
- 一次调用问 P(窗口内上涨)，双侧 edge = |P - ref| 取大
- 专为 updown 滚动窗口市场设计（ref_yes 来自 Gamma outcomePrices）
"""
from __future__ import annotations

import json
import logging
import time

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import get_logger
from polytrader.models import Market, Side, Signal, SignalType
from polytrader.strategies.llm_book import LLMBookStrategy, _outcome_price

log = get_logger("strategies.llm_updown")

HTTP = HttpClient(proxy="socks5h://127.0.0.1:7890", timeout=10)


def fetch_market_context(coin: str, window_s: int = 300) -> dict | None:
    """Binance 实时行情上下文：当前价 + 近 6 根 1m 走势 + 波动率。"""
    symbol = f"{coin.upper()}USDT"
    try:
        k = HTTP.get_json("https://api.binance.com/api/v3/klines",
                          params={"symbol": symbol, "interval": "1m", "limit": 6})
        ticker = HTTP.get_json("https://api.binance.com/api/v3/ticker/price",
                               params={"symbol": symbol})
    except Exception as e:
        log.warning("binance context failed for %s: %s", coin, e)
        return None
    if not k:
        return None
    closes = [float(b[4]) for b in k]
    opens = [float(b[1]) for b in k]
    cur = float(ticker["price"])
    changes = [(c - o) / o for o, c in zip(opens, closes)]
    vol = (max(closes) - min(closes)) / closes[0] if closes else 0.0
    last5 = (cur - closes[0]) / closes[0] if closes else 0.0
    return {
        "symbol": symbol, "price": cur,
        "last5_chg": last5,
        "per_min": [round(c, 5) for c in changes],
        "range5_pct": round(vol, 5),
        "up_minutes": sum(1 for c in changes if c > 0),
        "down_minutes": sum(1 for c in changes if c < 0),
        "window_s": window_s,
    }


def build_updown_prompt(coin: str, ctx: dict, market: Market, ref_yes: float,
                        secs_left: int) -> str:
    """构造短期方向预测 prompt（真实行情数据 + 窗口信息）。"""
    return (
        f"预测市场: {coin.upper()} 在未来 {secs_left} 秒内（窗口结算）是否上涨\n"
        f"窗口长度: {ctx['window_s']}s，剩余 {secs_left}s（已运行 {ctx['window_s'] - secs_left}s）\n"
        f"市场隐含 P(涨): {ref_yes:.3f}\n"
        f"实时行情 ({ctx['symbol']}):\n"
        f"  当前价 {ctx['price']:.2f}\n"
        f"  近5分钟涨跌: {ctx['last5_chg']:+.2%}（分钟涨跌幅 {ctx['per_min']}，"
        f"涨{ctx['up_minutes']}根/跌{ctx['down_minutes']}根）\n"
        f"  5分钟振幅: {ctx['range5_pct']:.2%}\n"
        f"市场状态: 问题={market.question} 结算={market.end_date}\n"
        f"请判断窗口结算时上涨的概率（结合行情动量/振幅，可高于或低于市场隐含）。"
        f"只输出 JSON: {{\"probability\": 0-1, \"reason\": \"一句话\"}}"
    )


class LLMUpdownStrategy(LLMBookStrategy):
    """updown 市场 LLM 方向判断策略（继承 LLMBookStrategy 的评分/信号骨架）。"""

    name = "llm_updown"

    def __init__(self, scorer: LLMScorer, min_edge: float = 0.05,
                 min_price: float = 0.03, max_price: float = 0.97,
                 max_markets: int = 10, coin_map: dict | None = None):
        super().__init__(scorer, min_edge=min_edge, min_liquidity_usd=0.0,
                         min_price=min_price, max_price=max_price,
                         max_markets=max_markets)
        self.coin_map = coin_map or {}

    def score_updown(self, market: Market, coin: str, window_s: int) -> dict | None:
        """单市场：行情上下文 + LLM 判断 + 双侧 edge。返回 None 表示无法评估。"""
        yes = market.outcomes[0]
        no = market.outcomes[1]
        ref_yes = _outcome_price(yes)
        ref_no = _outcome_price(no)
        if ref_yes is None or ref_no is None:
            return None
        if not (self.min_price <= ref_yes <= self.max_price):
            return None
        ctx = fetch_market_context(coin, window_s)
        if ctx is None:
            return None
        end_ts = int(time.mktime(time.strptime(market.end_date[:19],
                                               "%Y-%m-%dT%H:%M:%S"))) if "T" in market.end_date else 0
        secs_left = max(0, end_ts - int(time.time())) if end_ts else 0
        prompt = build_updown_prompt(coin, ctx, market, ref_yes, secs_left)
        p = self.scorer.score(f"{coin.upper()} {window_s // 60}min up or down",
                              prompt, "crypto")
        if p is None:
            return None
        yes_edge = p - ref_yes
        no_edge = (1.0 - p) - ref_no
        return {"llm_p": p, "ref_yes": ref_yes, "ref_no": ref_no,
                "yes_edge": yes_edge, "no_edge": no_edge,
                "ctx": ctx, "secs_left": secs_left}

    def scan(self, markets: list[Market],
             books: dict | None = None) -> list[Signal]:
        """markets 需携带 slug（btc-updown-5m-<ts>）与 coin_map 映射。"""
        if not self.enabled:
            log.warning("LLMUpdownStrategy disabled: no LLM_API_KEY")
            return []
        signals: list[Signal] = []
        for market in markets:
            if len(signals) >= self.max_markets:
                break
            coin = self.coin_map.get(market.slug.split("-")[0])
            if not coin:
                continue
            window_s = 300 if "-5m-" in market.slug else 900
            r = self.score_updown(market, coin, window_s)
            if r is None:
                continue
            yes, no = market.outcomes[0], market.outcomes[1]
            if r["yes_edge"] >= self.min_edge and r["yes_edge"] >= r["no_edge"]:
                signals.append(Signal(
                    type=SignalType.AI_PROBABILITY, market=market, outcome=yes,
                    side=Side.BUY, probability=r["llm_p"],
                    fair_price=r["llm_p"], edge=r["yes_edge"],
                    market_price=r["ref_yes"],
                    reason=f"llm_updown: p={r['llm_p']:.3f} yes_ref={r['ref_yes']:.3f} "
                           f"edge={r['yes_edge']:+.3f} ctx5m={r['ctx']['last5_chg']:+.2%}",
                    extra={"llm_p": r["llm_p"], "side": "YES",
                           "model": self.scorer.model, "ctx": r["ctx"]},
                ))
            elif r["no_edge"] >= self.min_edge:
                signals.append(Signal(
                    type=SignalType.AI_PROBABILITY, market=market, outcome=no,
                    side=Side.BUY, probability=1.0 - r["llm_p"],
                    fair_price=1.0 - r["llm_p"], edge=r["no_edge"],
                    market_price=r["ref_no"],
                    reason=f"llm_updown: p_no={1.0 - r['llm_p']:.3f} no_ref={r['ref_no']:.3f} "
                           f"edge={r['no_edge']:+.3f} ctx5m={r['ctx']['last5_chg']:+.2%}",
                    extra={"llm_p": r["llm_p"], "side": "NO",
                           "model": self.scorer.model, "ctx": r["ctx"]},
                ))
        return signals
