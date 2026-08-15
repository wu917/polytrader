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
    # BUY $1 @ 0.535 → 价格 tick 取整 0.54：付 $1.0044，得 1.86 shares（2 位）
    m, t = order_v2.calc_amounts(order_v2.BUY, 1.0, 0.535)
    assert (m, t) == (1_004_400, 1_860_000)
    # SELL 2 shares @ 0.5 → 卖 2 shares，收 $1
    m, t = order_v2.calc_amounts(order_v2.SELL, 2.0, 0.5)
    assert (m, t) == (2_000_000, 1_000_000)


def test_calc_amounts_marketable_tick_variants():
    """marketable 份额精度随 tick 变化（官方 ROUNDING_CONFIG 对齐）。

    2026-08-15 实测 bug：tick=0.001 市场（dota2/lol 事件盘）份额需 5 位，
    硬编码 4 位导致隐含价偏离网格 → CLOB 验签 hash 不匹配
    （invalid POLY_1271 signature）。
    """
    # tick=0.01：4 位份额（原行为不变）
    m, t = order_v2.calc_amounts(order_v2.BUY, 1.0, 0.76, marketable=True,
                                 tick_size=0.01)
    assert t == 1_315_700  # 1.3157（4 位）
    # tick=0.001：5 位份额（官方 roundDown 5 位 = 1.31578）
    m, t = order_v2.calc_amounts(order_v2.BUY, 1.0, 0.76, marketable=True,
                                 tick_size=0.001)
    assert t == 1_315_780  # 1.31578（5 位）


def test_calc_amounts_marketable_implied_price_on_tick():
    """marketable 隐含价受控：tick=0.001 严格落网格，tick=0.01 偏离远小于半 tick。"""
    for tick in (0.001, 0.01):
        for px in (0.45, 0.60, 0.76, 0.89):
            m, t = order_v2.calc_amounts(order_v2.BUY, 1.0, px,
                                         marketable=True, tick_size=tick)
            implied = (m / 1e6) / (t / 1e6)
            # 隐含价必须落在该 tick 的最近网格附近（偏离 ≤ 1e-4，远小于半 tick）
            ticks = round(implied / tick)
            assert abs(implied - ticks * tick) < 1e-4, \
                f"tick={tick} px={px}: implied={implied:.6f} 偏离网格 {abs(implied - ticks*tick):.6f}"
            if tick == 0.001:
                # tick=0.001 市场（lol/dota2 事件盘）需 5 位份额精度
                assert t % 10 == 0 or (t / 1e6) * 1e5 % 1 == 0, \
                    f"tick={tick} px={px}: taker={t} 非 5 位精度"


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
