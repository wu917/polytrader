"""风控与执行层测试：Kelly、RiskManager 熔断、Broker 三模式、OrderManager。"""
import pytest

from polytrader.execution.broker import DryRunBroker, LiveBroker, make_broker
from polytrader.execution.order_manager import OrderManager
from polytrader.models import Market, OrderBook, OrderBookLevel, Outcome, Side, Signal, SignalType
from polytrader.risk.kelly import kelly_fraction, kelly_size_usd
from polytrader.risk.risk_manager import RiskManager


def make_signal(price=0.50, prob=0.55, cid="0xcond", slug="m", size=100.0,
                group_id=None, stype=SignalType.AI_PROBABILITY) -> Signal:
    m = Market(condition_id=cid, question=slug, slug=slug,
               outcomes=[Outcome(outcome_id="o1", token_id=f"{slug}-yes", price="0.5", name="Yes"),
                         Outcome(outcome_id="o2", token_id=f"{slug}-no", price="0.5", name="No")])
    extra = {"group_id": group_id} if group_id else {}
    return Signal(type=stype, market=m, outcome=m.outcomes[0], side=Side.BUY,
                  probability=prob, fair_price=price, edge=prob - price,
                  market_price=price, size_usd=size, extra=extra)


# ---------- Kelly ----------

def test_kelly_fraction():
    # 概率 0.6 价格 0.5：b=1, f = (1*0.6-0.4)/1 = 0.2
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    # 概率 0.5 价格 0.5：f = 0（无优势不下注）
    assert kelly_fraction(0.5, 0.5) == pytest.approx(0.0)
    # 概率低于隐含概率 → 0
    assert kelly_fraction(0.4, 0.5) == 0.0
    # 边界价格
    assert kelly_fraction(0.9, 0.0) == 0.0
    assert kelly_fraction(0.9, 1.0) == 0.0


def test_kelly_size_capped():
    # bankroll 10000, f=0.2, fraction=0.25 → 500，但 max_position=200 → 200
    size = kelly_size_usd(0.6, 0.5, bankroll_usd=10000, fraction=0.25, max_position_usd=200.0)
    assert size == 200.0
    # 无上限时：10000*0.2*0.25 = 500
    size2 = kelly_size_usd(0.6, 0.5, bankroll_usd=10000, fraction=0.25, max_position_usd=10000.0)
    assert size2 == 500.0


# ---------- RiskManager ----------

def test_risk_price_band():
    rm = RiskManager(mode="dry-run", min_price=0.03, max_price=0.97)
    ok, _ = rm.check(make_signal(price=0.50))
    assert ok
    ok, reason = rm.check(make_signal(price=0.01))
    assert not ok and "outside band" in reason
    ok, _ = rm.check(make_signal(price=0.99))
    assert not ok


def test_risk_daily_loss_circuit_breaker():
    rm = RiskManager(mode="dry-run", max_daily_loss_usd=100.0)
    rm.record_pnl(-150.0)
    ok, reason = rm.check(make_signal(price=0.50))
    assert not ok and "circuit breaker" in reason


def test_risk_drawdown_circuit_breaker():
    rm = RiskManager(mode="dry-run", max_drawdown_pct=0.15, initial_equity=1000.0)
    rm.state.current_equity = 800.0  # 20% 回撤
    ok, reason = rm.check(make_signal(price=0.50))
    assert not ok and "drawdown" in reason


def test_risk_per_market_and_total_exposure():
    rm = RiskManager(mode="dry-run", max_position_usd=500.0, max_total_exposure_usd=600.0)
    ok, _ = rm.check(make_signal(price=0.50, size=300.0))
    assert ok
    # 同市场累计超单标的上限
    rm.record_trade(rm_trade(300.0))
    ok, reason = rm.check(make_signal(price=0.50, size=300.0))
    assert not ok and "per-market" in reason


def test_risk_max_open_positions():
    rm = RiskManager(mode="dry-run", max_open_positions=2, max_position_usd=1000.0,
                     max_total_exposure_usd=5000.0)
    for i in range(2):
        t = rm_trade(100.0)
        t.condition_id = f"0xc{i}"
        rm.record_trade(t)
    ok, reason = rm.check(make_signal(cid="0xcnew", price=0.50, size=100.0))
    assert not ok and "max open positions" in reason


def test_risk_cooldown():
    rm = RiskManager(mode="dry-run", cooldown_seconds=300)
    rm.record_trade(rm_trade(100.0))
    ok, reason = rm.check(make_signal(price=0.50, size=100.0))
    assert not ok and "cooldown" in reason


def test_risk_live_mode_rejected():
    rm = RiskManager(mode="live")
    ok, reason = rm.check(make_signal(price=0.50))
    assert not ok and "live" in reason


def test_mark_to_market_updates_equity():
    rm = RiskManager(mode="dry-run", initial_equity=1000.0)
    t = rm_trade(100.0, shares=200.0)
    t.token_id = "tokA"
    rm.record_trade(t)
    unrealized = rm.mark_to_market({"tokA": 0.6})
    assert unrealized == pytest.approx(120.0)
    assert rm.state.current_equity == pytest.approx(1120.0)
    assert rm.state.peak_equity == pytest.approx(1120.0)


def rm_trade(usd_value, shares=100.0, price=0.5) -> "Trade":
    from polytrader.models import Trade
    return Trade(signal=SignalType.AI_PROBABILITY, market_slug="m", condition_id="0xcond",
                 token_id="tokA", side=Side.BUY, price=price, shares=shares,
                 usd_value=usd_value, status="filled", mode="dry-run")


# ---------- Broker ----------

def test_dry_run_broker_fills():
    broker = DryRunBroker()
    sig = make_signal(price=0.50, size=100.0)
    trade = broker.place(sig)
    assert trade.status == "filled"
    assert trade.shares == pytest.approx(200.0)
    assert trade.usd_value == pytest.approx(100.0)
    assert trade.mode == "dry-run"


def test_live_broker_refuses():
    broker = LiveBroker(credentials_present=False)
    trade = broker.place(make_signal(price=0.50, size=100.0))
    assert trade.status == "rejected"
    assert "not implemented" in trade.reason


def test_make_broker_modes():
    assert make_broker("dry-run").mode == "dry-run"
    assert make_broker("paper").mode == "paper"
    assert make_broker("live").mode == "live"
    with pytest.raises(ValueError):
        make_broker("bogus")


# ---------- OrderManager ----------

def test_order_manager_executes_with_risk():
    rm = RiskManager(mode="dry-run", max_position_usd=500.0)
    om = OrderManager(DryRunBroker(), rm, bankroll_usd=10000.0, kelly_fraction=1.0)
    sig = make_signal(price=0.50, prob=0.6, size=1000.0)
    trades = om.execute([sig])
    assert len(trades) == 1
    assert trades[0].status == "filled"
    assert rm.total_exposure == pytest.approx(trades[0].usd_value)
    snap = om.snapshot()
    assert snap["trades_total"] == 1


def test_order_manager_group_atomicity():
    """套利组：任一笔被风控拒绝则整组放弃。"""
    rm = RiskManager(mode="dry-run", max_position_usd=200.0)
    om = OrderManager(DryRunBroker(), rm, bankroll_usd=10000.0, kelly_fraction=1.0)
    # 先占掉同市场敞口：150 + Kelly 200 = 350 > 200 → 整组拒绝
    rm.record_trade(rm_trade(150.0))
    sig1 = make_signal(cid="0xcond", slug="m1", price=0.48, prob=0.6, size=250.0,
                       group_id="g1", stype=SignalType.ARBITRAGE)
    sig2 = make_signal(cid="0xcond", slug="m2", price=0.48, prob=0.6, size=250.0,
                       group_id="g1", stype=SignalType.ARBITRAGE)
    trades = om.execute([sig1, sig2])
    assert trades == []  # 整组放弃
    assert om.trades == []


def test_order_manager_group_succeeds():
    rm = RiskManager(mode="dry-run", max_position_usd=1000.0,
                     max_total_exposure_usd=5000.0, cooldown_seconds=0)
    om = OrderManager(DryRunBroker(), rm, bankroll_usd=10000.0, kelly_fraction=1.0)
    sig1 = make_signal(cid="0xcond", slug="m1", price=0.48, prob=0.6,
                       group_id="g1", stype=SignalType.ARBITRAGE)
    sig2 = make_signal(cid="0xcond", slug="m2", price=0.48, prob=0.6,
                       group_id="g1", stype=SignalType.ARBITRAGE)
    trades = om.execute([sig1, sig2])
    assert len(trades) == 2
    assert all(t.status == "filled" for t in trades)


def test_order_manager_kelly_zero_skips():
    rm = RiskManager(mode="dry-run")
    om = OrderManager(DryRunBroker(), rm, bankroll_usd=10000.0, kelly_fraction=1.0)
    sig = make_signal(price=0.50, prob=0.45)  # Kelly=0
    assert om.execute([sig]) == []
