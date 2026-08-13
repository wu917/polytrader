"""signer.py 单测：EIP-712 订单签名、凭证派生、L2 认证头、资产转换。"""
import time

from polytrader.execution import signer

TEST_KEY = "0x" + "11" * 32


def test_asset_id():
    assert signer.asset_id("0x" + "ab" * 32) == "0x" + "ab" * 32
    assert signer.asset_id("ab" * 32) == "0x" + "ab" * 32
    try:
        signer.asset_id("0x123")
        assert False, "should reject short id"
    except ValueError:
        pass


def test_derive_api_credentials():
    cred = signer.derive_api_credentials(TEST_KEY)
    assert cred["api_key"].startswith("pk_") and len(cred["api_key"]) == 43
    assert cred["api_secret"].startswith("0x") and len(cred["api_secret"]) == 132
    assert cred["api_passphrase"] == "poly"
    # 确定性：同一私钥派生结果一致
    assert signer.derive_api_credentials(TEST_KEY) == cred


def test_sign_typed_order():
    acct = signer.Account.from_key(TEST_KEY)
    order = {
        "maker": acct.address,
        "taker": signer.ZERO_ADDRESS,
        "tokenId": int(signer.asset_id("0x" + "ab" * 32), 16),
        "makerAmount": signer.usd_to_maker_amount(1.0),
        "takerAmount": signer.shares_to_taker_amount(2.0),
        "id": 12345,
        "feeRateBps": 0,
        "nonce": 7,
        "expiration": int(time.time()) + 3600,
    }
    signed = signer.build_order(order, TEST_KEY)
    assert signed["signature"].startswith("0x")
    assert len(signed["signature"]) == 132  # 0x + 130 hex（65 字节 r/s/v）


def test_l2_auth_headers():
    cred = signer.derive_api_credentials(TEST_KEY)
    h = signer.l2_auth_headers(cred["api_key"], cred["api_secret"],
                               cred["api_passphrase"], TEST_KEY)
    assert set(h) == {"POLYMARKET-API-KEY", "POLYMARKET-SIGNATURE",
                      "POLYMARKET-TIMESTAMP", "POLYMARKET-PASSPHRASE"}
    assert h["POLYMARKET-SIGNATURE"].startswith("0x")
    assert h["POLYMARKET-API-KEY"] == cred["api_key"]


def test_amount_conversions():
    assert signer.usd_to_maker_amount(1.0) == 1_000_000
    assert signer.usd_to_maker_amount(0.5) == 500_000
    assert signer.shares_to_taker_amount(1.5) == 1_500_000
