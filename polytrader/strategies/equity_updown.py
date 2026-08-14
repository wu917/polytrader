"""股票/商品盘 LLM 策略：日级 Up-or-Down 方向判断。

与 llm_updown.py（5/15 分钟加密盘）的区别：
- 输入是日 K 技术特征 + 大盘局势（SPY/QQQ/VXX），不是分钟动量
- 结算为"当日收盘 vs 前一日收盘"（Pyth Close），日级等待
- 参考价 ref 来自 Gamma outcomePrices（市场隐含 P(涨)）
- 商品/指数用 ETF 代理（GLD/SLV/USO/EWH/EWU），prompt 已标注

流程：resolve slug → EquityContextFetcher 拉标的大盘 → 构建 prompt
→ LLM 输出 P(涨) → edge = |P - ref| 双侧取大 → 信号。
"""
from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import get_logger
from polytrader.models import Market, Side, Signal, SignalType
from polytrader.strategies.base import Strategy
from polytrader.strategies.equity_context import (
    EquityContext,
    EquityContextFetcher,
    MarketRegime,
    build_equity_prompt,
    resolve_symbol,
)

log = get_logger("strategies.equity_updown")


class EquityUpdownStrategy(Strategy):
    """日级股票/商品 updown 方向判断策略。"""

    name = "equity_updown"

    def __init__(
        self,
        scorer: LLMScorer,
        fetcher: EquityContextFetcher | None = None,
        min_edge: float = 0.05,
        min_price: float = 0.03,
        max_price: float = 0.97,
        max_markets: int = 10,
    ):
        self.scorer = scorer
        self.fetcher = fetcher or EquityContextFetcher()
        self.min_edge = min_edge
        self.min_price = min_price
        self.max_price = max_price
        self.max_markets = max_markets
        self.last_evaluations: list[dict] = []  # 本轮所有评估（含未下单原因）

    @property
    def enabled(self) -> bool:
        return self.scorer.enabled

    def score_updown(self, market: Market, regime: MarketRegime) -> dict | None:
        """单市场评估：日 K 上下文 + 大盘 + LLM 判断。返回 None=无法评估。"""
        if not market.is_binary or len(market.outcomes) < 2:
            return None
        yes, no = market.outcomes[0], market.outcomes[1]
        try:
            ref_yes = float(yes.price)
            ref_no = float(no.price)
        except (TypeError, ValueError):
            return None
        if not (self.min_price <= ref_yes <= self.max_price):
            return None
        if resolve_symbol(market.slug) is None:
            return None  # 非股票/商品盘（如加密 5m），跳过

        ctx = self.fetcher.fetch_asset(market.slug)
        if ctx is None:
            return None

        secs_to_close: int | None = None
        if "T" in market.end_date:
            import datetime as _dt
            try:
                end_ts = int(_dt.datetime.strptime(
                    market.end_date[:19], "%Y-%m-%dT%H:%M:%S")
                    .replace(tzinfo=_dt.timezone.utc).timestamp())
                secs_to_close = max(0, end_ts - int(time.time()))
                if secs_to_close < 1800:
                    # 距结算不足 30 分钟：日级盘尾段信息已定价，跳过
                    return None
            except (ValueError, OSError):
                secs_to_close = None

        prompt = build_equity_prompt(
            market.slug, ctx, regime, ref_yes,
            question=market.question, end_date=market.end_date[:10],
            secs_to_close=secs_to_close)
        p, reason = self.scorer.score_with_reason(
            f"{ctx.display_name} daily up or down", prompt, "equity")
        if p is None:
            return None
        yes_edge = p - ref_yes
        no_edge = (1.0 - p) - ref_no
        return {"llm_p": p, "ref_yes": ref_yes, "ref_no": ref_no,
                "yes_edge": yes_edge, "no_edge": no_edge,
                "reason": reason, "ctx": ctx, "regime": regime,
                "secs_to_close": secs_to_close}

    def _score_isolated(self, market: Market, regime: MarketRegime,
                        fetcher: EquityContextFetcher,
                        scorer: LLMScorer) -> dict | None:
        """线程隔离版单市场评估：用独立 fetcher/scorer（避免共享 Session）。

        与 score_updown 逻辑一致，但注入实例——并发 worker 用各自新建的
        fetcher + scorer，requests.Session 不跨线程共享。
        """
        if not market.is_binary or len(market.outcomes) < 2:
            return None
        yes, no = market.outcomes[0], market.outcomes[1]
        try:
            ref_yes = float(yes.price)
            ref_no = float(no.price)
        except (TypeError, ValueError):
            return None
        if not (self.min_price <= ref_yes <= self.max_price):
            return None
        if resolve_symbol(market.slug) is None:
            return None

        ctx = fetcher.fetch_asset(market.slug)
        if ctx is None:
            return None

        secs_to_close: int | None = None
        if "T" in market.end_date:
            import datetime as _dt
            try:
                end_ts = int(_dt.datetime.strptime(
                    market.end_date[:19], "%Y-%m-%dT%H:%M:%S")
                    .replace(tzinfo=_dt.timezone.utc).timestamp())
                secs_to_close = max(0, end_ts - int(time.time()))
                if secs_to_close < 1800:
                    return None
            except (ValueError, OSError):
                secs_to_close = None

        prompt = build_equity_prompt(
            market.slug, ctx, regime, ref_yes,
            question=market.question, end_date=market.end_date[:10],
            secs_to_close=secs_to_close)
        p, reason = scorer.score_with_reason(
            f"{ctx.display_name} daily up or down", prompt, "equity")
        if p is None:
            return None
        yes_edge = p - ref_yes
        no_edge = (1.0 - p) - ref_no
        return {"llm_p": p, "ref_yes": ref_yes, "ref_no": ref_no,
                "yes_edge": yes_edge, "no_edge": no_edge,
                "reason": reason, "ctx": ctx, "regime": regime,
                "secs_to_close": secs_to_close}

    def scan(self, markets: list[Market],
             books: dict | None = None,
             max_workers: int = 4) -> list[Signal]:
        """对股票/商品 updown 市场逐一评估（并发），edge ≥ 阈值出信号。

        大盘局势只拉一次共享；LLM 调用是 IO 瓶颈，用线程池并发。
        """
        if not self.enabled:
            log.warning("EquityUpdownStrategy disabled: no LLM_API_KEY")
            return []
        signals: list[Signal] = []
        self.last_evaluations = []
        regime = self.fetcher.fetch_regime()

        results: list[tuple[Market, dict | None]] = []
        if max_workers <= 1:
            # 串行：直接用注入的 fetcher/scorer（测试与单市场调试友好）
            for m in markets:
                results.append((m, self.score_updown(m, regime)))
        else:
            def _worker(m: Market) -> dict | None:
                # 线程隔离：每个 worker 独立 fetcher + scorer（独立 Session）
                f = EquityContextFetcher()
                s = LLMScorer(
                    api_key=self.scorer.api_key, base_url=self.scorer.base_url,
                    model=self.scorer.model,
                    http=HttpClient(timeout=90))  # 推理模型长 prompt 可达 60s+
                return self._score_isolated(m, regime, f, s)

            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futs = {ex.submit(_worker, m): m for m in markets}
                for fut in as_completed(futs):
                    m = futs[fut]
                    try:
                        results.append((m, fut.result()))
                    except Exception as e:
                        log.warning("score failed %s: %s", m.slug, e)
                        results.append((m, None))
        # 按原顺序输出，保持确定性
        results.sort(key=lambda t: t[0].slug)
        for market, r in results:
            if len(signals) >= self.max_markets:
                break
            if r is None:
                continue
            yes, no = market.outcomes[0], market.outcomes[1]
            best_edge = max(r["yes_edge"], r["no_edge"])
            self.last_evaluations.append({
                "slug": market.slug, "evaluated": True,
                "llm_p": round(r["llm_p"], 4), "ref_yes": round(r["ref_yes"], 4),
                "ref_no": round(r["ref_no"], 4),
                "yes_edge": round(r["yes_edge"], 4), "no_edge": round(r["no_edge"], 4),
                "best_edge": round(best_edge, 4),
                "signal": best_edge >= self.min_edge,
                "reason": r.get("reason"),
                "secs_to_close": r.get("secs_to_close"),
            })
            if r["yes_edge"] >= self.min_edge and r["yes_edge"] >= r["no_edge"]:
                signals.append(Signal(
                    type=SignalType.AI_PROBABILITY, market=market, outcome=yes,
                    side=Side.BUY, probability=r["llm_p"],
                    fair_price=r["llm_p"], edge=r["yes_edge"],
                    market_price=r["ref_yes"],
                    reason=f"equity_updown: p={r['llm_p']:.3f} yes_ref={r['ref_yes']:.3f} "
                           f"edge={r['yes_edge']:+.3f}",
                    extra={"llm_p": r["llm_p"], "side": "YES",
                           "model": self.scorer.model,
                           "llm_reason": r.get("reason"),
                           "ctx": _ctx_summary(r["ctx"]),
                           "regime": _regime_summary(r["regime"])},
                ))
            elif r["no_edge"] >= self.min_edge:
                signals.append(Signal(
                    type=SignalType.AI_PROBABILITY, market=market, outcome=no,
                    side=Side.BUY, probability=1.0 - r["llm_p"],
                    fair_price=1.0 - r["llm_p"], edge=r["no_edge"],
                    market_price=r["ref_no"],
                    reason=f"equity_updown: p_no={1.0 - r['llm_p']:.3f} "
                           f"no_ref={r['ref_no']:.3f} edge={r['no_edge']:+.3f}",
                    extra={"llm_p": r["llm_p"], "side": "NO",
                           "model": self.scorer.model,
                           "llm_reason": r.get("reason"),
                           "ctx": _ctx_summary(r["ctx"]),
                           "regime": _regime_summary(r["regime"])},
                ))
        return signals


def _ctx_summary(ctx: EquityContext) -> dict:
    return {
        "symbol": ctx.symbol, "display": ctx.display_name,
        "close": ctx.last_close, "chg_pct": ctx.last_change_pct,
        "ma5": ctx.ma5, "ma20": ctx.ma20, "ma60": ctx.ma60,
        "rsi14": ctx.rsi14, "vol20_pct": ctx.vol20_pct,
        "dist_high20_pct": ctx.dist_high20_pct,
        "dist_low20_pct": ctx.dist_low20_pct,
        "streak": ctx.streak,
    }


def _regime_summary(regime: MarketRegime) -> list[dict]:
    return [{"symbol": c.symbol, "close": c.last_close,
             "chg_pct": c.last_change_pct} for c in regime.components]
