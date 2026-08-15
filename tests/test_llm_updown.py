"""LLM updown 策略测试：并发评估 + 窗口内缓存。"""
import datetime
import time

import pytest

from polytrader.models import Market, Outcome, Side, SignalType
from polytrader.strategies import llm_updown


class FakeScorer:
    """记录调用次数的假 LLM 评分器。"""

    enabled = True
    model = "fake-model"

    def __init__(self, p: float = 0.65):
        self.p = p
        self.calls = 0

    def score_with_reason(self, question: str, description: str = "",
                          market_category: str = ""):
        self.calls += 1
        return self.p, f"test p={self.p}"


def _market(slug: str, ref_yes: str = "0.45") -> Market:
    end = (datetime.datetime.now(datetime.timezone.utc)
           + datetime.timedelta(seconds=300))
    return Market(
        condition_id=f"0x{slug[:40]:0<40}",
        question=f"Q {slug}", slug=slug,
        end_date=end.strftime("%Y-%m-%dT%H:%M:%S") + "Z",
        outcomes=[
            Outcome(outcome_id="o1", token_id=f"{slug}-yes",
                    price=ref_yes, name="Yes"),
            Outcome(outcome_id="o2", token_id=f"{slug}-no",
                    price=str(round(1 - float(ref_yes), 4)), name="No"),
        ],
    )


@pytest.fixture(autouse=True)
def fake_context(monkeypatch):
    """行情上下文打桩（避免真实请求 Binance/OKX）。"""
    ctx = {"symbol": "BTCUSDT", "price": 60000.0, "last5_chg": 0.01,
           "per_min": [0.01, 0.0, 0.01, -0.01, 0.01], "range5_pct": 0.02,
           "up_minutes": 3, "down_minutes": 2, "window_s": 300}
    monkeypatch.setattr(llm_updown, "fetch_market_context",
                        lambda coin, window_s=300: ctx)


def _strategy(scorer, **kw):
    params = {"min_edge": 0.05, "min_price": 0.03, "max_price": 0.97,
              "max_markets": 10, "coin_map": {"btc": "btc"}}
    params.update(kw)
    return llm_updown.LLMUpdownStrategy(scorer, **params)


def test_scan_concurrent_emits_signals_in_order():
    """并发评估后信号按 markets 原序输出（可复现）。"""
    scorer = FakeScorer(p=0.65)
    strat = _strategy(scorer, max_workers=4)
    markets = [_market(f"btc-updown-5m-17866{i:04d}") for i in range(4)]
    signals = strat.scan(markets)
    # ref_yes=0.45, p=0.65 → yes_edge=0.20 ≥ 0.05 全出信号
    assert len(signals) == 4
    assert [s.market.slug for s in signals] == [m.slug for m in markets]
    assert all(s.type == SignalType.AI_PROBABILITY for s in signals)
    assert all(s.side == Side.BUY for s in signals)
    assert scorer.calls == 4  # 4 个市场各调一次 LLM


def test_scan_concurrent_skips_failed_market():
    """单个市场评估失败不影响其余市场。"""
    scorer = FakeScorer(p=0.65)
    strat = _strategy(scorer, max_workers=3)

    def boom(market, coin, window_s):
        if "fail" in market.slug:
            return None
        return strat.score_updown(market, coin, window_s)

    strat._score_cached = boom
    markets = [_market("btc-updown-5m-fail-0001"),
               _market("btc-updown-5m-ok-0002")]
    signals = strat.scan(markets)
    assert len(signals) == 1
    assert signals[0].market.slug == "btc-updown-5m-ok-0002"
    evals = {e["slug"]: e["evaluated"] for e in strat.last_evaluations}
    assert evals["btc-updown-5m-fail-0001"] is False


def test_window_cache_reuses_llm_result():
    """TTL 内二次 scan 复用缓存，不重复调 LLM。"""
    scorer = FakeScorer(p=0.65)
    strat = _strategy(scorer, max_workers=4, cache_ttl=45.0)
    markets = [_market("btc-updown-5m-1786700001")]
    assert len(strat.scan(markets)) == 1
    assert scorer.calls == 1
    assert len(strat.scan(markets)) == 1  # TTL 内缓存命中
    assert scorer.calls == 1  # 未新增 LLM 调用


def test_cache_expiry_triggers_new_call():
    """缓存过期后再次 scan 重新调 LLM。"""
    scorer = FakeScorer(p=0.65)
    strat = _strategy(scorer, max_workers=4, cache_ttl=0.001)
    markets = [_market("btc-updown-5m-1786700002")]
    strat.scan(markets)
    assert scorer.calls == 1
    time.sleep(0.01)
    strat.scan(markets)
    assert scorer.calls == 2  # 过期重新评估


def test_scan_evaluation_limited_by_max_markets():
    """评估量不超过 max_markets（并发不放大 LLM 调用量）。"""
    scorer = FakeScorer(p=0.65)
    strat = _strategy(scorer, max_workers=8, max_markets=2)
    markets = [_market(f"btc-updown-5m-17866{i:04d}") for i in range(5)]
    strat.scan(markets)
    assert scorer.calls == 2
