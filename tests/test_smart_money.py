"""聪明钱回测测试（合成数据，无网络）。"""
import pytest

from polytrader.copytrade.smart_money import run_smart_money_backtest
from polytrader.models import Market, Outcome


def make_market(slug, end_ts, label_yes) -> Market:
    prices = ["1.0", "0.0"] if label_yes else ["0.0", "1.0"]
    return Market(
        condition_id=f"0x{slug}", question=f"Q {slug}", slug=slug,
        end_date=f"{int(end_ts * 1000)}",
        liquidity=1000.0, volume=500.0, closed=True, active=False,
        outcomes=[Outcome(outcome_id="o1", token_id=f"{slug}-yes", price=prices[0], name="Yes"),
                  Outcome(outcome_id="o2", token_id=f"{slug}-no", price=prices[1], name="No")],
    )


def trade(wallet, side, asset, price, ts, cid="0xm", size=100.0):
    return {"proxyWallet": wallet, "side": side, "asset": asset,
            "price": price, "size": size, "timestamp": ts}


def _synthetic():
    """构造：预热期钱包 A 持续盈利（BUY 低价 YES → SELL 高价），
    测试期 A 在市场 m5/m6 上 BUY 正确方向的 YES。"""
    markets = []
    for i in range(10):
        end_ts = 1_700_000_000.0 + i * 7 * 86400.0
        markets.append(make_market(f"m{i}", end_ts, label_yes=(i >= 5)))

    trades = {}
    # 预热期（m0-m4 期间）：A 在 m0 低价买 YES 高价卖（盈利）；B 反向（亏损）
    t0 = 1_700_000_000.0
    trades["0xm0"] = [
        trade("0xA", "BUY", "m0-yes", 0.20, t0 + 3600, "0xm0"),
        trade("0xA", "SELL", "m0-yes", 0.90, t0 + 7200, "0xm0"),   # +70
        trade("0xB", "BUY", "m0-yes", 0.80, t0 + 3600, "0xm0"),
        trade("0xB", "SELL", "m0-yes", 0.30, t0 + 7200, "0xm0"),   # -50
    ]
    # 测试期：m5/m6（label YES），A 持续参与（early 成交）并在临近结算 BUY
    # size=5000 → 每市场名义额 >= 1000 门槛
    for i, cid in enumerate(["0xm5", "0xm6"]):
        end_ts = 1_700_000_000.0 + (5 + i) * 7 * 86400.0
        trades[cid] = [
            trade("0xA", "SELL", f"m{5 + i}-yes", 0.30, end_ts - 100 * 3600, cid, size=5000),
            trade("0xA", "BUY", f"m{5 + i}-yes", 0.40, end_ts - 7200, cid, size=5000),
            trade("0xOther", "BUY", f"m{5 + i}-yes", 0.45, end_ts - 1800, cid, size=5000),
        ]
    return markets, trades


def test_smart_money_follows_profitable_wallet():
    markets, trades = _synthetic()
    result = run_smart_money_backtest(
        markets, trades,
        lookback_days=90, top_k=2, min_trades=2, min_profit_usd=10.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=48,
    )
    # 预热 5 个 / 测试 5 个
    assert result.n_preheat == 5
    assert result.n_test == 5
    # A 在 m5/m6 的 BUY 被跟随（2 笔），B 不入选
    assert result.n_trades == 2
    assert result.top_wallets_used == 1
    assert all(t.wallet == "0xA" for t in result.trades)
    # 结算：m5/m6 label=YES=1 → pnl = 100*(1-0.40) = +60/笔
    assert result.total_pnl_usd == pytest.approx(120.0)
    assert result.win_rate == 1.0
    assert result.total_return_pct == pytest.approx(60.0)
    # 交易单字段完整
    t0 = result.trades[0]
    assert t0.entry_price == 0.40
    assert t0.settle_price == 1.0


def test_smart_money_no_trades_without_qualified_wallets():
    markets, trades = _synthetic()
    # 提高盈利门槛使 A 不入选
    result = run_smart_money_backtest(
        markets, trades,
        top_k=2, min_trades=2, min_profit_usd=100000.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=48,
    )
    assert result.n_trades == 0
    assert result.total_pnl_usd == 0.0


def test_smart_money_no_lookahead_entry():
    """入场仅限结算前 follow_window 内的 BUY。"""
    markets, trades = _synthetic()
    # follow_window=0.5h：A 的 BUY 在结算前 1h → 在窗口外 → 不跟随
    result = run_smart_money_backtest(
        markets, trades,
        top_k=2, min_trades=2, min_profit_usd=10.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=0.5,
    )
    assert result.n_trades == 0


def test_smart_money_to_dict_serializable():
    markets, trades = _synthetic()
    result = run_smart_money_backtest(
        markets, trades, top_k=2, min_trades=2, min_profit_usd=10.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=48,
    )
    d = result.to_dict()
    assert d["summary"]["n_trades"] == len(d["trades"])
    assert "meta" in d


def test_smart_money_volume_threshold():
    """市场交易额低于门槛时跳过（盘口条件 1）。"""
    markets, trades = _synthetic()
    # 门槛 10000 > 市场名义额 ~4250 → 0 交易
    result = run_smart_money_backtest(
        markets, trades, top_k=2, min_trades=2, min_profit_usd=10.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=48,
        min_market_volume_usd=10000.0,
    )
    assert result.n_trades == 0
    # 门槛 1000 < 4250 → 正常跟随
    result2 = run_smart_money_backtest(
        markets, trades, top_k=2, min_trades=2, min_profit_usd=10.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=48,
        min_market_volume_usd=1000.0,
    )
    assert result2.n_trades == 2


def test_smart_money_confirmation_filter():
    """聪明钱确认：top 钱包须在市场早期已有成交（持续参与）才跟随。

    m6：窗口外只有 Other 成交（无 A）、窗口内 A 有 BUY →
    confirm=True 时 A 非"持续参与者"→ m6 跳过（只跟 m5）；
    confirm=False 时跟随 A 的 BUY（2 笔）。
    """
    markets, trades = _synthetic()
    end6 = 1_700_000_000.0 + 6 * 7 * 86400.0
    trades["0xm6"] = [
        trade("0xOther", "SELL", "m6-yes", 0.50, end6 - 100 * 3600, "0xm6", size=5000),
        trade("0xA", "BUY", "m6-yes", 0.40, end6 - 7200, "0xm6", size=5000),
    ]
    result = run_smart_money_backtest(
        markets, trades, top_k=2, min_trades=2, min_profit_usd=10.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=48,
        smart_money_confirmation=True,
    )
    assert result.n_trades == 1
    assert result.trades[0].market_slug == "m5"
    # 关闭确认 → 跟随 m6 的 A BUY → 2 笔
    result2 = run_smart_money_backtest(
        markets, trades, top_k=2, min_trades=2, min_profit_usd=10.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=48,
        smart_money_confirmation=False,
    )
    assert result2.n_trades == 2


def test_smart_money_lifetime_follow():
    """生命周期模式（follow_window_h<=0）：跟随 top 钱包最后一笔 BUY。

    m6 中 A 有两笔 BUY（早期 0.30 + 晚期 0.40）→ 只跟最后一笔（0.40）。
    """
    markets, trades = _synthetic()
    end6 = 1_700_000_000.0 + 6 * 7 * 86400.0
    trades["0xm6"] = [
        trade("0xA", "SELL", "m6-yes", 0.30, end6 - 100 * 3600, "0xm6", size=5000),
        trade("0xA", "BUY", "m6-yes", 0.30, end6 - 96 * 3600, "0xm6", size=5000),
        trade("0xA", "BUY", "m6-yes", 0.40, end6 - 7200, "0xm6", size=5000),
    ]
    result = run_smart_money_backtest(
        markets, trades, top_k=2, min_trades=2, min_profit_usd=10.0,
        train_frac=0.5, size_usd=100.0, follow_window_h=0,   # 全生命周期
    )
    # m5（1 笔）+ m6（1 笔，取最后 BUY 0.40）
    assert result.n_trades == 2
    m6_trades = [t for t in result.trades if t.market_slug == "m6"]
    assert len(m6_trades) == 1
    assert m6_trades[0].entry_price == 0.40
