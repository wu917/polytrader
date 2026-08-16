"""equity_updown 深度测试：标的解析、prompt、边界、并发一致性。"""
from __future__ import annotations

import datetime

import pytest

from polytrader.models import Market, Outcome
from polytrader.strategies.equity_context import (
    EquityContext, MarketRegime, build_equity_prompt, resolve_symbol,
)
from polytrader.strategies.equity_updown import EquityUpdownStrategy, _ctx_summary
import tests.test_equity_updown as teu
from tests.test_equity_updown import FakeFetcher, FakeScorer, _market


def _ctx(close: float = 225.3) -> EquityContext:
    return EquityContext(
        symbol="NVDA", display_name="NVIDIA (NVDA)", source_kind="s",
        is_proxy=False, n_bars=70, last_close=close, prev_close=close - 1,
        last_change_pct=0.5, closes=[close] * 70,
        ma5=close, ma20=close, ma60=close, rsi14=60, vol20_pct=0.2,
        high20=close, low20=close, dist_high20_pct=0.0, dist_low20_pct=0.1,
        streak=2, up_days_20=12,
    )


# ---------- resolve_symbol 标的解析 ----------

def test_resolve_symbol_all_symbols():
    """17 个标的 slug 前缀全部可解析。"""
    cases = {
        "nvda": ("s", "NVDA"), "tsla": ("s", "TSLA"), "msft": ("s", "MSFT"),
        "aapl": ("s", "AAPL"), "amzn": ("s", "AMZN"), "googl": ("s", "GOOGL"),
        "meta": ("s", "META"), "coin": ("s", "COIN"), "pltr": ("s", "PLTR"),
        "spy": ("e", "SPY"), "qqq": ("e", "QQQ"), "ndx": ("e", "QQQ"),
        "xauusd": ("e", "GLD"), "xagusd": ("e", "SLV"), "wti": ("e", "USO"),
        "hsi": ("e", "EWH"), "ukx": ("e", "EWU"),
    }
    for prefix, (kind, sym) in cases.items():
        r = resolve_symbol(f"{prefix}-up-or-down-on-2026-08-16")
        assert r is not None, f"{prefix} 未解析"
        assert r[0] == kind and r[1] == sym, f"{prefix} → {r}"


def test_resolve_symbol_none_for_unknown():
    assert resolve_symbol("btc-updown-5m-1786600000") is None
    assert resolve_symbol("will-us-elections-2026") is None


# ---------- 边界过滤 ----------

def test_skip_price_out_of_band():
    """ref 超价格带 [0.03, 0.97] 跳过。"""
    strat = EquityUpdownStrategy(FakeScorer(0.9), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    m = _market(yes=0.985, no=0.015)  # YES 0.985 > 0.97
    assert strat.scan([m], max_workers=1) == []


def test_skip_non_binary_market():
    """非二元市场跳过。"""
    strat = EquityUpdownStrategy(FakeScorer(0.9), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    m = Market(condition_id="c1", question="Q", slug="nvda-up-or-down",
               outcomes=[Outcome(outcome_id="o0", token_id="t0", price="0.3", name="A"),
                         Outcome(outcome_id="o1", token_id="t1", price="0.3", name="B"),
                         Outcome(outcome_id="o2", token_id="t2", price="0.4", name="C")])
    assert strat.scan([m], max_workers=1) == []


def test_skip_when_fetcher_returns_none():
    """fetcher 拉取失败（返回 None）跳过。"""
    class EmptyFetcher:
        def fetch_asset(self, slug): return None
        def fetch_regime(self):
            return MarketRegime(components=[])

    strat = EquityUpdownStrategy(FakeScorer(0.9), fetcher=EmptyFetcher(),
                                 min_edge=0.05)
    assert strat.scan([_market()], max_workers=1) == []


def test_near_close_skip_boundary():
    """距结算恰 30 分钟边界：<30min 跳过，≥30min 评估。"""
    strat = EquityUpdownStrategy(FakeScorer(0.68), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    now = datetime.datetime.now(datetime.timezone.utc)
    # 25 分钟 → 跳过
    soon = (now + datetime.timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert strat.scan([_market(end_date=soon)], max_workers=1) == []
    # 45 分钟 → 评估出信号
    ok = (now + datetime.timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert len(strat.scan([_market(end_date=ok)], max_workers=1)) == 1


# ---------- prompt 内容 ----------

def test_prompt_contains_proxy_annotation():
    """商品 ETF 代理盘 prompt 标注代理。"""
    ctx = EquityContext(
        symbol="GLD", display_name="Gold (XAUUSD→GLD ETF 代理)", source_kind="e",
        is_proxy=True, n_bars=70, last_close=200, prev_close=199,
        last_change_pct=0.5, closes=[200] * 70,
        ma5=200, ma20=200, ma60=200, rsi14=55, vol20_pct=0.2,
        high20=200, low20=200, dist_high20_pct=0.0, dist_low20_pct=0.1,
        streak=1, up_days_20=10,
    )
    regime = MarketRegime(components=[ctx])
    p = build_equity_prompt("xauusd-up-or-down", ctx, regime, 0.5,
                            question="Gold Up or Down?", end_date="2026-08-16")
    assert "代理" in p
    assert "0.500" in p
    assert "距结算" not in p  # secs_to_close=None 时不显示


def test_prompt_secs_to_close_formatted():
    ctx = _ctx()
    regime = MarketRegime(components=[ctx])
    p = build_equity_prompt("nvda-up", ctx, regime, 0.5,
                            secs_to_close=7200)  # 2h
    assert "2小时0分" in p
    assert "市场隐含 P(涨): 0.500" in p
    assert "收盘" in p


# ---------- 并发一致性 ----------

def test_concurrent_scan_matches_serial(monkeypatch):
    """并发（max_workers=4）与串行结果一致（按 slug 排序确定性）。

    并发路径的 worker 会新建真实 fetcher/scorer（网络）——monkeypatch
    为 Fake 组件避免真实 HTTP。
    """
    import polytrader.strategies.equity_updown as eu
    from polytrader.ai.llm_scorer import LLMScorer

    class FakeHTTP:
        def __init__(self, *a, **kw): pass

    class FakeWorkerFetcher(FakeFetcher):
        pass

    class FakeScorer68(teu.FakeScorer):
        """worker 以关键字参数构造（api_key/base_url/model/http）——
        固定 p=0.68（FakeScorer 默认 p=0.5 会无信号）。"""

        def __init__(self, *a, **kw):
            super().__init__(p=0.68)

    monkeypatch.setattr(eu, "EquityContextFetcher", FakeWorkerFetcher)
    monkeypatch.setattr(eu, "LLMScorer", FakeScorer68)
    monkeypatch.setattr(eu, "HttpClient", FakeHTTP)

    m1 = _market(slug="nvda-up-or-down-on-august-16-a", yes=0.45)
    m2 = _market(slug="nvda-up-or-down-on-august-16-b", yes=0.55)
    m3 = _market(slug="nvda-up-or-down-on-august-16-c", yes=0.49)
    ser = EquityUpdownStrategy(FakeScorer(0.68), fetcher=FakeFetcher(),
                               min_edge=0.05)
    con = EquityUpdownStrategy(FakeScorer(0.68), fetcher=FakeFetcher(),
                               min_edge=0.05)
    s1 = ser.scan([m1, m2, m3], max_workers=1)
    s2 = con.scan([m1, m2, m3], max_workers=4)
    assert [x.market.slug for x in s1] == [x.market.slug for x in s2]
    assert len(s1) == len(s2) == 3


def test_max_markets_limit():
    """max_markets 限制信号数。"""
    strat = EquityUpdownStrategy(FakeScorer(0.68), fetcher=FakeFetcher(),
                                 min_edge=0.05, max_markets=1)
    markets = [_market(slug=f"nvda-up-or-down-{i}", yes=0.4) for i in range(3)]
    assert len(strat.scan(markets, max_workers=1)) == 1


# ---------- ctx 摘要 ----------

def test_ctx_summary_complete():
    s = _ctx_summary(_ctx())
    for k in ("symbol", "display", "close", "chg_pct", "ma5", "ma20", "ma60",
              "rsi14", "vol20_pct", "dist_high20_pct", "dist_low20_pct", "streak"):
        assert k in s
