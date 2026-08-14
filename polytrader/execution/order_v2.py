"""Polymarket CLOB V2 订单构建与签名。

V2 与 V1 差异（详见 docs.polymarket.com/trading/deposit-wallets）：
- 11 字段 signed struct（去掉 taker/nonce/feeRateBps；expiration 仅在未签名 payload）
- EIP-712 domain version = "2"，verifyingContract = CTF Exchange V2
- side 序列化为 "BUY"/"SELL"（API payload）
- deposit wallet 下单：signatureType=3 (POLY_1271)，
  签名是 ERC-7739-wrapped（EOA 签嵌套 TypedDataSign，deposit wallet 通过
  ERC-1271 验证），maker = signer = deposit wallet 地址

参考官方 @polymarket/clob-client-v2 / py-clob-client-v2 实现（MIT）。
"""
from __future__ import annotations

import random
import time

from eth_account import Account
from eth_utils import keccak

# ---- 常量（与官方 SDK 逐字节一致）----
ORDER_TYPE_STRING = (
    "Order(uint256 salt,address maker,address signer,uint256 tokenId,"
    "uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,"
    "uint256 timestamp,bytes32 metadata,bytes32 builder)"
)
SOLADY_TYPE_STRING = (
    "TypedDataSign(Order contents,string name,string version,uint256 chainId,"
    "address verifyingContract,bytes32 salt)"
    f"{ORDER_TYPE_STRING}"
)
DOMAIN_TYPE_STRING = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)

ORDER_TYPE_HASH = keccak(text=ORDER_TYPE_STRING)
DOMAIN_TYPE_HASH = keccak(text=DOMAIN_TYPE_STRING)
SOLADY_TYPE_HASH = keccak(text=SOLADY_TYPE_STRING)
DEPOSIT_WALLET_NAME_HASH = keccak(text="DepositWallet")
DEPOSIT_WALLET_VERSION_HASH = keccak(text="1")
CTF_EXCHANGE_NAME_HASH = keccak(text="Polymarket CTF Exchange")
CTF_EXCHANGE_VERSION_HASH = keccak(text="2")
DEPOSIT_WALLET_DOMAIN_SALT = bytes(32)

# Polygon 主网合约（V2）
CTF_EXCHANGE_V2 = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_EXCHANGE_V2 = "0xe2222d279d744050d28e00520010520000310F59"
ZERO_BYTES32 = "0x" + "00" * 32

# WalletType（CLOB V2）
EOA = 0
POLY_PROXY = 1
GNOSIS_SAFE = 2
POLY_1271 = 3  # deposit wallet

# Side
BUY = 0
SELL = 1


def _enc(*args: int | bytes | str) -> bytes:
    """定长 ABI 编码（bytes32/uint256/uint8/address 均为 32B 填充）。"""
    out = b""
    for a in args:
        if isinstance(a, bytes) and len(a) == 32:
            out += a
        elif isinstance(a, int):
            out += a.to_bytes(32, "big")
        elif isinstance(a, str) and a.startswith("0x") and len(a) == 42:
            out += bytes.fromhex(a[2:].zfill(64))
        else:
            raise ValueError(f"unsupported abi arg: {type(a)} {a!r}")
    return out


def _to_bytes32(v: str | bytes) -> bytes:
    if isinstance(v, bytes):
        return v
    return bytes.fromhex(v.replace("0x", "").zfill(64))


def _app_domain_separator(chain_id: int, contract: str) -> bytes:
    return keccak(_enc(DOMAIN_TYPE_HASH, CTF_EXCHANGE_NAME_HASH,
                       CTF_EXCHANGE_VERSION_HASH, chain_id, contract))


def generate_order_salt() -> str:
    return str(int(random.random() * (time.time_ns() // 1_000_000)))


def round_price_tick(price: float, tick_size: float = 0.01) -> float:
    """价格按 tick 取整（tick=0.01 → 2 位小数）。"""
    import decimal
    ticks = int(round(decimal.Decimal(str(price)) / decimal.Decimal(str(tick_size))))
    return round(ticks * tick_size, 4)


def calc_amounts(side: int, size: float, price: float) -> tuple[int, int]:
    """limit order 的 (maker_amount, taker_amount)（6 decimals）。

    遵循官方文档精度规则（tick=0.01 → Price 2 位 / Size 2 位 / Amount 4 位）：
    1. price 按 tick 取整（≤2 位小数）
    2. 份额 round DOWN 到 2 位小数
    3. USD 金额 round UP 到 6 位、再 DOWN 到 4 位（Amount decimals）
    BUY:  maker=USD(price×shares)，taker=shares
    SELL: maker=shares，taker=USD(price×shares)
    """
    from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_CEILING

    def _usd_4(v: Decimal) -> Decimal:
        # Amount 4 位：先 round UP 到 8 位再 DOWN 到 4 位
        return v.quantize(Decimal("0.00000001"),
                          rounding=ROUND_HALF_UP).quantize(Decimal("0.0001"),
                                                           rounding=ROUND_DOWN)

    def _to_6(v: Decimal) -> int:
        return int((v * Decimal(1_000_000)).to_integral_value(rounding=ROUND_DOWN))

    price_d = Decimal(str(round_price_tick(price)))
    if side == BUY:
        # BUY: size = 美元金额；shares = ceil(usd/price, 2 位)（向上取整保证
        # 金额不缩水），usd = shares×price（4 位）→ 隐含价精确 = price。
        usd_in = Decimal(str(size)).quantize(Decimal("0.01"),
                                             rounding=ROUND_HALF_UP)
        shares = (usd_in / price_d).quantize(Decimal("0.01"),
                                             rounding=ROUND_CEILING)
        if shares == 0:
            shares = Decimal("0.01")
        usd = _usd_4(shares * price_d)
        maker, taker = _to_6(usd), _to_6(shares)
    else:
        # SELL: size = 份额数；maker=shares（2 位），taker=USD=shares×price
        shares = Decimal(str(size)).quantize(Decimal("0.01"),
                                             rounding=ROUND_DOWN)
        usd = _usd_4(shares * price_d)
        maker, taker = _to_6(shares), _to_6(usd)
    return maker, taker


def build_order_typed_data(
    *, maker: str, signer: str, token_id: str, maker_amount: int,
    taker_amount: int, side: int, signature_type: int, timestamp_ms: str,
    metadata: str = ZERO_BYTES32, builder: str = ZERO_BYTES32,
    contract: str = CTF_EXCHANGE_V2, chain_id: int = 137,
    salt: str | None = None,
) -> dict:
    """构建 V2 订单的 EIP-712 typed data（未签名）。"""
    return {
        "primaryType": "Order",
        "types": {
            "EIP712Domain": [
                {"name": "name", "type": "string"},
                {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"},
                {"name": "verifyingContract", "type": "address"},
            ],
            "Order": [
                {"name": "salt", "type": "uint256"},
                {"name": "maker", "type": "address"},
                {"name": "signer", "type": "address"},
                {"name": "tokenId", "type": "uint256"},
                {"name": "makerAmount", "type": "uint256"},
                {"name": "takerAmount", "type": "uint256"},
                {"name": "side", "type": "uint8"},
                {"name": "signatureType", "type": "uint8"},
                {"name": "timestamp", "type": "uint256"},
                {"name": "metadata", "type": "bytes32"},
                {"name": "builder", "type": "bytes32"},
            ],
        },
        "domain": {
            "name": "Polymarket CTF Exchange",
            "version": "2",
            "chainId": chain_id,
            "verifyingContract": contract,
        },
        "message": {
            "salt": int(salt or generate_order_salt()),
            "maker": maker,
            "signer": signer,
            "tokenId": int(token_id),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "side": side,
            "signatureType": signature_type,
            "timestamp": int(timestamp_ms),
            "metadata": _to_bytes32(metadata),
            "builder": _to_bytes32(builder),
        },
    }


def sign_order_ecdsa(typed_data: dict, private_key: str) -> str:
    """普通 ECDSA 订单签名（EOA / 非 deposit wallet 场景）。"""
    from eth_account.messages import encode_typed_data
    encoded = encode_typed_data(full_message=typed_data)
    signed = Account.sign_message(encoded, private_key=private_key)
    sig = signed.signature.hex()
    return sig if sig.startswith("0x") else "0x" + sig


def sign_order_poly1271(typed_data: dict, private_key: str,
                        verifying_contract: str = CTF_EXCHANGE_V2,
                        chain_id: int = 137) -> str:
    """ERC-7739-wrapped 签名（deposit wallet / signatureType=3）。

    外层由 EOA（owner）对嵌套 TypedDataSign payload 签名，deposit wallet
    通过 ERC-1271 验证。格式与官方 SDK 逐字节一致。
    """
    m = typed_data["message"]
    contents_hash = keccak(_enc(
        ORDER_TYPE_HASH, int(m["salt"]), m["maker"], m["signer"],
        int(m["tokenId"]), int(m["makerAmount"]), int(m["takerAmount"]),
        int(m["side"]), int(m["signatureType"]), int(m["timestamp"]),
        _to_bytes32(m["metadata"]), _to_bytes32(m["builder"]),
    ))
    typed_data_sign_struct_hash = keccak(_enc(
        SOLADY_TYPE_HASH, contents_hash, DEPOSIT_WALLET_NAME_HASH,
        DEPOSIT_WALLET_VERSION_HASH, chain_id, m["signer"],
        DEPOSIT_WALLET_DOMAIN_SALT,
    ))
    app_sep = _app_domain_separator(chain_id, verifying_contract)
    digest = keccak(b"\x19\x01" + app_sep + typed_data_sign_struct_hash)
    signed = Account._sign_hash(digest, private_key=private_key)
    inner_sig = signed.signature.hex()
    if inner_sig.startswith("0x"):
        inner_sig = inner_sig[2:]
    contents_type = ORDER_TYPE_STRING.encode().hex()
    contents_type_len = len(ORDER_TYPE_STRING).to_bytes(2, "big").hex()
    return ("0x" + inner_sig + app_sep.hex() + contents_hash.hex()
            + contents_type + contents_type_len)


def order_to_json_v2(order: dict, owner: str,
                     order_type: str = "GTC", post_only: bool = False,
                     defer_exec: bool = False) -> dict:
    """组装 POST /order 请求体（owner = L2 api key）。"""
    side_str = "BUY" if int(order["side"]) == BUY else "SELL"
    return {
        "order": {
            "salt": int(order["salt"]),
            "maker": order["maker"],
            "signer": order["signer"],
            "tokenId": str(order["tokenId"]),   # 大整数必须字符串
            "makerAmount": str(order["makerAmount"]),
            "takerAmount": str(order["takerAmount"]),
            "side": side_str,
            "expiration": order.get("expiration", "0"),
            "signatureType": int(order["signatureType"]),
            "timestamp": str(order["timestamp"]),
            "metadata": order["metadata"],
            "builder": order["builder"],
            "signature": order["signature"],
        },
        "owner": owner,
        "orderType": order_type,
        "deferExec": defer_exec,
        "postOnly": post_only,
    }
