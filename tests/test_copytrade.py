"""跟单引擎测试：FIFO PnL、钱包聚合、镜像过滤与去重。"""
import pytest

from polytrader.copytrade.leaderboard import SeedProvider, TradesAggregatorProvider
from polytrader.copytrade.mirror import MirrorEngine, _trade_id
from polytrader.copytrade.wallet_analysis import analyze_wallet_trades, score_wallet
from polytrader.models import Market, OrderBook, OrderBookLevel, Outcome, Side, SignalType, WalletProfile


def t(side, asset, size, price, ts, wallet="0xw1", title="M"):
    return {"proxyWallet": wallet, "side": side, "asset": asset,
            "size": size, "price": price, "timestamp": ts, "title": title}


# ---------- FIFO PnL ----------

def test_fifo_realized_pnl():
    trades = [
        t("BUY", "tokA", 100, 0.50, 100),
        t("BUY", "tokA", 50, 0.40, 200),
        t("SELL", "tokA", 120, 0.60, 300),
    ]
    p = analyze_wallet_trades(trades)
    # 100*(0.60-0.50) + 20*(0.60-0.40) = 10 + 4 = 14
    assert p.realized_profit_usd == pytest.approx(14.0)
    assert p.total_trades == 3
    assert p.win_rate == 1.0
    assert p.address == "0xw1"


def test_fifo_unrealized_mark():
    trades = [
        t("BUY", "tokA", 100, 0.50, 100),
        t("BUY", "tokA", 100, 0.40, 200),
    ]
    p = analyze_wallet_trades(trades, current_prices={"tokA": 0.60})
    # 剩余 200 股 avg cost 0.45, mark 0.60 → 200*0.15 = 30
    assert p.unrealized_profit_usd == pytest.approx(30.0)
    assert p.realized_profit_usd == 0.0


def test_fifo_partial_sell_keeps_remaining():
    trades = [
        t("BUY", "tokA", 100, 0.50, 100),
        t("SELL", "tokA", 40, 0.55, 200),
        t("SELL", "tokA", 60, 0.45, 300),
    ]
    p = analyze_wallet_trades(trades, current_prices={"tokA": 0.60})
    # realized = 40*0.05 + 60*(-0.05) = 2 - 3 = -1
    assert p.realized_profit_usd == pytest.approx(-1.0)
    assert p.win_rate == 0.5
    assert p.unrealized_profit_usd == 0.0  # 全部卖出


def test_multi_asset_aggregation():
    trades = [
        t("BUY", "tokA", 10, 0.50, 100),
        t("BUY", "tokB", 10, 0.30, 100),
        t("SELL", "tokB", 10, 0.40, 200),
    ]
    p = analyze_wallet_trades(trades)
    assert p.realized_profit_usd == pytest.approx(1.0)


def test_score_wallet_ranks():
    good = WalletProfile(address="a", realized_profit_usd=10000, total_trades=100, win_rate=0.6)
    bad = WalletProfile(address="b", realized_profit_usd=100, total_trades=5, win_rate=0.3)
    assert score_wallet(good) > score_wallet(bad)


# ---------- Provider ----------

class FakeDataApi:
    def __init__(self, trades_by_market):
        self._t = trades_by_market

    def get_trades(self, cid, limit=100):
        return self._t.get(cid, [])

    def get_user_trades(self, wallet, limit=100):
        return []


def test_trades_aggregator_provider():
    trades_m1 = [
        t("BUY", "tokA", 100, 0.50, 100, wallet="0xw1"),
        t("SELL", "tokA", 100, 0.80, 200, wallet="0xw1"),  # +30
        t("BUY", "tokA", 100, 0.50, 100, wallet="0xw2"),
        t("SELL", "tokA", 100, 0.40, 200, wallet="0xw2"),  # -10
    ]
    provider = TradesAggregatorProvider(FakeDataApi({"0xm1": trades_m1}), ["0xm1"])
    profiles = provider.fetch_profiles()
    assert len(profiles) == 2
    assert profiles[0].address == "0xw1"  # 高盈利排前
    assert profiles[0].realized_profit_usd == pytest.approx(30.0)
    assert all(p.source == "trades_aggregator" for p in profiles)


def test_seed_provider():
    p = WalletProfile(address="0xseed", realized_profit_usd=99999, total_trades=500)
    provider = SeedProvider([p])
    assert provider.fetch_profiles() == [p]


# ---------- MirrorEngine ----------

class FakeDataApiMirror:
    def __init__(self, trades_by_wallet):
        self._t = trades_by_wallet

    def get_user_trades(self, wallet, limit=100):
        return self._t.get(wallet, [])

    def get_trades(self, cid, limit=100):
        return []


def _mirror_engine(data_api, **kwargs):
    profile = WalletProfile(address="0xpro", realized_profit_usd=8000, total_trades=50,
                            win_rate=0.6,
                            recent_activity=[{"timestamp": 2000000000}])
    provider = SeedProvider([profile])
    params = {"min_profit_usd": 5000, "min_trades": 30}
    params.update(kwargs)
    return MirrorEngine(provider, data_api, **params)


def _market_with_book(slug="m", ask=0.60, token="tokA"):
    m = Market(condition_id=f"0x{slug}", question=f"Q {slug}", slug=slug,
               outcomes=[Outcome(outcome_id="o1", token_id=token, price="0.5", name="Yes"),
                         Outcome(outcome_id="o2", token_id=f"{token}-no", price="0.5", name="No")])
    b = OrderBook(token_id=token, asks=[OrderBookLevel(ask, 100)])
    return m, {token: b}


def test_mirror_emits_signal_for_qualified_wallet_buy():
    trades = [t("BUY", "tokA", 50, 0.55, 2100000000, wallet="0xpro")]
    engine = _mirror_engine(FakeDataApiMirror({"0xpro": trades}))
    m, books = _market_with_book(ask=0.56)  # 滑点 ~1.8% < 3%
    signals = engine.scan([m], books)
    assert len(signals) == 1
    assert signals[0].type == SignalType.COPYTRADE
    assert signals[0].side == Side.BUY
    assert signals[0].market_price == 0.56  # 用当前 ask
    assert signals[0].extra["mirror_wallet"] == "0xpro"


def test_mirror_dedup_repeated_trades():
    trades = [t("BUY", "tokA", 50, 0.55, 2100000000, wallet="0xpro")]
    engine = _mirror_engine(FakeDataApiMirror({"0xpro": trades}))
    m, books = _market_with_book(ask=0.56)
    assert len(engine.scan([m], books)) == 1
    assert len(engine.scan([m], books)) == 0  # 同一笔不重复镜像


def test_mirror_skips_sell():
    trades = [t("SELL", "tokA", 50, 0.55, 2100000000, wallet="0xpro")]
    engine = _mirror_engine(FakeDataApiMirror({"0xpro": trades}))
    m, books = _market_with_book(ask=0.60)
    assert engine.scan([m], books) == []


def test_mirror_skips_excessive_slippage():
    # 目标成交 0.55，当前 ask 0.70 → 滑点 27% > 3%
    trades = [t("BUY", "tokA", 50, 0.55, 2100000000, wallet="0xpro")]
    engine = _mirror_engine(FakeDataApiMirror({"0xpro": trades}), max_slippage=0.03)
    m, books = _market_with_book(ask=0.70)
    assert engine.scan([m], books) == []


def test_mirror_target_qualification():
    poor = WalletProfile(address="0xpoor", realized_profit_usd=100, total_trades=5,
                         recent_activity=[{"timestamp": 2000000000}])
    engine = _mirror_engine(FakeDataApiMirror({}), min_profit_usd=5000, min_trades=30)
    targets = engine.refresh_targets([poor])
    assert targets == []


def test_mirror_skips_unknown_token_when_yes_only():
    """mirror_yes_only 时未知 token 保守拒绝（评审修复）。"""
    trades = [t("BUY", "unknown-token-xyz", 50, 0.55, 2100000000, wallet="0xpro")]
    engine = _mirror_engine(FakeDataApiMirror({"0xpro": trades}))
    m, books = _market_with_book(ask=0.56)
    assert engine.scan([m], books) == []


def test_trade_id_stability():
    a = t("BUY", "tokA", 50, 0.55, 2100000000, wallet="0xpro")
    b = dict(a)
    assert _trade_id(a) == _trade_id(b)
    b["timestamp"] = 2100000001
    assert _trade_id(a) != _trade_id(b)
