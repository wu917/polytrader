"""CLOB 客户端测试（mock HTTP）。"""
from polytrader.data.clob_client import ClobClient, _to_level
from polytrader.models import OrderBookLevel


class FakeHttp:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url, params=None, **kwargs):
        return self._payload


BOOK_JSON = {
    "bids": [{"price": "0.52", "size": "100.5"}, {"price": "0.51", "size": "200"},
             {"price": "0.00", "size": "999"}],
    "asks": [{"price": "0.53", "size": "80"}, {"price": "0.54", "size": "150"}],
    "timestamp": 1700000000000,
}


def test_parse_book_object_levels():
    client = ClobClient(http=FakeHttp(BOOK_JSON))
    book = client.get_book("tok1")
    assert book is not None
    assert book.bids[0] == OrderBookLevel(price=0.52, size=100.5)
    assert len(book.bids) == 2      # 零价档被过滤
    assert len(book.asks) == 2
    assert book.best_bid().price == 0.52
    assert book.best_ask().price == 0.53
    assert abs(book.mid_price() - 0.525) < 1e-9
    assert book.depth_usd(2) > 0


def test_parse_book_array_levels():
    """旧版/WS 格式兼容。"""
    client = ClobClient(http=FakeHttp({
        "bids": [["0.52", "100.5"]],
        "asks": [["0.53", "80"]],
    }))
    book = client.get_book("tok1")
    assert book is not None
    assert book.bids[0] == OrderBookLevel(0.52, 100.5)


def test_to_level_variants():
    assert _to_level(["0.5", "100"]) == OrderBookLevel(0.5, 100.0)
    assert _to_level([0.5, 100]) == OrderBookLevel(0.5, 100.0)
    assert _to_level(["bad", "100"]) is None
    assert _to_level(None) is None
    assert _to_level(["0.5"]) is None


def test_midpoint():
    client = ClobClient(http=FakeHttp({"mid": "0.525"}))
    assert client.get_midpoint("tok1") == 0.525
    client2 = ClobClient(http=FakeHttp({"mid": None}))
    assert client2.get_midpoint("tok1") is None
