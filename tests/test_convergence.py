"""收敛/确定性折价策略回测测试（合成数据）。"""
import pytest

from polytrader.ai.convergence import run_convergence_backtest
from polytrader.models import Market, Outcome


def make_market(slug, label_yes=True) -> Market:
    prices = ["1.0", "0.0"] if label_yes else ["0.0", "1.0"]
    return Market(
        condition_id=f"0x{slug}", question=f"Q {slug}", slug=slug,
        end_date="1700000000000", liquidity=1000.0, volume=500.0,
        closed=True, active=False,
        outcomes=[Outcome(outcome_id="o1", token_id=f"{slug}-yes", price=prices[0], name="Yes"),
                  Outcome(outcome_id="o2", token_id=f"{slug}-no", price=prices[1], name="No")],
    )


def trade(asset, price, ts, side="BUY"):
    return {"asset": asset, "price": price, "timestamp": ts, "side": side, "size": 100.0}


def test_convergence_buys_high_certainty_yes():
    """最后成交价 0.95 ≥ 阈值 → 买 YES，结算 1 → 盈利 5%。"""
    m = make_market("m1", label_yes=True)
    trades = {"0xm1": [
        trade("m1-yes", 0.80, 100),
        trade("m1-yes", 0.95, 200),   # 最后价 0.95
        trade("m1-no", 0.90, 150),    # 其他 token 忽略
    ]}
    r = run_convergence_backtest([m], trades, threshold=0.90)
    assert r.n_trades == 1
    t = r.trades[0]
    assert t.side == "YES"
    assert t.entry_price == pytest.approx(0.95)
    assert t.pnl_usd == pytest.approx(5.0)      # 100 * (1 - 0.95)
    assert t.settle_price == 1.0
    assert r.win_rate == 1.0


def test_convergence_buys_no_when_yes_tail_low():
    """最后成交价 0.05 → 买 NO（成本 0.95），结算 1 → 盈利 5%。"""
    m = make_market("m2", label_yes=False)
    trades = {"0xm2": [trade("m2-yes", 0.05, 300)]}
    r = run_convergence_backtest([m], trades, threshold=0.90)
    assert r.n_trades == 1
    t = r.trades[0]
    assert t.side == "NO"
    assert t.entry_price == pytest.approx(0.95)
    assert t.pnl_usd == pytest.approx(5.0)


def test_convergence_skips_uncertain_tail():
    """最后成交价 0.6（不确定）→ 不交易。"""
    m = make_market("m3", label_yes=True)
    trades = {"0xm3": [trade("m3-yes", 0.60, 100)]}
    r = run_convergence_backtest([m], trades, threshold=0.90)
    assert r.n_trades == 0


def test_convergence_mispriced_tail_loses():
    """最后成交价 0.95 但结算 NO → 亏损（市场尾段错价）。"""
    m = make_market("m4", label_yes=False)   # 实际结算 NO
    trades = {"0xm4": [trade("m4-yes", 0.95, 100)]}
    r = run_convergence_backtest([m], trades, threshold=0.90)
    assert r.n_trades == 1
    assert r.trades[0].pnl_usd == pytest.approx(-95.0)
    assert r.win_rate == 0.0


def test_convergence_to_dict():
    m = make_market("m5", label_yes=True)
    trades = {"0xm5": [trade("m5-yes", 0.97, 100)]}
    r = run_convergence_backtest([m], trades)
    d = r.to_dict()
    assert d["summary"]["n_trades"] == 1
    assert d["trades"][0]["side"] == "YES"
