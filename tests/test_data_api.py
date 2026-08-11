"""data-api 客户端测试（mock HTTP）。"""
from polytrader.data.data_api import DataApiClient, _interval_to_fidelity


class FakeHttp:
    def __init__(self, payload):
        self._payload = payload
        self.last_params = None

    def get_json(self, url, params=None, **kwargs):
        self.last_params = params or {}
        return self._payload


HISTORY = {"history": [
    {"t": 1700000000, "p": 0.50},
    {"t": 1700000360, "p": 0.53},
]}

TRADES = [
    {"proxyWallet": "0xaaa", "side": "BUY", "asset": "tok1",
     "conditionId": "0xcond", "size": 100.0, "price": 0.52, "timestamp": 1700000000},
]


def test_price_history_parsing():
    client = DataApiClient(http=FakeHttp(HISTORY))
    rows = client.price_history("tok1", interval="1h")
    assert len(rows) == 2
    assert rows[0]["p"] == 0.50
    assert client.http.last_params["fidelity"] == 3600
    assert client.http.last_params["market"] == "tok1"


def test_price_now_take_last_close():
    client = DataApiClient(http=FakeHttp(HISTORY))
    assert client.price_now("tok1") == 0.53


def test_price_now_empty():
    client = DataApiClient(http=FakeHttp({"history": []}))
    assert client.price_now("tok1") is None


def test_interval_fidelity_mapping():
    assert _interval_to_fidelity("1m") == 60
    assert _interval_to_fidelity("1h") == 3600
    assert _interval_to_fidelity("1d") == 86400
    assert _interval_to_fidelity("max") is None
    assert _interval_to_fidelity("bogus") is None


def test_get_trades():
    client = DataApiClient(http=FakeHttp(TRADES))
    trades = client.get_trades("0xcond", limit=10)
    assert len(trades) == 1
    assert trades[0]["proxyWallet"] == "0xaaa"
    assert trades[0]["side"] == "BUY"


def test_get_user_trades():
    client = DataApiClient(http=FakeHttp(TRADES))
    trades = client.get_user_trades("0xaaa")
    assert len(trades) == 1
    assert client.http.last_params["user"] == "0xaaa"


def test_trades_to_history_buckets():
    """成交记录按 1h 桶聚合为价格序列。"""
    client = DataApiClient(http=FakeHttp([]))
    trades = [
        {"asset": "tokA", "timestamp": 1700000000, "price": 0.50},
        {"asset": "tokA", "timestamp": 1700001800, "price": 0.52},   # 同桶 → 覆盖
        {"asset": "tokA", "timestamp": 1700007200, "price": 0.54},   # 新桶
        {"asset": "tokB", "timestamp": 1700000000, "price": 0.99},   # 其他 token → 过滤
        {"asset": "tokA", "timestamp": "bad", "price": 0.5},         # 坏行 → 跳过
    ]
    hist = client.trades_to_history(trades, "tokA")
    assert len(hist) == 2
    assert hist[0]["t"] == 1700001800 and hist[0]["p"] == 0.52
    assert hist[1]["t"] == 1700007200 and hist[1]["p"] == 0.54


def test_market_trade_history():
    client = DataApiClient(http=FakeHttp(TRADES))
    hist = client.market_trade_history("0xcond", "tok1")
    assert len(hist) == 1
    assert hist[0]["p"] == 0.52


def test_timestamps_converted_to_ms():
    """历史价格窗口按秒传递（CLOB prices-history 用秒）。"""
    client = DataApiClient(http=FakeHttp(HISTORY))
    client.price_history("tok1", interval="1h", start_ts=1700000000.5, end_ts=1700003600.7)
    assert client.http.last_params["startTs"] == 1700000000   # 浮点被截断为整数
    assert client.http.last_params["endTs"] == 1700003600
