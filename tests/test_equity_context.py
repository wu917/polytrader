"""equity_context 模块测试：特征计算 + slug 解析 + prompt 构建。

特征计算用内联 mock 日 K（不依赖网络）；数据拉取用 monkeypatch。
"""
from __future__ import annotations

import pytest

import polytrader.strategies.equity_context as ec


def _make_bars(closes: list[float], vols: list[float] | None = None,
               start: str = "2026-01-05") -> list[dict]:
    """构造正序日 K（t 递增，日期不重要）。"""
    import datetime as dt
    d = dt.date.fromisoformat(start)
    vols = vols or [1_000_000.0] * len(closes)
    bars = []
    for i, c in enumerate(closes):
        bars.append({
            "t": (d + dt.timedelta(days=i)).isoformat(),
            "o": c, "h": c * 1.01, "l": c * 0.99, "c": c,
            "v": vols[i], "ch": 0.0,
        })
    return bars


def test_parse_daily_bars_both_shapes():
    """兼容两种响应结构，且倒序自动转正序。"""
    bars = _make_bars([100.0, 101.0, 102.0])
    # 默认结构: data.data
    raw1 = {"status": 200, "data": {"data": list(reversed(bars))}}
    out = ec.parse_daily_bars(raw1)
    assert [b["c"] for b in out] == [100.0, 101.0, 102.0]
    # range 结构: data 直接是数组
    raw2 = {"status": 200, "data": list(reversed(bars))}
    out2 = ec.parse_daily_bars(raw2)
    assert [b["c"] for b in out2] == [100.0, 101.0, 102.0]
    # 空/坏数据
    assert ec.parse_daily_bars({"data": {}}) == []
    assert ec.parse_daily_bars({"data": []}) == []


def test_compute_features_basic():
    """上涨序列：MA、斜率、RSI、回撤、连续天数。"""
    closes = [100.0 + i * 1.0 for i in range(70)]  # 单调上涨
    bars = _make_bars(closes)
    ctx = ec.compute_features(bars)
    assert ctx is not None
    assert ctx.n_bars == 70
    assert ctx.last_close == 169.0
    assert ctx.prev_close == 168.0
    assert ctx.last_change_pct == pytest.approx(100.0 / 168.0, rel=1e-6)
    # 单调上涨: MA5 > MA20 > MA60
    assert ctx.ma5 > ctx.ma20 > ctx.ma60
    assert ctx.slope5_pct is not None and ctx.slope5_pct > 0
    assert ctx.slope20_pct is not None and ctx.slope20_pct > 0
    assert ctx.rsi14 is not None and ctx.rsi14 > 70  # 超买
    assert ctx.dist_high20_pct is not None and ctx.dist_high20_pct <= 0  # 在最高点附近
    assert ctx.streak == 69  # 连续上涨
    assert ctx.up_days_20 == 20


def test_compute_features_downtrend():
    """下跌序列：RSI 超卖、连续下跌、MA 空头排列。"""
    closes = [200.0 - i * 1.0 for i in range(70)]
    bars = _make_bars(closes)
    ctx = ec.compute_features(bars)
    assert ctx is not None
    assert ctx.ma5 < ctx.ma20 < ctx.ma60
    assert ctx.rsi14 is not None and ctx.rsi14 < 30
    assert ctx.streak == -69
    assert ctx.dist_low20_pct is not None and ctx.dist_low20_pct >= 0


def test_compute_features_insufficient():
    assert ec.compute_features([]) is None
    assert ec.compute_features(_make_bars([100.0])) is None


def test_vol_ratio():
    """放量: 近 5 日均量是前 20 日的 2 倍。"""
    closes = [100.0] * 30
    vols = [1_000_000.0] * 25 + [2_000_000.0] * 5
    ctx = ec.compute_features(_make_bars(closes, vols))
    assert ctx is not None
    assert ctx.vol_ratio == pytest.approx(2.0, rel=1e-6)


def test_resolve_symbol():
    assert ec.resolve_symbol("nvda-up-or-down-on-august-14-2026") == ("s", "NVDA", "NVIDIA (NVDA)")
    assert ec.resolve_symbol("spy-up-or-down-on-august-14-2026")[0:2] == ("e", "SPY")
    assert ec.resolve_symbol("xauusd-up-or-down-on-august-14-2026")[0:2] == ("e", "GLD")
    assert ec.resolve_symbol("wti-up-or-down-on-august-14-2026")[0:2] == ("e", "USO")
    assert ec.resolve_symbol("unknown-market-xyz") is None


def test_slug_from_question():
    assert ec.slug_from_question("NVIDIA (NVDA) Up or Down on August 13?") == "nvda"
    assert ec.slug_from_question("WTI Crude Oil (WTI) Up or Down") == "wti"
    assert ec.slug_from_question("no ticker here") == ""


def test_fetch_bars_monkeypatched(monkeypatch):
    """mock HTTP：确认 URL 与 days 参数拼接正确。"""
    captured = {}

    class FakeHttp:
        def get_json(self, url, params=None, **kw):
            captured["url"] = url
            captured["params"] = params
            bars = _make_bars([100.0 + i for i in range(30)])
            return {"status": 200, "data": {"data": list(reversed(bars))}}

    f = ec.EquityContextFetcher(http=FakeHttp(), days=60)
    bars = f.fetch_bars("s", "NVDA")
    assert len(bars) == 30
    assert captured["url"].endswith("/s/NVDA/history")
    assert captured["params"] is None  # days <= 125 用默认 125 根
    # days > 125 时请求 5 年
    f2 = ec.EquityContextFetcher(http=FakeHttp(), days=300)
    f2.fetch_bars("s", "NVDA")
    assert captured["params"] == {"range": "5Y"}


def test_fetch_asset_and_regime(monkeypatch):
    """fetch_asset 用 slug 映射 + fetch_regime 组合。"""
    bars = _make_bars([100.0 + i for i in range(70)])

    class FakeHttp:
        def get_json(self, url, params=None, **kw):
            return {"status": 200, "data": {"data": list(reversed(bars))}}

    f = ec.EquityContextFetcher(http=FakeHttp(), days=250)
    ctx = f.fetch_asset("nvda-up-or-down-on-august-14-2026")
    assert ctx is not None
    assert ctx.symbol == "NVDA"
    assert ctx.display_name == "NVIDIA (NVDA)"
    assert ctx.is_proxy is False
    assert ctx.ma20 is not None

    # 商品代理
    g = f.fetch_asset("xauusd-up-or-down-on-august-14-2026")
    assert g is not None and g.symbol == "GLD" and g.is_proxy is True

    # 未知 slug
    assert f.fetch_asset("foo-bar-baz") is None

    # 大盘
    reg = f.fetch_regime()
    assert len(reg.components) == 3
    assert [c.symbol for c in reg.components] == ["SPY", "QQQ", "VXX"]


def test_prompt_contains_key_parts():
    bars = _make_bars([100.0 + i for i in range(70)])
    ctx = ec.compute_features(bars)
    ctx.symbol = "NVDA"
    ctx.display_name = "NVIDIA (NVDA)"
    ctx.source_kind = "s"
    ctx.is_proxy = False
    reg = ec.MarketRegime(components=[ctx])
    prompt = ec.build_equity_prompt(
        "nvda-up-or-down-on-august-14-2026", ctx, reg,
        ref_yes=0.49, question="NVIDIA (NVDA) Up or Down on August 14?",
        end_date="2026-08-14", secs_to_close=18 * 3600)
    assert "NVIDIA (NVDA)" in prompt
    assert "0.490" in prompt
    assert "2026-08-14" in prompt
    assert "大盘局势" in prompt
    assert "RSI" in prompt
    assert '"probability"' in prompt
    assert "±0.25" in prompt


def test_prompt_proxy_note():
    bars = _make_bars([100.0 + i for i in range(70)])
    ctx = ec.compute_features(bars)
    ctx.symbol = "GLD"
    ctx.display_name = "Gold (XAUUSD→GLD ETF 代理)"
    ctx.source_kind = "e"
    ctx.is_proxy = True
    reg = ec.MarketRegime(components=[])
    prompt = ec.build_equity_prompt("xauusd-up-or-down", ctx, reg, ref_yes=0.30)
    assert "代理标的" in prompt
    assert "大盘局势: 不可用" in prompt
