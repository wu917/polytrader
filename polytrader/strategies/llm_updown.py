"""LLM updown 策略：LLM 判断 5/15 分钟窗口方向 + 盘口定价对比。

与 LLMBookStrategy 的区别：
- 输入实时行情上下文（Binance 价格/动量/波动率）——LLM 没有实时感知，
  必须喂数据才能判断短期方向
- 一次调用问 P(窗口内上涨)，双侧 edge = |P - ref| 取大
- 专为 updown 滚动窗口市场设计（ref_yes 来自 Gamma outcomePrices）

性能（2026-08-15 优化）：
- scan 并发评估（ThreadPoolExecutor，max_workers 可配），多市场不再串行等 LLM
- 窗口内评估缓存（TTL 45s）：同一窗口 30s 间隔扫描复用上轮 LLM 结果，
  大幅降低 LLM 调用量与评估耗时
"""
from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import get_logger
from polytrader.models import Market, Side, Signal, SignalType
from polytrader.strategies.llm_book import LLMBookStrategy, _outcome_price

log = get_logger("strategies.llm_updown")

HTTP = HttpClient(proxy="http://127.0.0.1:7897", timeout=10)


def fetch_market_context(coin: str, window_s: int = 300) -> dict | None:
    """实时行情上下文：当前价 + 近 6 根 1m 走势 + 波动率。

    Binance 无 HYPEUSDT → fallback OKX（HYPE-USDT）。
    """
    symbol = f"{coin.upper()}USDT"
    k = None
    ticker = None
    try:
        k = HTTP.get_json("https://api.binance.com/api/v3/klines",
                          params={"symbol": symbol, "interval": "1m", "limit": 6})
        ticker = HTTP.get_json("https://api.binance.com/api/v3/ticker/price",
                               params={"symbol": symbol})
    except Exception as e:
        log.info("binance context failed for %s (%s) — trying okx", coin, e)
    if k is None or not k:
        try:
            ok = HTTP.get_json(
                f"https://www.okx.com/api/v5/market/candles?instId={symbol}&bar=1m&limit=6")
            if ok and ok.get("code") == "0":
                rows = sorted(ok["data"], key=lambda r: r[0])  # 时间升序
                k = [[r[0], r[1], r[2], r[3], r[4]] for r in rows]
                t = HTTP.get_json(
                    f"https://www.okx.com/api/v5/market/ticker?instId={symbol}")
                if t and t.get("code") == "0":
                    ticker = {"price": t["data"][0]["last"]}
        except Exception as e2:
            log.warning("okx context failed for %s: %s", coin, e2)
    if not k or ticker is None:
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
    """构造短期方向预测 prompt（真实行情数据 + 窗口信息 + 概率锚定）。"""
    return (
        f"预测市场: {coin.upper()} 在未来 {secs_left} 秒内（窗口结算）是否上涨\n"
        f"窗口长度: {ctx['window_s']}s，剩余 {secs_left}s（已运行 {ctx['window_s'] - secs_left}s）\n"
        f"市场隐含 P(涨): {ref_yes:.3f}（这是其他交易者的共识定价）\n"
        f"实时行情 ({ctx['symbol']}):\n"
        f"  当前价 {ctx['price']:.2f}\n"
        f"  近5分钟涨跌: {ctx['last5_chg']:+.2%}（分钟涨跌幅 {ctx['per_min']}，"
        f"涨{ctx['up_minutes']}根/跌{ctx['down_minutes']}根）\n"
        f"  5分钟振幅: {ctx['range5_pct']:.2%}\n"
        f"任务：结合行情动量给出你对结算时上涨的修正概率。\n"
        f"约束（必须遵守）：\n"
        f"1. 输出概率必须在 [0.02, 0.98] 区间内（极端确定性在短窗口不存在）\n"
        f"2. 以市场隐含 {ref_yes:.3f} 为锚，你的修正幅度应在 ±0.25 以内\n"
        f"3. 先给出理由再给概率，最后只输出 JSON: {{\"probability\": 0-1, \"reason\": \"一句话理由\"}}"
    )


class LLMUpdownStrategy(LLMBookStrategy):
    """updown 市场 LLM 方向判断策略（继承 LLMBookStrategy 的评分/信号骨架）。"""

    name = "llm_updown"

    def __init__(self, scorer: LLMScorer, min_edge: float = 0.05,
                 min_price: float = 0.03, max_price: float = 0.97,
                 max_markets: int = 10, coin_map: dict | None = None,
                 max_workers: int = 6, cache_ttl: float = 45.0):
        super().__init__(scorer, min_edge=min_edge, min_liquidity_usd=0.0,
                         min_price=min_price, max_price=max_price,
                         max_markets=max_markets)
        self.coin_map = coin_map or {}
        self.last_evaluations: list[dict] = []  # 本轮所有评估（含未下单原因）
        self.max_workers = max_workers  # 并发评估线程数
        self.cache_ttl = cache_ttl  # 窗口内评估缓存 TTL（秒）
        self._cache: dict[str, tuple[dict, float]] = {}  # slug -> (result, ts)

    def _score_cached(self, market: Market, coin: str, window_s: int) -> dict | None:
        """带缓存的单市场评估：TTL 内复用上轮 LLM 结果（省 LLM 调用）。"""
        cached = self._cache.get(market.slug)
        if cached and time.time() - cached[1] < self.cache_ttl:
            return cached[0]
        r = self.score_updown(market, coin, window_s)
        if r is not None:
            self._cache[market.slug] = (r, time.time())
        return r

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
        end_ts = 0
        if "T" in market.end_date:
            import datetime as _dt
            try:
                end_ts = int(_dt.datetime.strptime(
                    market.end_date[:19], "%Y-%m-%dT%H:%M:%S")
                    .replace(tzinfo=_dt.timezone.utc).timestamp())  # endDate 为 UTC
            except (ValueError, OSError):
                end_ts = 0
        secs_left = max(0, end_ts - int(time.time())) if end_ts else 0
        if secs_left < 20:
            log.warning("skip %s: window ended or too close (secs_left=%d)",
                        market.slug, secs_left)
            return None  # 窗口已结束/过近，不评估
        prompt = build_updown_prompt(coin, ctx, market, ref_yes, secs_left)
        p, reason = self.scorer.score_with_reason(
            f"{coin.upper()} {window_s // 60}min up or down", prompt, "crypto")
        if p is None:
            return None
        yes_edge = p - ref_yes
        no_edge = (1.0 - p) - ref_no
        return {"llm_p": p, "ref_yes": ref_yes, "ref_no": ref_no,
                "yes_edge": yes_edge, "no_edge": no_edge,
                "reason": reason, "ctx": ctx, "secs_left": secs_left}

    def scan(self, markets: list[Market],
             books: dict | None = None) -> list[Signal]:
        """markets 需携带 slug（btc-updown-5m-<ts>）与 coin_map 映射。

        并发评估（ThreadPoolExecutor）：每个市场独立网络请求（行情+LLM），
        并发可把 N 个市场的评估耗时从 N×单次压到接近单次；信号按 markets
        原序输出，保证可复现。
        """
        if not self.enabled:
            log.warning("LLMUpdownStrategy disabled: no LLM_API_KEY")
            return []
        tasks: list[tuple[Market, str, int]] = []
        for market in markets:
            coin = self.coin_map.get(market.slug.split("-")[0])
            if not coin:
                continue
            window_s = 300 if "-5m-" in market.slug else 900
            tasks.append((market, coin, window_s))
        # 与旧串行语义一致：评估量不超过 max_markets（并发不放大 LLM 调用量）
        tasks = tasks[: self.max_markets]

        results: dict[str, dict | None] = {}
        if tasks:
            workers = max(1, min(self.max_workers, len(tasks)))
            with ThreadPoolExecutor(max_workers=workers) as ex:
                futures = {
                    ex.submit(self._score_cached, m, c, w): m.slug
                    for m, c, w in tasks}
                for f in as_completed(futures):
                    slug = futures[f]
                    try:
                        results[slug] = f.result()
                    except Exception as exc:  # noqa: BLE001 单市场失败不影响整体
                        log.warning("concurrent eval failed for %s: %s", slug, exc)
                        results[slug] = None

        signals: list[Signal] = []
        self.last_evaluations = []
        for market in markets:
            if len(signals) >= self.max_markets:
                break
            coin = self.coin_map.get(market.slug.split("-")[0])
            if not coin:
                continue
            window_s = 300 if "-5m-" in market.slug else 900
            r = results.get(market.slug)
            if r is None:
                self.last_evaluations.append({"slug": market.slug, "evaluated": False,
                                              "skip_reason": "llm/context failed"})
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
                "ctx5m": round(r["ctx"]["last5_chg"], 5),
            })
            if r["yes_edge"] >= self.min_edge and r["yes_edge"] >= r["no_edge"]:
                signals.append(Signal(
                    type=SignalType.AI_PROBABILITY, market=market, outcome=yes,
                    side=Side.BUY, probability=r["llm_p"],
                    fair_price=r["llm_p"], edge=r["yes_edge"],
                    market_price=r["ref_yes"],
                    reason=f"llm_updown: p={r['llm_p']:.3f} yes_ref={r['ref_yes']:.3f} "
                           f"edge={r['yes_edge']:+.3f} ctx5m={r['ctx']['last5_chg']:+.2%}",
                    extra={"llm_p": r["llm_p"], "side": "YES",
                           "model": self.scorer.model, "ctx": r["ctx"],
                           "llm_reason": r.get("reason")},
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
                           "model": self.scorer.model, "ctx": r["ctx"],
                           "llm_reason": r.get("reason")},
                ))
        return signals
