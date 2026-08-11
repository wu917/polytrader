"""LLM 盘口策略测试（mock LLM scorer，无网络）。"""
import pytest

from polytrader.models import Market, OrderBook, OrderBookLevel, Outcome, Side, SignalType
from polytrader.strategies.llm_book import LLMBookStrategy, build_book_prompt


class FakeScorer:
    def __init__(self, proba=0.90, enabled=True):
        self.proba = proba
        self.enabled = enabled
        self.model = "fake"
        self.last_prompt = None
        self.calls = 0

    def score(self, question, prompt, category):
        self.calls += 1
        self.last_prompt = prompt
        return self.proba


def make_market(slug, liq=5000.0, category="crypto") -> Market:
    return Market(
        condition_id=f"0x{slug}", question=f"Will {slug} happen?", slug=slug,
        category=category, description="some description",
        liquidity=liq, volume=1000.0, active=True,
        end_date="2026-12-31T00:00:00Z",
        outcomes=[Outcome(outcome_id="o1", token_id=f"{slug}-yes", price="0.5", name="Yes"),
                  Outcome(outcome_id="o2", token_id=f"{slug}-no", price="0.5", name="No")],
    )


def book_for(token, ask, bid=None) -> OrderBook:
    return OrderBook(token_id=token,
                     bids=[OrderBookLevel(bid if bid is not None else max(ask - 0.02, 0.001), 100)],
                     asks=[OrderBookLevel(ask, 100)])


def test_build_book_prompt_includes_order_book():
    m = make_market("m1")
    b = book_for("m1-yes", ask=0.55)
    prompt = build_book_prompt(m, b)
    assert "best ask" in prompt and "0.55" in prompt
    assert "best bid" in prompt
    assert "spread" in prompt
    assert "depth" in prompt
    assert "liquidity" in prompt


def test_llm_strategy_emits_signal_when_edge_positive():
    scorer = FakeScorer(proba=0.90)
    s = LLMBookStrategy(scorer, min_edge=0.05)
    m = make_market("m1")
    # bid 0.58 / ask 0.62 → mid 0.60
    books = {
        m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, ask=0.62, bid=0.58),
        m.outcomes[1].token_id: book_for(m.outcomes[1].token_id, ask=0.42, bid=0.38),
    }
    signals = s.scan([m], books)
    assert len(signals) == 1
    assert signals[0].type == SignalType.AI_PROBABILITY
    assert signals[0].side == Side.BUY
    assert signals[0].edge == pytest.approx(0.40)   # p 0.90 - gamma ref 0.50
    assert signals[0].extra["llm_p"] == 0.90
    assert scorer.calls == 1


def test_llm_strategy_uses_mid_not_extreme_ask():
    """盘口只有极端挂单时（mid=0.5 垃圾值），参考价用 Gamma outcome price。"""
    scorer = FakeScorer(proba=0.70)
    s = LLMBookStrategy(scorer, min_edge=0.05, max_price=0.95)
    m = make_market("m5")
    # Gamma YES price = "0.60"；盘口 bid 0.05/ask 0.999（mid 0.5245 不可信）
    m.outcomes[0].price = "0.60"
    m.outcomes[1].price = "0.40"
    books = {
        m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, ask=0.999, bid=0.05),
        m.outcomes[1].token_id: book_for(m.outcomes[1].token_id, ask=0.999, bid=0.05),
    }
    signals = s.scan([m], books)
    assert len(signals) == 1
    assert signals[0].market_price == pytest.approx(0.60)  # gamma ref 而非 book mid
    assert signals[0].edge == pytest.approx(0.10)


def test_llm_strategy_no_side_signal():
    """LLM 对 NO 侧给出正 edge 时买入 NO。"""
    scorer = FakeScorer(proba=0.10)   # LLM 认为 YES 概率很低
    s = LLMBookStrategy(scorer, min_edge=0.05)
    m = make_market("m6")
    # YES mid 0.30 → yes_edge = 0.10-0.30 < 0；NO mid 0.70 → no_edge = 0.90-0.70 = 0.20
    books = {
        m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, ask=0.31, bid=0.29),
        m.outcomes[1].token_id: book_for(m.outcomes[1].token_id, ask=0.71, bid=0.69),
    }
    signals = s.scan([m], books)
    assert len(signals) == 1
    assert signals[0].outcome.name == "No"
    assert signals[0].edge == pytest.approx(0.40)   # (1-0.10) - 0.50
    assert signals[0].extra["side"] == "NO"


def test_llm_strategy_no_signal_when_edge_too_small():
    scorer = FakeScorer(proba=0.62)
    s = LLMBookStrategy(scorer, min_edge=0.05)
    m = make_market("m2")
    m.outcomes[0].price = "0.60"   # edge = 0.62 - 0.60 = 0.02 < 0.05
    books = {
        m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, 0.60),
        m.outcomes[1].token_id: book_for(m.outcomes[1].token_id, 0.40),
    }
    assert s.scan([m], books) == []


def test_llm_strategy_disabled_without_key():
    scorer = FakeScorer(enabled=False)
    s = LLMBookStrategy(scorer)
    m = make_market("m3")
    books = {m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, 0.40)}
    assert s.scan([m], books) == []


def test_llm_strategy_filters_liquidity_and_price_band():
    scorer = FakeScorer(proba=0.95)
    s = LLMBookStrategy(scorer, min_liquidity_usd=10000.0, min_price=0.10, max_price=0.90)
    low_liq = make_market("lowliq", liq=100.0)
    books = {
        low_liq.outcomes[0].token_id: book_for(low_liq.outcomes[0].token_id, 0.50),
        low_liq.outcomes[1].token_id: book_for(low_liq.outcomes[1].token_id, 0.50),
    }
    assert s.scan([low_liq], books) == []          # 流动性过滤

    extreme = make_market("extreme", liq=20000.0)
    extreme.outcomes[0].price = "0.02"            # gamma ref 在带外
    books2 = {
        extreme.outcomes[0].token_id: book_for(extreme.outcomes[0].token_id, 0.02),
        extreme.outcomes[1].token_id: book_for(extreme.outcomes[1].token_id, 0.98),
    }
    assert s.scan([extreme], books2) == []          # 价格带过滤


def test_llm_strategy_respects_max_markets():
    scorer = FakeScorer(proba=0.90)
    s = LLMBookStrategy(scorer, min_edge=0.05, max_markets=3)
    markets = [make_market(f"m{i}") for i in range(6)]
    books = {}
    for m in markets:
        books[m.outcomes[0].token_id] = book_for(m.outcomes[0].token_id, 0.40)
        books[m.outcomes[1].token_id] = book_for(m.outcomes[1].token_id, 0.60)
    signals = s.scan(markets, books)
    assert scorer.calls == 3
    assert len(signals) == 3


def test_llm_score_failure_skipped():
    class FailingScorer(FakeScorer):
        def score(self, question, prompt, category):
            self.calls += 1
            return None

    scorer = FailingScorer()
    s = LLMBookStrategy(scorer, min_edge=0.05)
    m = make_market("m4")
    books = {m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, 0.40)}
    assert s.scan([m], books) == []
