"""股票/商品盘行情上下文模块（日级 Up-or-Down 市场）。

与 llm_updown.py（5/15 分钟加密盘）的差异：
- 5m 盘：喂 1 分钟 K 线动量，LLM 判断短窗口方向
- 股票/商品盘：日级结算（收盘 vs 前日收盘），LLM 需要的是
  日 K 趋势结构、技术指标、大盘局势（SPY/VIX），而非分钟动量

数据源：stockanalysis.com 公开 API（免费、无 key、无反爬）
  - 股票/商品期货: GET /api/symbol/s/{SYM}/history
  - ETF/指数代理:  GET /api/symbol/e/{SYM}/history
  - 返回倒序日 K（最新在前），默认 125 根，?range=5Y 可拿 1255 根
  - 商品（XAU/XAG/WTI）在 Polymarket 上是现货价，本模块用 ETF
    代理（GLD/SLV/USO）作为价格代理——与现货高度相关，LLM 提示词
    中会注明是代理标的

输出：EquityContext（标的日 K 特征）+ MarketRegime（大盘局势），
可直接拼进 LLM prompt（build_equity_prompt）。
"""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field
from typing import Optional

from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import get_logger

log = get_logger("strategies.equity_context")

API_BASE = "https://stockanalysis.com/api/symbol"

# Polymarket updown slug 前缀 -> (类型, 数据源符号, 显示名)
# 类型: s=股票/期货, e=ETF（含代理）
# 商品用 ETF 代理：XAUUSD→GLD、XAGUSD→SLV、WTI→USO（标注代理）
# HSI（恒生）无直接源，用香港 ETF EWH 代理；UKX（富时100）用 EWU 代理
SYMBOL_MAP: dict[str, tuple[str, str, str]] = {
    "nvda":   ("s", "NVDA", "NVIDIA (NVDA)"),
    "tsla":   ("s", "TSLA", "Tesla (TSLA)"),
    "msft":   ("s", "MSFT", "Microsoft (MSFT)"),
    "aapl":   ("s", "AAPL", "Apple (AAPL)"),
    "amzn":   ("s", "AMZN", "Amazon (AMZN)"),
    "googl":  ("s", "GOOGL", "Alphabet (GOOGL)"),
    "meta":   ("s", "META", "Meta (META)"),
    "coin":   ("s", "COIN", "Coinbase (COIN)"),
    "pltr":   ("s", "PLTR", "Palantir (PLTR)"),
    "spy":    ("e", "SPY", "S&P 500 ETF (SPY)"),
    "qqq":    ("e", "QQQ", "Nasdaq 100 ETF (QQQ)"),
    "ndx":    ("e", "QQQ", "Nasdaq 100 (NDX→QQQ 代理)"),
    "xauusd": ("e", "GLD", "Gold (XAUUSD→GLD ETF 代理)"),
    "xagusd": ("e", "SLV", "Silver (XAGUSD→SLV ETF 代理)"),
    "wti":    ("e", "USO", "WTI Crude (WTI→USO ETF 代理)"),
    "hsi":    ("e", "EWH", "Hang Seng (HSI→EWH ETF 代理)"),
    "ukx":    ("e", "EWU", "FTSE 100 (UKX→EWU ETF 代理)"),
}

# 大盘局势代理：SPY（标普趋势）+ VXX（VIX 恐慌度）+ QQQ（科技风向）
REGIME_SYMBOLS: list[tuple[str, str, str]] = [
    ("e", "SPY", "S&P 500 (SPY)"),
    ("e", "QQQ", "Nasdaq 100 (QQQ)"),
    ("e", "VXX", "VIX 恐慌度 (VXX)"),
]


def resolve_symbol(slug: str) -> tuple[str, str, str] | None:
    """从 Polymarket slug（如 'nvda-up-or-down-on-...'）解析数据源。

    返回 (kind, symbol, display_name) 或 None。
    """
    prefix = slug.split("-")[0].lower()
    return SYMBOL_MAP.get(prefix)


def parse_daily_bars(raw: dict) -> list[dict]:
    """解析 stockanalysis /history 响应 → 正序日 K 列表。

    兼容两种结构：
      {"data": {"data": [...]}}   （默认 125 根）
      {"data": [...]}             （?range=5Y 时直接数组）
    每根 bar: {t: 日期, o/h/l/c: 价格, v: 量, ch: 涨跌%, a: 复权收盘}
    """
    data = raw.get("data")
    bars = data.get("data") if isinstance(data, dict) else data
    if not isinstance(bars, list) or not bars:
        return []
    # 接口返回倒序（最新在前），统一为正序（最旧在前）
    bars = [b for b in bars if isinstance(b, dict) and b.get("c") is not None]
    if bars and bars[0].get("t", "") > bars[-1].get("t", ""):
        bars = list(reversed(bars))
    return bars


@dataclass
class EquityContext:
    """标的的日 K 行情上下文（喂 LLM 用）。"""

    symbol: str
    display_name: str
    source_kind: str          # s / e
    is_proxy: bool            # True=ETF 代理（商品/指数）
    n_bars: int
    last_close: float
    prev_close: float
    last_change_pct: float    # 最新一日涨跌 %
    closes: list[float] = field(default_factory=list)
    ma5: Optional[float] = None
    ma20: Optional[float] = None
    ma60: Optional[float] = None
    slope5_pct: Optional[float] = None   # 近 5 日收盘线性斜率（% / 日）
    slope20_pct: Optional[float] = None  # 近 20 日收盘线性斜率（% / 日）
    rsi14: Optional[float] = None
    vol20_pct: Optional[float] = None    # 近 20 日收益年化波动率
    high20: Optional[float] = None
    low20: Optional[float] = None
    dist_high20_pct: Optional[float] = None  # 距 20 日高点回撤 %
    dist_low20_pct: Optional[float] = None   # 距 20 日低点涨幅 %
    vol_ratio: Optional[float] = None    # 近 5 日均量 / 近 20 日均量
    streak: int = 0                      # 连续上涨（正）/ 下跌（负）天数
    up_days_20: int = 0                  # 近 20 日上涨天数

    def to_prompt_block(self) -> str:
        """渲染为 LLM 可读的文本块。"""
        proxy_note = "（代理标的：跟踪实际商品/指数的 ETF）" if self.is_proxy else ""
        lines = [
            f"标的: {self.display_name}{proxy_note}",
            f"最新收盘: {self.last_close:.2f}（前收 {self.prev_close:.2f}，"
            f"当日 {self.last_change_pct:+.2f}%）",
            f"趋势: MA5={self._fmt(self.ma5)} MA20={self._fmt(self.ma20)} "
            f"MA60={self._fmt(self.ma60)}",
            f"斜率: 5日 {self._fmt_pct(self.slope5_pct)}/日，"
            f"20日 {self._fmt_pct(self.slope20_pct)}/日",
        ]
        if self.rsi14 is not None:
            state = "超买" if self.rsi14 > 70 else ("超卖" if self.rsi14 < 30 else "中性")
            lines.append(f"RSI(14): {self.rsi14:.0f}（{state}）")
        if self.vol20_pct is not None:
            lines.append(f"20日年化波动率: {self.vol20_pct:.1%}")
        lines.append(
            f"区间: 20日高 {self._fmt(self.high20)}（回撤 "
            f"{self._fmt_pct(self.dist_high20_pct)}），20日低 "
            f"{self._fmt(self.low20)}（涨幅 {self._fmt_pct(self.dist_low20_pct)}）"
        )
        if self.vol_ratio is not None:
            lines.append(f"量能: 5日/20日均量比 {self.vol_ratio:.2f}")
        lines.append(f"近20日: 涨 {self.up_days_20}/20 天，连续"
                     f"{'涨' if self.streak > 0 else '跌' if self.streak < 0 else '平'}"
                     f"{abs(self.streak)} 天")
        return "\n".join(lines)

    @staticmethod
    def _fmt(v: Optional[float]) -> str:
        return "—" if v is None else f"{v:.2f}"

    @staticmethod
    def _fmt_pct(v: Optional[float]) -> str:
        return "—" if v is None else f"{v:+.2%}"


@dataclass
class MarketRegime:
    """大盘局势（SPY/QQQ/VIX 的日 K 摘要）。"""

    components: list[EquityContext] = field(default_factory=list)

    def to_prompt_block(self) -> str:
        if not self.components:
            return "大盘局势: 不可用"
        lines = ["大盘局势:"]
        for c in self.components:
            trend = "多头" if (c.ma20 and c.ma5 and c.ma5 > c.ma20) else \
                    ("空头" if (c.ma20 and c.ma5 and c.ma5 < c.ma20) else "震荡")
            lines.append(
                f"  - {c.display_name}: 收 {c.last_close:.2f} "
                f"({c.last_change_pct:+.2f}%)，MA5>MA20={trend}，"
                f"20日波动率 {c.vol20_pct:.1%} 回撤 {c.dist_high20_pct:+.2%}" if c.vol20_pct else
                f"  - {c.display_name}: 收 {c.last_close:.2f} ({c.last_change_pct:+.2f}%)")
        return "\n".join(lines)


def _sma(values: list[float], n: int) -> Optional[float]:
    if len(values) < n:
        return None
    return sum(values[-n:]) / n


def _linear_slope_pct(values: list[float], n: int) -> Optional[float]:
    """近 n 个收盘价的线性斜率（% / 日）。"""
    if len(values) < n or values[-n] <= 0:
        return None
    xs = list(range(n))
    ys = values[-n:]
    xm, ym = sum(xs) / n, sum(ys) / n
    num = sum((x - xm) * (y - ym) for x, y in zip(xs, ys))
    den = sum((x - xm) ** 2 for x in xs)
    if den == 0:
        return None
    slope = num / den
    return slope / values[-n]


def _rsi(values: list[float], n: int = 14) -> Optional[float]:
    """Wilder RSI。"""
    if len(values) < n + 1:
        return None
    gains, losses = [], []
    for i in range(-n, 0):
        d = values[i] - values[i - 1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    ag = sum(gains) / n
    al = sum(losses) / n
    if al == 0:
        return 100.0
    rs = ag / al
    return 100.0 - 100.0 / (1.0 + rs)


def _annualized_vol(closes: list[float], n: int = 20) -> Optional[float]:
    if len(closes) < n + 1:
        return None
    rets = [(closes[i] - closes[i - 1]) / closes[i - 1]
            for i in range(-n, 0) if closes[i - 1] > 0]
    if not rets:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / len(rets)
    return math.sqrt(var) * math.sqrt(252)


def compute_features(bars: list[dict]) -> EquityContext | None:
    """从日 K 列表计算技术特征。bars 正序（最旧在前）。"""
    if len(bars) < 2:
        return None
    closes = [float(b["c"]) for b in bars]
    vols = [float(b.get("v") or 0) for b in bars]
    last_close = closes[-1]
    prev_close = closes[-2]
    ctx = EquityContext(
        symbol="", display_name="", source_kind="", is_proxy=False,
        n_bars=len(bars),
        last_close=last_close,
        prev_close=prev_close,
        last_change_pct=(last_close - prev_close) / prev_close * 100 if prev_close else 0.0,
        closes=closes,
        ma5=_sma(closes, 5),
        ma20=_sma(closes, 20),
        ma60=_sma(closes, 60),
        slope5_pct=_linear_slope_pct(closes, 5),
        slope20_pct=_linear_slope_pct(closes, 20),
        rsi14=_rsi(closes, 14),
        vol20_pct=_annualized_vol(closes, 20),
        high20=max(closes[-20:]) if len(closes) >= 20 else max(closes),
        low20=min(closes[-20:]) if len(closes) >= 20 else min(closes),
    )
    if ctx.high20:
        ctx.dist_high20_pct = (last_close - ctx.high20) / ctx.high20
    if ctx.low20:
        ctx.dist_low20_pct = (last_close - ctx.low20) / ctx.low20
    if len(vols) >= 25 and sum(vols[-25:-5]) > 0:
        ctx.vol_ratio = (sum(vols[-5:]) / 5) / (sum(vols[-25:-5]) / 20)
    # 连续涨跌天数
    streak = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] > closes[i - 1]:
            if streak >= 0:
                streak += 1
            else:
                break
        elif closes[i] < closes[i - 1]:
            if streak <= 0:
                streak -= 1
            else:
                break
        else:
            break
    ctx.streak = streak
    ctx.up_days_20 = sum(
        1 for i in range(max(1, len(closes) - 20), len(closes))
        if closes[i] > closes[i - 1])
    return ctx


class EquityContextFetcher:
    """拉取标的 + 大盘的日 K 并计算特征。"""

    def __init__(self, http: HttpClient | None = None, days: int = 250):
        self.http = http or HttpClient(proxy="http://127.0.0.1:7897", timeout=15)
        self.days = days

    def fetch_bars(self, kind: str, symbol: str) -> list[dict]:
        """拉取某标的历史日 K（正序）。"""
        url = f"{API_BASE}/{kind}/{symbol}/history"
        # stockanalysis 只支持 range=5Y（1255 根）或默认 125 根；days>125 时取 5Y
        params = {"range": "5Y"} if self.days > 125 else None
        try:
            raw = self.http.get_json(url, params=params)
        except Exception as e:
            log.warning("fetch bars failed %s %s: %s", kind, symbol, e)
            return []
        return parse_daily_bars(raw)

    def fetch_asset(self, slug: str) -> EquityContext | None:
        """按 Polymarket slug 拉取标的上下文。"""
        resolved = resolve_symbol(slug)
        if resolved is None:
            return None
        kind, symbol, display = resolved
        bars = self.fetch_bars(kind, symbol)
        ctx = compute_features(bars)
        if ctx is None:
            return None
        ctx.symbol = symbol
        ctx.display_name = display
        ctx.source_kind = kind
        ctx.is_proxy = kind == "e" and symbol in ("GLD", "SLV", "USO", "EWH", "EWU")
        return ctx

    def fetch_regime(self) -> MarketRegime:
        """大盘局势：SPY + QQQ + VXX 日 K 特征。"""
        comps: list[EquityContext] = []
        for kind, symbol, display in REGIME_SYMBOLS:
            bars = self.fetch_bars(kind, symbol)
            ctx = compute_features(bars)
            if ctx is None:
                continue
            ctx.symbol = symbol
            ctx.display_name = display
            ctx.source_kind = kind
            ctx.is_proxy = False
            comps.append(ctx)
        return MarketRegime(components=comps)


def build_equity_prompt(slug: str, ctx: EquityContext, regime: MarketRegime,
                        ref_yes: float, question: str = "",
                        end_date: str = "", secs_to_close: int | None = None) -> str:
    """构造股票/商品盘 LLM 判断 prompt（与 llm_updown 风格一致）。

    ref_yes: 市场隐含 P(涨)（Gamma outcomePrices YES 价，LLM 修正锚）。
    """
    head = [f"预测市场: {question or slug}"]
    if end_date:
        head.append(f"结算: {end_date} 收盘 vs 前一日收盘（日级）")
    if secs_to_close is not None:
        head.append(f"距结算: {secs_to_close // 3600}小时{(secs_to_close % 3600) // 60}分")
    head.append(f"市场隐含 P(涨): {ref_yes:.3f}（其他交易者的共识定价）")
    parts = ["\n".join(head), "", ctx.to_prompt_block(), "", regime.to_prompt_block(), "",
             "任务: 结合日 K 趋势结构、技术指标与大盘局势，给出标的今日收盘上涨的修正概率。",
             "约束（必须遵守）:",
             "1. 输出概率必须在 [0.02, 0.98] 区间内（单日方向不存在极端确定性）",
             f"2. 以市场隐含 {ref_yes:.3f} 为锚，你的修正幅度应在 ±0.25 以内",
             "3. 先给出理由再给概率，最后只输出 JSON: "
             '{"probability": 0-1, "reason": "一句话理由"}']
    return "\n".join(parts)


def slug_from_question(question: str) -> str:
    """从 question 提取 slug 前缀（如 'NVIDIA (NVDA) Up or Down...' → nvda）。"""
    m = re.search(r"\(([A-Z0-9]+)\)", question)
    if m:
        return m.group(1).lower()
    return ""
