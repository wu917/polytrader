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
        t = rm_trade(100.0, token_id=f"tok{i}")
        t.condition_id = f"0xc{i}"
        rm.record_trade(t)
    assert rm.open_position_count == 2
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
    rm.record_trade(t)                          # 成本 200 股 × 0.5 = 100
    unrealized = rm.mark_to_market({"tokA": 0.6})
    assert unrealized == pytest.approx(20.0)    # 200×0.6 - 100 成本
    assert rm.state.current_equity == pytest.approx(1020.0)
    assert rm.state.peak_equity == pytest.approx(1020.0)


def rm_trade(usd_value, shares=100.0, price=0.5, token_id="tokA") -> "Trade":
    from polytrader.models import Trade
    return Trade(signal=SignalType.AI_PROBABILITY, market_slug="m", condition_id="0xcond",
                 token_id=token_id, side=Side.BUY, price=price, shares=shares,
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


# ---------- PaperBroker（真实取价模拟）----------

class FakeClob:
    def __init__(self, book_or_none, ask_price=None):
        self._book = book_or_none
        self._ask = ask_price

    def get_book(self, token_id):
        if self._book is None:
            return None
        from polytrader.models import OrderBook, OrderBookLevel
        if self._ask is not None:
            return OrderBook(token_id=token_id, asks=[OrderBookLevel(self._ask, 100)])
        return self._book


def test_paper_broker_fills_at_ask():
    from polytrader.execution.broker import PaperBroker
    # 信号价 0.50，ask 0.52 → 滑点 4% < 5% 容忍
    broker = PaperBroker(FakeClob(True, ask_price=0.52), slippage_tolerance=0.05)
    sig = make_signal(price=0.50, size=100.0)
    trade = broker.place(sig)
    assert trade.status == "filled"
    assert trade.price == 0.52
    assert trade.mode == "paper"


def test_paper_broker_rejects_no_ask():
    from polytrader.execution.broker import PaperBroker
    broker = PaperBroker(FakeClob(None))
    trade = broker.place(make_signal(price=0.50, size=100.0))
    assert trade.status == "rejected"
    assert "no ask" in trade.reason


def test_paper_broker_rejects_excessive_slippage():
    from polytrader.execution.broker import PaperBroker
    # 信号价 0.50，ask 0.60 → 滑点 20% > 2% 容忍
    broker = PaperBroker(FakeClob(True, ask_price=0.60), slippage_tolerance=0.02)
    trade = broker.place(make_signal(price=0.50, size=100.0))
    assert trade.status == "rejected"
    assert "slippage" in trade.reason


def test_mark_to_market_drawdown_path():
    """价格下跌后权益下降、回撤上升，触发回撤熔断。"""
    rm = RiskManager(mode="dry-run", max_drawdown_pct=0.10, initial_equity=1000.0)
    t = rm_trade(200.0, shares=400.0, price=0.5)
    t.token_id = "tokA"
    rm.record_trade(t)                            # 成本 200
    rm.mark_to_market({"tokA": 0.50})             # 平：市值 200 = 成本
    assert rm.state.current_equity == pytest.approx(1000.0)
    assert rm.drawdown_pct == 0.0
    rm.mark_to_market({"tokA": 0.20})             # 市值 80 → 亏损 120
    assert rm.state.current_equity == pytest.approx(880.0)
    assert rm.drawdown_pct == pytest.approx(0.12)
    ok, reason = rm.check(make_signal(price=0.50, size=100.0))
    assert not ok and "drawdown" in reason


class RejectingBroker(DryRunBroker):
    """第 2 笔起的信号全部拒绝（模拟滑点/缺 ask）。"""

    def __init__(self):
        super().__init__()
        self.calls = 0

    def place(self, signal):
        self.calls += 1
        if self.calls >= 2:
            from polytrader.models import Trade
            return Trade(signal=signal.type, market_slug=signal.market.slug,
                         condition_id=signal.market.condition_id,
                         token_id=signal.outcome.token_id if signal.outcome else "",
                         side=signal.side, price=signal.market_price, shares=0.0,
                         usd_value=0.0, status="rejected", mode=self.mode,
                         reason="simulated rejection")
        return super().place(signal)


def test_order_manager_rolls_back_partial_group_fill():
    """组内第 2 笔被 broker 拒绝 → 已成交的第 1 笔回滚，无残留敞口。"""
    rm = RiskManager(mode="dry-run", max_position_usd=1000.0,
                     max_total_exposure_usd=5000.0, cooldown_seconds=0)
    om = OrderManager(RejectingBroker(), rm, bankroll_usd=10000.0, kelly_fraction=1.0)
    sig1 = make_signal(cid="0xcond", slug="m1", price=0.48, prob=0.6,
                       group_id="g1", stype=SignalType.ARBITRAGE)
    sig2 = make_signal(cid="0xcond", slug="m2", price=0.48, prob=0.6,
                       group_id="g1", stype=SignalType.ARBITRAGE)
    trades = om.execute([sig1, sig2])
    assert [t.status for t in trades] == ["rolled_back", "rejected"]
    assert om.trades == []                       # 已成交记录被移除
    assert rm.total_exposure == 0.0              # 风控状态干净
    assert rm.open_position_count == 0


def test_risk_exposure_marked_to_market():
    """敞口按市值重估：价格下跌后敞口同步下降。"""
    rm = RiskManager(mode="dry-run", max_position_usd=500.0)
    t = rm_trade(200.0, shares=400.0, price=0.5)
    t.token_id = "tokA"
    rm.record_trade(t)
    assert rm.exposure_of("0xcond") == pytest.approx(200.0)  # 无价格时按成本

    rm.update_prices({"tokA": 0.30})                       # 价格跌到 0.30
    assert rm.exposure_of("0xcond") == pytest.approx(120.0)  # 400 * 0.30


def test_risk_remove_trade_rollback():
    rm = RiskManager(mode="dry-run")
    t = rm_trade(200.0, shares=400.0, price=0.5)
    t.token_id = "tokA"
    rm.record_trade(t)
    rm.remove_trade(t)
    assert rm.total_exposure == 0.0
    assert rm.open_position_count == 0
    assert "tokA" not in rm.state.open_positions
