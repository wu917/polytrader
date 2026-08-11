"""套利引擎测试。"""
import pytest

from polytrader.models import Market, OrderBook, OrderBookLevel, Outcome, Side, SignalType
from polytrader.strategies.arbitrage import ArbitrageStrategy


def make_market(slug: str, cid: str = "0xcond", closed: bool = False,
                active: bool = True) -> Market:
    return Market(
        condition_id=cid, question=slug, slug=slug,
        closed=closed, active=active,
        outcomes=[
            Outcome(outcome_id="o1", token_id=f"{slug}-yes", price="0.5", name="Yes"),
            Outcome(outcome_id="o2", token_id=f"{slug}-no", price="0.5", name="No"),
        ],
    )


def book(ask: float, bid: float = 0.0) -> OrderBook:
    return OrderBook(
        token_id="t",
        bids=[OrderBookLevel(bid, 100.0)] if bid > 0 else [],
        asks=[OrderBookLevel(ask, 100.0)],
    )


def books_for(m: Market, ask_yes: float, ask_no: float) -> dict:
    return {
        m.outcomes[0].token_id: OrderBook(token_id=m.outcomes[0].token_id,
                                          asks=[OrderBookLevel(ask_yes, 100.0)]),
        m.outcomes[1].token_id: OrderBook(token_id=m.outcomes[1].token_id,
                                          asks=[OrderBookLevel(ask_no, 100.0)]),
    }


def test_binary_arbitrage_triggers():
    s = ArbitrageStrategy(min_edge=0.02)
    m = make_market("m1")
    signals = s.scan([m], books_for(m, 0.48, 0.48))
    assert len(signals) == 2
    assert all(sig.type == SignalType.ARBITRAGE for sig in signals)
    assert all(sig.side == Side.BUY for sig in signals)
    assert signals[0].edge == pytest.approx(0.04)
    # 两笔共享 group_id（风控按组同时执行）
    assert signals[0].extra["group_id"] == signals[1].extra["group_id"]


def test_binary_arbitrage_no_edge():
    s = ArbitrageStrategy(min_edge=0.02)
    m = make_market("m2")
    assert s.scan([m], books_for(m, 0.52, 0.50)) == []


def test_binary_arbitrage_boundary():
    """edge == min_edge 时触发（>= 判定）。"""
    s = ArbitrageStrategy(min_edge=0.02)
    m = make_market("m3")
    signals = s.scan([m], books_for(m, 0.49, 0.49))
    assert len(signals) == 2
    assert signals[0].edge == pytest.approx(0.02)


def test_closed_or_inactive_markets_skipped():
    s = ArbitrageStrategy(min_edge=0.02)
    m_closed = make_market("mc", closed=True)
    m_inactive = make_market("mi", active=False)
    m_ok = make_market("mok")
    signals = s.scan([m_closed, m_inactive, m_ok],
                     {**books_for(m_closed, 0.4, 0.4),
                      **books_for(m_inactive, 0.4, 0.4),
                      **books_for(m_ok, 0.4, 0.4)})
    assert len(signals) == 2  # 只有 m_ok 触发
    assert signals[0].market.slug == "mok"


def test_missing_book_skipped():
    s = ArbitrageStrategy(min_edge=0.02)
    m = make_market("m4")
    assert s.scan([m], {m.outcomes[0].token_id: book(0.4)}) == []


def test_categorical_arbitrage_triggers():
    s = ArbitrageStrategy(min_edge=0.02)
    candidates = [make_market(f"c{i}", cid=f"0xevt{i}") for i in range(3)]
    books = {}
    for m in candidates:
        books[m.outcomes[0].token_id] = book(0.30)
        books[m.outcomes[1].token_id] = book(0.70)
    signals = s.scan_categorical(candidates, books)
    assert len(signals) == 3
    assert signals[0].edge == pytest.approx(0.10)
    assert all(sig.extra["group_size"] == 3 for sig in signals)


def test_categorical_arbitrage_no_edge():
    s = ArbitrageStrategy(min_edge=0.02)
    candidates = [make_market(f"c{i}", cid=f"0xevt{i}") for i in range(3)]
    books = {}
    for m in candidates:
        books[m.outcomes[0].token_id] = book(0.33)   # sum=0.99 无边缘
        books[m.outcomes[1].token_id] = book(0.67)
    assert s.scan_categorical(candidates, books) == []


def test_categorical_requires_at_least_two():
    s = ArbitrageStrategy(min_edge=0.02)
    m = make_market("solo", cid="0xevt0")
    books = {m.outcomes[0].token_id: book(0.30), m.outcomes[1].token_id: book(0.70)}
    assert s.scan_categorical([m], books) == []


def test_categorical_group_size_cap():
    s = ArbitrageStrategy(min_edge=0.02, group_size_cap=4)
    candidates = [make_market(f"c{i}", cid=f"0xevt{i}") for i in range(6)]
    books = {}
    for m in candidates:
        books[m.outcomes[0].token_id] = book(0.15)   # 6*0.15=0.90
        books[m.outcomes[1].token_id] = book(0.85)
    signals = s.scan_categorical(candidates, books)
    assert len(signals) == 4  # 只取前 4 个
