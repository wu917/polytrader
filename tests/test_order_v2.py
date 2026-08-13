"""order_v2 单测：V2 订单构建 + ERC-7739 签名（与官方 SDK 交叉验证）。"""
import pytest

from polytrader.execution import order_v2

SK = "0x" + "22" * 32
DEPOSIT = "0x1111111111111111111111111111111111111111"   # 测试用地址
EXCH = order_v2.CTF_EXCHANGE_V2
TOKEN = str(int("0x" + "ab" * 32, 16))


def _td(**kw):
    base = dict(
        maker=DEPOSIT, signer=DEPOSIT, token_id=TOKEN,
        maker_amount=1_000_000, taker_amount=2_000_000,
        side=order_v2.BUY, signature_type=order_v2.POLY_1271,
        timestamp_ms="1786000000000", contract=EXCH,
    )
    base.update(kw)
    return order_v2.build_order_typed_data(**base)


def test_calc_amounts():
    # BUY $1 @ 0.5 → 付 $1，得 2 shares
    m, t = order_v2.calc_amounts(order_v2.BUY, 1.0, 0.5)
    assert (m, t) == (1_000_000, 2_000_000)
    # BUY $1 @ 0.535 → 付 $1.00，得 1.8691 shares（4 位）
    m, t = order_v2.calc_amounts(order_v2.BUY, 1.0, 0.535)
    assert (m, t) == (1_000_000, 1_869_100)
    # SELL 2 shares @ 0.5 → 卖 2 shares，收 $1
    m, t = order_v2.calc_amounts(order_v2.SELL, 2.0, 0.5)
    assert (m, t) == (2_000_000, 1_000_000)


def test_build_order_typed_data():
    td = _td()
    assert td["domain"]["version"] == "2"
    assert td["domain"]["verifyingContract"] == EXCH
    assert td["message"]["signatureType"] == order_v2.POLY_1271
    assert td["message"]["maker"] == DEPOSIT
    assert td["message"]["signer"] == DEPOSIT


def test_poly1271_signature_length():
    td = _td()
    sig = order_v2.sign_order_poly1271(td, SK, EXCH)
    assert sig.startswith("0x") and len(sig) == 636


def test_signature_deterministic_with_salt():
    td1 = _td(salt="12345")
    td2 = _td(salt="12345")
    assert order_v2.sign_order_poly1271(td1, SK, EXCH) == \
        order_v2.sign_order_poly1271(td2, SK, EXCH)
    td3 = _td(salt="99999")
    assert order_v2.sign_order_poly1271(td1, SK, EXCH) != \
        order_v2.sign_order_poly1271(td3, SK, EXCH)


def test_ecdsa_signature():
    td = _td(signature_type=order_v2.EOA, maker=DEPOSIT)
    sig = order_v2.sign_order_ecdsa(td, SK)
    assert sig.startswith("0x") and len(sig) == 132


def test_order_to_json_v2():
    td = _td()
    sig = order_v2.sign_order_poly1271(td, SK, EXCH)
    order = {**td["message"], "signature": sig}
    payload = order_v2.order_to_json_v2(order, owner="uuid-key", order_type="GTC")
    assert payload["owner"] == "uuid-key"
    assert payload["orderType"] == "GTC"
    o = payload["order"]
    assert o["side"] == "BUY"
    assert o["signatureType"] == 3
    assert o["signature"] == sig
    assert o["maker"] == DEPOSIT


def test_cross_validate_with_official_sdk():
    """与官方 py-clob-client-v2 生成的签名逐字节对比（若 SDK 可导入）。"""
    sdk = pytest.importorskip("py_clob_client_v2")
    from py_clob_client_v2.order_utils.exchange_order_builder_v2 import (
        ExchangeOrderBuilderV2)
    from py_clob_client_v2.order_utils.model.order_data_v2 import OrderDataV2
    from py_clob_client_v2.order_utils.model.signature_type_v2 import (
        SignatureTypeV2)
    from py_clob_client_v2.signer import Signer

    ts = "1786000000000"
    sdk_order = ExchangeOrderBuilderV2(contract_address=EXCH, chain_id=137,
                                       signer=Signer(SK, 137)).build_signed_order(
        OrderDataV2(maker=DEPOSIT, tokenId=TOKEN,
                    makerAmount=str(1_000_000), takerAmount=str(2_000_000),
                    side=0, signatureType=SignatureTypeV2.POLY_1271,
                    timestamp=ts))
    td = _td(salt=str(sdk_order.salt), timestamp_ms=ts)
    assert order_v2.sign_order_poly1271(td, SK, EXCH) == sdk_order.signature
