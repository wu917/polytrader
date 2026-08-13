"""relayer.py 单测：认证头、WALLET-CREATE 提交、交易轮询、终态处理。"""
import pytest

from polytrader.execution import relayer
from polytrader.execution.relayer import RelayerClient

KEY = "test-relayer-key-00000000-0000-0000-0000-000000000000"
ADDR = "0xcEEf18EB50F16aCCdf7132669eF78365ca6b07c0"


class _FakeHttp:
    def __init__(self, responses=None):
        self.responses = responses or []
        self.calls = []

    def post_json(self, url, json_body=None, headers=None, **kw):
        self.calls.append(("POST", url, json_body, headers))
        return self.responses.pop(0) if self.responses else {"transactionID": "tx1"}

    def get_json(self, url, params=None, headers=None, **kw):
        self.calls.append(("GET", url, params, headers))
        return self.responses.pop(0) if self.responses else [{"state": "STATE_CONFIRMED"}]


def test_auth_headers():
    http = _FakeHttp()
    c = RelayerClient(KEY, ADDR, http=http)
    h = c._headers()
    assert h["RELAYER_API_KEY"] == KEY
    assert h["RELAYER_API_KEY_ADDRESS"] == ADDR


def test_auth_required():
    c = RelayerClient("", "")
    with pytest.raises(RuntimeError):
        c._headers()


def test_wallet_create_body():
    http = _FakeHttp()
    c = RelayerClient(KEY, ADDR, http=http)
    c.wallet_create(signer=ADDR)
    method, url, body, headers = http.calls[0]
    assert method == "POST" and url.endswith("/submit")
    assert body["type"] == "WALLET-CREATE"
    assert body["from"] == ADDR
    assert body["to"] == relayer.DEPOSIT_WALLET_FACTORY


def test_get_transaction_normalizes():
    http = _FakeHttp([{"transactions": [{"state": "STATE_NEW", "transactionID": "t1"}]}])
    c = RelayerClient(KEY, ADDR, http=http)
    rows = c.get_transaction("t1")
    assert rows[0]["transactionID"] == "t1"


def test_wait_transaction_confirmed():
    http = _FakeHttp([[{"state": "STATE_CONFIRMED", "proxyAddress": "0xabc"}]])
    c = RelayerClient(KEY, ADDR, http=http)
    row = c.wait_transaction("t1", poll=0)
    assert row["proxyAddress"] == "0xabc"


def test_wait_transaction_failed():
    http = _FakeHttp([[{"state": "STATE_FAILED"}]])
    c = RelayerClient(KEY, ADDR, http=http)
    with pytest.raises(RuntimeError, match="STATE_FAILED"):
        c.wait_transaction("t1", poll=0)


def test_execute_body():
    http = _FakeHttp()
    c = RelayerClient(KEY, ADDR, http=http)
    tx = [{"to": "0xcontract", "data": "0x1234", "value": "0"}]
    c.execute(tx, metadata="approve")
    method, url, body, headers = http.calls[0]
    assert body["type"] == "EXECUTE"
    assert body["transactions"] == tx
    assert body["from"] == ADDR
