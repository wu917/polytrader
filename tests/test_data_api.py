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
    rows = client.price_history("0xcond", interval="1h")
    assert len(rows) == 2
    assert rows[0]["p"] == 0.50
    assert client.http.last_params["fidelity"] == 3600
    assert "market" in client.http.last_params


def test_price_now_take_last_close():
    client = DataApiClient(http=FakeHttp(HISTORY))
    assert client.price_now("0xcond") == 0.53


def test_price_now_empty():
    client = DataApiClient(http=FakeHttp({"history": []}))
    assert client.price_now("0xcond") is None


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
