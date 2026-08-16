"""equity_updown 策略测试：mock LLM + mock 数据源。"""
from __future__ import annotations

import datetime

import pytest

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.models import Market, Outcome
from polytrader.strategies.equity_context import EquityContext
from polytrader.strategies.equity_updown import EquityUpdownStrategy, _ctx_summary


def _market(slug: str = "nvda-up-or-down-on-august-14-2026",
            yes: float = 0.49, no: float = 0.51,
            end_date: str | None = None) -> Market:
    # 结算时间必须为未来（策略距结算 <30 分钟会跳过）——动态生成防
    # 固定历史日期随时间过期（2026-08-16 曾致 5 个测试全部失败）
    if end_date is None:
        end_date = (datetime.datetime.now(datetime.timezone.utc)
                    + datetime.timedelta(hours=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return Market(
        condition_id="c1", question="NVIDIA (NVDA) Up or Down on August 14?",
        slug=slug, end_date=end_date,
        outcomes=[Outcome(outcome_id="o0", token_id="t0", price=str(yes), name="Up"),
                  Outcome(outcome_id="o1", token_id="t1", price=str(no), name="Down")],
    )


def _ctx(close: float = 225.3) -> EquityContext:
    return EquityContext(
        symbol="NVDA", display_name="NVIDIA (NVDA)", source_kind="s",
        is_proxy=False, n_bars=70, last_close=close, prev_close=close - 1,
        last_change_pct=0.5, closes=[close] * 70,
        ma5=close, ma20=close, ma60=close, rsi14=60, vol20_pct=0.2,
        high20=close, low20=close, dist_high20_pct=0.0, dist_low20_pct=0.1,
        streak=2, up_days_20=12,
    )


class FakeScorer(LLMScorer):
    """固定返回 P(涨)，并记录收到的 prompt。

    构造兼容策略并发路径的关键字参数调用（api_key/base_url/model/http）。
    """

    def __init__(self, p: float = 0.5, **kwargs):
        super().__init__(api_key="test-key", base_url="https://api.openai.com/v1",
                         model="test-model")
        self._p = p
        self.prompts: list[str] = []

    def score_with_reason(self, question: str, description: str = "",
                          market_category: str = "") -> tuple[float | None, str | None]:
        self.prompts.append(description)
        return self._p, f"fixed p={self._p}"


class FakeFetcher:
    def __init__(self, ctx: EquityContext | None = None):
        self._ctx = ctx or _ctx()

    def fetch_asset(self, slug: str) -> EquityContext | None:
        if "nvda" in slug or "spy" in slug:
            return self._ctx
        return None

    def fetch_regime(self):
        from polytrader.strategies.equity_context import MarketRegime
        return MarketRegime(components=[self._ctx])


def test_signal_yes_when_llm_bullish():
    """LLM P(涨)=0.68 vs ref 0.49 → YES 信号。"""
    strat = EquityUpdownStrategy(FakeScorer(0.68), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    signals = strat.scan([_market()], max_workers=1)
    assert len(signals) == 1
    s = signals[0]
    assert s.side.value == "BUY"
    assert s.outcome.name == "Up"
    assert s.edge == pytest.approx(0.19, rel=1e-6)
    assert s.extra["llm_p"] == pytest.approx(0.68)
    assert s.extra["ctx"]["symbol"] == "NVDA"


def test_signal_no_when_llm_bearish():
    """LLM P(涨)=0.32 → NO 信号（1-P=0.68 vs ref_no 0.51）。"""
    strat = EquityUpdownStrategy(FakeScorer(0.32), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    signals = strat.scan([_market()], max_workers=1)
    assert len(signals) == 1
    s = signals[0]
    assert s.outcome.name == "Down"
    assert s.edge == pytest.approx(0.17, rel=1e-6)


def test_no_signal_within_edge():
    """LLM P(涨)=0.52 与 ref 0.49 差异 < 阈值 → 无信号。"""
    strat = EquityUpdownStrategy(FakeScorer(0.52), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    signals = strat.scan([_market()], max_workers=1)
    assert signals == []
    assert strat.last_evaluations[0]["signal"] is False


def test_skip_non_equity_slug():
    """非股票/商品 slug（加密 5m）不评估。"""
    strat = EquityUpdownStrategy(FakeScorer(0.9), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    m = _market(slug="btc-updown-5m-1786600000")
    signals = strat.scan([m], max_workers=1)
    assert signals == []
    assert strat.last_evaluations == []


def test_skip_unresolvable_asset():
    """resolve 不到数据源的 slug 跳过（fetcher 返回 None）。"""
    strat = EquityUpdownStrategy(FakeScorer(0.9), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    m = _market(slug="coin-up-or-down-on-august-14-2026")
    signals = strat.scan([m], max_workers=1)
    assert signals == []
    assert strat.last_evaluations == []


def test_skip_near_close():
    """距结算 < 30 分钟跳过（尾段已定价）。"""
    strat = EquityUpdownStrategy(FakeScorer(0.9), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    import datetime as dt
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=10))
    m = _market(end_date=soon.strftime("%Y-%m-%dT%H:%M:%SZ"))
    signals = strat.scan([m], max_workers=1)
    assert signals == []
    assert strat.last_evaluations == []


def test_prompt_includes_market_and_regime():
    """prompt 包含市场问题、隐含概率、大盘。"""
    scorer = FakeScorer(0.68)
    strat = EquityUpdownStrategy(scorer, fetcher=FakeFetcher(), min_edge=0.05)
    strat.scan([_market()], max_workers=1)
    assert scorer.prompts, "prompt should be recorded"
    p = scorer.prompts[0]
    assert "NVIDIA (NVDA) Up or Down on August 14?" in p
    assert "0.490" in p
    assert "大盘局势" in p
    assert "S&P 500" in p or "NVDA" in p


def test_last_evaluations_recorded():
    strat = EquityUpdownStrategy(FakeScorer(0.68), fetcher=FakeFetcher(),
                                 min_edge=0.05)
    strat.scan([_market()], max_workers=1)
    ev = strat.last_evaluations[0]
    assert ev["slug"] == "nvda-up-or-down-on-august-14-2026"
    assert ev["evaluated"] is True
    assert ev["signal"] is True
    assert ev["best_edge"] == pytest.approx(0.19, rel=1e-6)


def test_ctx_summary_keys():
    c = _ctx()
    s = _ctx_summary(c)
    assert s["symbol"] == "NVDA"
    assert s["close"] == 225.3
    assert "rsi14" in s and "vol20_pct" in s
