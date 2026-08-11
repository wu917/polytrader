"""Gamma 客户端解析测试（mock HTTP）。"""
from polytrader.data.gamma_client import GammaClient


class FakeHttp:
    def __init__(self, payload):
        self._payload = payload

    def get_json(self, url, params=None, **kwargs):
        return self._payload


MARKET_JSON = {
    "conditionId": "0xcond123",
    "question": "Will BTC be above $100k in 2025?",
    "slug": "btc-100k-2025",
    "category": "Crypto",
    "description": "A test market",
    "endDate": "2025-12-31T23:59:59Z",
    "liquidity": "12345.67",
    "volume24hr": "500",
    "closed": False,
    "active": True,
    # 现行 Gamma 格式：名称数组 + 价格数组 + token 数组（顺序对应）
    "outcomes": ["Yes", "No"],
    "outcomePrices": ["0.52", "0.48"],
    "clobTokenIds": ["tok1", "tok2"],
}

# 旧格式兼容性 fixture
LEGACY_MARKET_JSON = {
    "conditionId": "0xcond999",
    "question": "Legacy format market?",
    "slug": "legacy-market",
    "outcomes": [
        {"id": "out1", "token_id": "ltok1", "price": "0.55", "name": "Yes"},
        {"id": "out2", "token_id": "ltok2", "price": "0.45", "name": "No"},
    ],
}


def test_parse_market_json():
    client = GammaClient(http=FakeHttp([MARKET_JSON]))
    markets = client.get_markets()
    assert len(markets) == 1
    m = markets[0]
    assert m.condition_id == "0xcond123"
    assert m.question == "Will BTC be above $100k in 2025?"
    assert m.is_binary
    assert m.liquidity == 12345.67
    assert m.volume == 500.0
    assert m.outcomes[0].token_id == "tok1"
    assert m.outcomes[0].price == "0.52"


def test_parse_single_market():
    client = GammaClient(http=FakeHttp(MARKET_JSON))
    m = client.get_market("0xcond123")
    assert m is not None
    assert m.slug == "btc-100k-2025"


def test_missing_fields_are_safe():
    client = GammaClient(http=FakeHttp([{}]))
    markets = client.get_markets()
    assert len(markets) == 1
    assert markets[0].condition_id == ""
    assert markets[0].liquidity == 0.0
    assert markets[0].is_binary is False


def test_iter_markets_pagination():
    client = GammaClient(http=FakeHttp([MARKET_JSON, MARKET_JSON]))
    got = list(client.iter_markets(batch=100, max_markets=300))
    # 每批返回 2 个（payload 恒为 2 项），共 2 批后空批停止
    assert len(got) >= 2


def test_legacy_outcomes_object_format():
    client = GammaClient(http=FakeHttp([LEGACY_MARKET_JSON]))
    markets = client.get_markets()
    assert markets[0].outcomes[0].name == "Yes"
    assert markets[0].outcomes[0].token_id == "ltok1"
    assert markets[0].is_binary is True


def test_json_encoded_string_arrays():
    """Gamma 实际把数组字段编码为 JSON 字符串，必须兼容。"""
    payload = dict(MARKET_JSON)
    payload["outcomes"] = '["Yes", "No"]'
    payload["outcomePrices"] = '["0.52", "0.48"]'
    payload["clobTokenIds"] = '["tok1", "tok2"]'
    client = GammaClient(http=FakeHttp([payload]))
    markets = client.get_markets()
    assert markets[0].is_binary
    assert [o.name for o in markets[0].outcomes] == ["Yes", "No"]
    assert [o.token_id for o in markets[0].outcomes] == ["tok1", "tok2"]
    assert markets[0].outcomes[0].price == "0.52"


def test_invalid_json_string_arrays_are_safe():
    payload = dict(MARKET_JSON)
    payload["outcomes"] = "not-json"
    client = GammaClient(http=FakeHttp([payload]))
    markets = client.get_markets()
    assert markets[0].outcomes == []
