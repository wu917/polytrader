"""event_market 策略与 scan_event_markets 过滤测试。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.models import Market, Outcome
from polytrader.strategies.event_market import EventMarketStrategy, calc_rr_ev

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "scan_event_markets", ROOT / "scripts" / "scan_event_markets.py")
scan_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(scan_mod)


# ---- calc_rr_ev ----
def test_calc_rr_ev_yes():
    """买 YES @0.30，胜率 0.5：RR=(1-0.3)/0.3=2.333，EV=0.5*0.7-0.5*0.3=0.2。"""
    rr, ev = calc_rr_ev(0.5, 0.30)
    assert rr == pytest.approx(2.333, rel=1e-3)
    assert ev == pytest.approx(0.2, rel=1e-3)


def test_calc_rr_ev_no():
    """买 NO @0.40（即 YES ref=0.60），NO 胜率 0.6：RR=(1-0.4)/0.4=1.5。"""
    rr, ev = calc_rr_ev(0.6, 0.40)
    assert rr == pytest.approx(1.5, rel=1e-3)
    assert ev == pytest.approx(0.6 * 0.6 - 0.4 * 0.4, rel=1e-3)


def test_calc_rr_ev_extreme_price():
    """极端价格（0 或 1）返回 0 保护。"""
    assert calc_rr_ev(0.5, 0.0) == (0.0, 0.0)
    assert calc_rr_ev(0.5, 1.0) == (0.0, 0.0)


# ---- EventMarketStrategy ----
class FakeScorer(LLMScorer):
    def __init__(self, p: float):
        super().__init__(api_key="k", base_url="https://api.openai.com/v1", model="m")
        self._p = p

    def score(self, question: str, description: str = "",
              market_category: str = "") -> float | None:
        return self._p


def _market(slug: str, ref_yes: float, q: str = "Event question?") -> Market:
    return Market(
        condition_id="c-" + slug[:8], question=q, slug=slug,
        category="Politics",
        outcomes=[Outcome(outcome_id="o0", token_id="t0",
                          price=str(ref_yes), name="Yes"),
                  Outcome(outcome_id="o1", token_id="t1",
                          price=str(round(1 - ref_yes, 4)), name="No")],
        liquidity=10000.0, active=True,
    )


def _book(price: float = 0.5):
    """构造 OrderBook（YES token t0 的盘口，供 LLMBookStrategy 使用）。"""
    from polytrader.models import OrderBook, OrderBookLevel
    b = OrderBook(token_id="t0")
    b.bids.append(OrderBookLevel(price=price - 0.01, size=1000))
    b.asks.append(OrderBookLevel(price=price + 0.01, size=1000))
    return b


def _scan(strat, markets):
    """带盘口调用 scan（LLMBookStrategy 需要 books）。"""
    books = {m.outcomes[0].token_id: _book(float(m.outcomes[0].price))
             for m in markets}
    return strat.scan(markets, books)


def test_strategy_signal_with_rr_ev():
    """LLM P=0.55 vs ref=0.30 → YES edge 0.25，RR=2.333，EV>0 → 出信号。"""
    strat = EventMarketStrategy(FakeScorer(0.55), min_edge=0.05, min_rr=1.5)
    sigs = _scan(strat, [_market("will-x-happen", 0.30)])
    assert len(sigs) == 1
    s = sigs[0]
    assert s.extra["rr"] == pytest.approx(2.333, rel=1e-3)
    assert s.extra["ev"] > 0
    assert s.edge == pytest.approx(0.25, rel=1e-3)


def test_strategy_rr_filter():
    """ref=0.49，edge 够但 RR=(1-0.49)/0.49=1.04 < 1.5 → 过滤。"""
    strat = EventMarketStrategy(FakeScorer(0.60), min_edge=0.05, min_rr=1.5)
    sigs = _scan(strat, [_market("will-x-happen", 0.49)])
    assert sigs == []


def test_strategy_ev_filter():
    """EV<=0 的场景：P 高但价格已贵（ref 高），EV 仍可能为正/负。"""
    # P=0.76 ref=0.70: edge=0.06 ≥0.05, RR=(1-0.7)/0.7=0.43 <1.5 → RR 过滤
    strat = EventMarketStrategy(FakeScorer(0.76), min_edge=0.05, min_rr=0.3)
    sigs = _scan(strat, [_market("will-x-happen", 0.70)])
    assert len(sigs) == 1
    ev = sigs[0].extra["ev"]
    # EV = 0.76*0.3 - 0.24*0.7 = 0.228-0.168 = 0.06 > 0
    assert ev == pytest.approx(0.06, abs=1e-3)
    # require_ev=True 时 EV>0 保留
    assert sigs[0].extra["rr"] == pytest.approx(0.429, rel=1e-2)


def test_strategy_no_signal_small_edge():
    strat = EventMarketStrategy(FakeScorer(0.32), min_edge=0.05, min_rr=1.5)
    sigs = _scan(strat, [_market("will-x-happen", 0.30)])
    assert sigs == []


def test_strategy_max_edge_filter():
    """LLM 与共识偏差 >30%（幻觉防护）→ 过滤。"""
    strat = EventMarketStrategy(FakeScorer(0.95), min_edge=0.05, min_rr=1.5,
                                max_edge=0.30)
    # ref=0.30, P=0.95 → edge=0.65 > 0.30 → 跳过
    sigs = _scan(strat, [_market("will-x-happen", 0.30)])
    assert sigs == []


# ---- scan_event_markets 过滤 ----
def test_is_event_market():
    assert scan_mod.is_event_market("will-x-happen", "Will X happen?") is True
    # 价格类
    assert scan_mod.is_event_market("bitcoin-above-60k", "BTC above?") is False
    assert scan_mod.is_event_market("will-wti-reach-90", "WTI reach?") is False
    assert scan_mod.is_event_market("btc-updown-5m", "BTC updown?") is False
    # 体育
    assert scan_mod.is_event_market("dota2-ts8-aur1", "Dota2 match") is False
    assert scan_mod.is_event_market("will-philadelphia-76ers-win-the-finals",
                                    "Will the 76ers win the NBA Finals?") is False
    assert scan_mod.is_event_market("clf-bay-rbl-2026-08-15-draw",
                                    "Bayern Legends vs RB Leipzig draw?") is False
    # 事件盘保留
    assert scan_mod.is_event_market("will-marco-rubio-win-the-2028-election",
                                    "Will Marco Rubio win the 2028 US Presidential Election?") is True
    assert scan_mod.is_event_market("fed-rate-hike-in-2026",
                                    "Fed rate hike in 2026?") is True
