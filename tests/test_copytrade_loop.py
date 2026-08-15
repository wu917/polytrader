"""跟单循环 + 官方排行榜 + 活动流扫描测试。"""
import time

import pytest

from polytrader.copytrade.leaderboard import OfficialLeaderboardProvider
from polytrader.copytrade.mirror import MirrorEngine, _trade_id
from polytrader.copytrade.wallet_analysis import analyze_wallet_trades
from polytrader.models import OrderBook, OrderBookLevel, Side, SignalType, WalletProfile


# ---------- 官方排行榜 Provider ----------

class FakeLeaderboardApi:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.calls = []

    def get_leaderboard(self, limit=25, offset=0, time_period="MONTH",
                        order_by="PNL", category="OVERALL"):
        self.calls.append((limit, offset, time_period, order_by, category))
        return self.rows

    def get_user_activity(self, wallet, limit=50):
        return []

    def get_trades(self, cid, limit=100):
        return []

    def get_user_trades(self, wallet, limit=100):
        return []


def _lb_row(addr, pnl, vol, rank="1"):
    return {"rank": rank, "proxyWallet": addr, "userName": "u" + addr[-4:],
            "vol": vol, "pnl": pnl, "xUsername": "x", "verifiedBadge": False}


def test_leaderboard_provider_monthly_mapping():
    api = FakeLeaderboardApi([_lb_row("0xaaa", 1200.5, 8000),
                              _lb_row("0xbbb", 300.0, 9000)])
    provider = OfficialLeaderboardProvider(api, time_period="MONTH", top_n=10)
    profiles = provider.fetch_profiles()
    assert len(profiles) == 2
    assert profiles[0].address == "0xaaa"
    assert profiles[0].realized_profit_usd == pytest.approx(1200.5)
    assert profiles[0].source == "leaderboard"
    assert profiles[0].score == pytest.approx(1200.5)
    assert api.calls[0][2] == "MONTH"  # time_period 透传


def test_leaderboard_provider_skips_empty_address():
    api = FakeLeaderboardApi([_lb_row("", 100, 1000)])
    provider = OfficialLeaderboardProvider(api)
    assert provider.fetch_profiles() == []


def test_leaderboard_provider_sorts_by_pnl():
    api = FakeLeaderboardApi([_lb_row("0xbbb", 10, 1000),
                              _lb_row("0xaaa", 5000, 2000)])
    provider = OfficialLeaderboardProvider(api)
    profiles = provider.fetch_profiles()
    assert [p.address for p in profiles] == ["0xaaa", "0xbbb"]


# ---------- 活动流扫描（scan_activity）----------

class FakeActivityApi:
    def __init__(self, activities_by_wallet):
        self._a = activities_by_wallet

    def get_user_activity(self, wallet, limit=50):
        return self._a.get(wallet, [])

    def get_trades(self, cid, limit=100):
        return []

    def get_user_trades(self, wallet, limit=100):
        return []


def act(**kw):
    base = {"type": "TRADE", "side": "BUY", "size": "25", "price": "0.49",
            "asset": "tokA", "transactionHash": "0xhash1",
            "timestamp": int(time.time()), "conditionId": "0xcond1",
            "title": "Will X happen?", "slug": "will-x-happen",
            "outcome": "Yes", "outcomeIndex": 0}
    base.update(kw)
    return base


def _activity_engine(api, **kwargs):
    profile = WalletProfile(address="0xpro", realized_profit_usd=8000,
                            source="leaderboard")
    from polytrader.copytrade.leaderboard import SeedProvider
    params = {"min_profit_usd": 5000, "min_trades": 0, "require_activity": False}
    params.update(kwargs)
    return MirrorEngine(SeedProvider([profile]), api, **params)


def test_activity_scan_emits_signal():
    api = FakeActivityApi({"0xpro": [act()]})
    engine = _activity_engine(api)
    engine.refresh_targets()
    signals = engine.scan_activity()
    assert len(signals) == 1
    s = signals[0]
    assert s.type == SignalType.COPYTRADE
    assert s.side == Side.BUY
    assert s.market.slug == "will-x-happen"
    assert s.market.condition_id == "0xcond1"
    assert s.outcome.token_id == "tokA"
    assert s.market_price == pytest.approx(0.49)
    assert s.extra["mirror_wallet"] == "0xpro"


def test_activity_scan_dedup_by_transaction_hash():
    api = FakeActivityApi({"0xpro": [act()]})
    engine = _activity_engine(api)
    assert len(engine.scan_activity()) == 1
    assert len(engine.scan_activity()) == 0  # transactionHash 去重


def test_activity_scan_skips_non_trade_and_sell():
    api = FakeActivityApi({"0xpro": [
        act(type="REWARD", amount="5"),
        act(side="SELL", transactionHash="0xsell", conditionId="0xcondOther",
            outcomeIndex=1),
        act(transactionHash="0xbuy"),
    ]})
    engine = _activity_engine(api)
    signals = engine.scan_activity()
    assert len(signals) == 1
    assert signals[0].extra["mirror_trade_id"] == "transactionHash:0xbuy"


def test_activity_scan_yes_only_skips_no_side():
    api = FakeActivityApi({"0xpro": [act(outcome="No", outcomeIndex=1,
                                        transactionHash="0xno")]})
    engine = _activity_engine(api)
    assert engine.scan_activity() == []


def test_activity_scan_missing_asset_skipped():
    api = FakeActivityApi({"0xpro": [act(asset="")]})
    engine = _activity_engine(api)
    assert engine.scan_activity() == []


def test_activity_scan_slippage_filter_with_book():
    api = FakeActivityApi({"0xpro": [act(price="0.49")]})
    engine = _activity_engine(api, max_slippage=0.03)
    book = OrderBook(token_id="tokA", asks=[OrderBookLevel(0.70, 100)])
    # 目标成交 0.49，ask 0.70 → 滑点 43% 超限
    assert engine.scan_activity({"tokA": book}) == []
    book2 = OrderBook(token_id="tokA", asks=[OrderBookLevel(0.50, 100)])
    signals = engine.scan_activity({"tokA": book2})
    assert len(signals) == 1
    assert signals[0].market_price == pytest.approx(0.50)  # 用 ask 成交


# ---------- 活动年龄与动态滑点 ----------

def test_activity_age_filter_stale_skipped():
    """超过 max_age_seconds 的旧活动不跟（信息已消化）。"""
    api = FakeActivityApi({"0xpro": [act(timestamp=int(time.time()) - 700)]})
    engine = _activity_engine(api, max_age_seconds=600)
    assert engine.scan_activity() == []


def test_activity_age_filter_recent_kept():
    api = FakeActivityApi({"0xpro": [act(timestamp=int(time.time()) - 120)]})
    engine = _activity_engine(api, max_age_seconds=600)
    assert len(engine.scan_activity()) == 1


def test_activity_missing_timestamp_not_filtered():
    """无时间戳的活动不过滤（避免误杀索引延迟条目）。"""
    api = FakeActivityApi({"0xpro": [act(timestamp=None)]})
    engine = _activity_engine(api, max_age_seconds=600)
    assert len(engine.scan_activity()) == 1


def test_dynamic_slippage_widens_with_age():
    """5 分钟前的活动滑点容忍放宽：0.03 + 5min×0.01 = 0.08。"""
    api = FakeActivityApi({"0xpro": [act(price="0.49",
                                         timestamp=int(time.time()) - 300)]})
    engine = _activity_engine(api, max_slippage=0.03, slippage_per_min=0.01)
    book = OrderBook(token_id="tokA", asks=[OrderBookLevel(0.52, 100)])
    # 0.52/0.49-1 = 6.1% < 8%（固定 3% 会被挡，动态放行）
    signals = engine.scan_activity({"tokA": book})
    assert len(signals) == 1
    # 更贵的 ask 0.55 → 12.2% > 8% 仍被挡
    book2 = OrderBook(token_id="tokA", asks=[OrderBookLevel(0.55, 100)])
    assert engine.scan_activity({"tokA": book2}) == []


def test_dynamic_slippage_cap():
    """动态容忍封顶：10 分钟前活动允许 = 0.03 + 10×0.01 = 0.13（cap 0.15 内）。"""
    engine = _activity_engine(FakeActivityApi({}), max_slippage=0.03,
                              slippage_per_min=0.01, slippage_cap=0.15)
    old = act(timestamp=int(time.time()) - 600)
    allowed = engine._allowed_slippage(old)
    assert allowed == pytest.approx(0.03 + 0.10, abs=0.001)
    # 30 分钟前 → 0.03+0.30 但封顶 0.15
    very_old = act(timestamp=int(time.time()) - 1800)
    assert engine._allowed_slippage(very_old) == pytest.approx(0.15, abs=0.001)


def test_activity_trade_id_prefers_hash():
    a = act()
    assert _trade_id(a) == "transactionHash:0xhash1"
    b = dict(a)
    del b["transactionHash"]
    assert _trade_id(b) != "transactionHash:0xhash1"  # 降级指纹


# ---------- 套利/冲单过滤 ----------

def test_wash_filter_skips_sell_roundtrip():
    """同钱包同市场先 SELL 后 BUY → 冲单/往返，BUY 被过滤。"""
    api = FakeActivityApi({"0xpro": [
        act(side="SELL", transactionHash="0xsell", conditionId="0xcondA",
            outcomeIndex=1, outcome="No"),
        act(transactionHash="0xbuy", conditionId="0xcondA"),
    ]})
    engine = _activity_engine(api)
    assert engine.scan_activity() == []


def test_wash_filter_skips_both_side_buy():
    """同钱包同市场 BUY YES + BUY NO → 二元套利，第二个 BUY 被过滤。"""
    api = FakeActivityApi({"0xpro": [
        act(transactionHash="0xbuy1", conditionId="0xcondB", outcomeIndex=0,
            outcome="Yes"),
        act(transactionHash="0xbuy2", conditionId="0xcondB", outcomeIndex=1,
            outcome="No"),
    ]})
    engine = _activity_engine(api)
    assert engine.scan_activity() == []  # buy2 是反向侧被过滤


def test_wash_filter_allows_normal_same_side_buys():
    """同市场同侧连续 BUY（加仓）不是套利，均保留。"""
    api = FakeActivityApi({"0xpro": [
        act(transactionHash="0xbuy1", conditionId="0xcondC", outcomeIndex=0),
        act(transactionHash="0xbuy2", conditionId="0xcondC", outcomeIndex=0),
    ]})
    engine = _activity_engine(api)
    signals = engine.scan_activity()
    assert len(signals) == 2  # 加仓是方向性行为，两笔都正常


def test_wash_filter_cross_market_not_affected():
    """不同市场的买卖互不影响。"""
    api = FakeActivityApi({"0xpro": [
        act(side="SELL", transactionHash="0xsell", conditionId="0xcondX",
            outcomeIndex=1),
        act(transactionHash="0xbuy", conditionId="0xcondY", outcomeIndex=0),
    ]})
    engine = _activity_engine(api)
    assert len(engine.scan_activity()) == 1


def test_wash_filter_expiry_after_window():
    """时间窗过期后同市场可再次跟随。"""
    import time as _t
    old_sell = act(side="SELL", transactionHash="0xold",
                   conditionId="0xcondZ", outcomeIndex=1)
    old_sell["timestamp"] = int(_t.time()) - 3600  # 1 小时前（超出 1800s 窗）
    api = FakeActivityApi({"0xpro": [old_sell,
                                     act(transactionHash="0xbuy",
                                         conditionId="0xcondZ")]})
    engine = _activity_engine(api, wash_window_s=1800)
    assert len(engine.scan_activity()) == 1  # 过期 SELL 不影响


def test_wash_filter_disabled():
    """关闭过滤时套利订单不被拦截。"""
    api = FakeActivityApi({"0xpro": [
        act(side="SELL", transactionHash="0xsell", conditionId="0xcondW",
            outcomeIndex=1, outcome="No"),
        act(transactionHash="0xbuy", conditionId="0xcondW", outcomeIndex=0,
            outcome="Yes"),
    ]})
    engine = _activity_engine(api, wash_filter=False)
    assert len(engine.scan_activity()) == 1


# ---------- 排行榜源目标资格（放宽交易数/活跃检查）----------

def test_leaderboard_target_qualification_no_trades_ok():
    """排行榜源 total_trades=0 + 无 recent_activity 也能合格（跳过两项检查）。"""
    from polytrader.copytrade.leaderboard import SeedProvider
    profile = WalletProfile(address="0xlb", realized_profit_usd=9000,
                            source="leaderboard", total_trades=0,
                            recent_activity=[])
    api = FakeActivityApi({})
    engine = MirrorEngine(SeedProvider([profile]), api,
                          min_profit_usd=5000, min_trades=30,
                          require_activity=False)
    assert engine.refresh_targets() == ["0xlb"]


def test_leaderboard_target_profit_gate():
    from polytrader.copytrade.leaderboard import SeedProvider
    profile = WalletProfile(address="0xlp", realized_profit_usd=100,
                            source="leaderboard")
    api = FakeActivityApi({})
    engine = MirrorEngine(SeedProvider([profile]), api,
                          min_profit_usd=5000, min_trades=0,
                          require_activity=False)
    assert engine.refresh_targets() == []
