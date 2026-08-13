"""Polymarket CLOB 签名与认证：EIP-712 订单签名、L1/L2 API 凭证、认证头。

协议要点（Polygon mainnet，chainId=137）：
- 订单用 EIP-712 结构化数据签名（Domain: Polymarket CTF Exchange v1）
- L2 认证头：POLYMARKET-API-KEY / SIGNATURE / TIMESTAMP / PASSPHRASE，
  signature = L1 消息签名(keccak(hex(ms_timestamp) + "\\x00" + hex(api_secret)))
- API 凭证可从私钥 L1 派生（用户已提供三元组时直接使用）

⚠️ 未经 mainnet 实盘验证：上线前必须在 Polymarket 测试网全链路验证。
"""
from __future__ import annotations

import time

from eth_account import Account
from eth_account.messages import encode_defunct, encode_typed_data

# Polygon mainnet 合约（代码内置，无需配置）
CHAIN_ID = 137
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"   # CTF Exchange
NEG_RISK_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
USDC_ADDRESS = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"

# 派生 API 凭证用的固定消息（与官方 clob-client 一致）
_DERIVE_MESSAGE = "This is a signature for a Polymarket API key"

_ORDER_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ],
    "Order": [
        {"name": "maker", "type": "address"},
        {"name": "taker", "type": "address"},
        {"name": "tokenId", "type": "uint256"},
        {"name": "makerAmount", "type": "uint256"},
        {"name": "takerAmount", "type": "uint256"},
        {"name": "id", "type": "uint256"},
        {"name": "feeRateBps", "type": "uint256"},
        {"name": "nonce", "type": "uint256"},
        {"name": "expiration", "type": "uint256"},
    ],
}

_ORDER_DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": CHAIN_ID,
    "verifyingContract": EXCHANGE_ADDRESS,
}


def sign_typed_order(order: dict, private_key: str) -> str:
    """对 CLOB 订单做 EIP-712 签名，返回 0x 签名。

    order 字段：maker/taker/tokenId/makerAmount/takerAmount/id/feeRateBps/nonce/expiration
    （deposit wallet 模式下额外含 funder；signatureType 由请求体携带，不进 EIP-712 message）
    """
    full = {
        "types": _ORDER_TYPES,
        "primaryType": "Order",
        "domain": _ORDER_DOMAIN,
        "message": order,
    }
    msg = encode_typed_data(full_message=full)
    sig = Account.sign_message(msg, private_key)
    sig_hex = sig.signature.hex()
    return sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex


# ---- ClobAuth（现代 CLOB V2 的 L1 认证签名，用于派生 L2 API 凭证）----
_CLOB_AUTH_TYPES = {
    "EIP712Domain": [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
    ],
    "ClobAuth": [
        {"name": "address", "type": "address"},
        {"name": "timestamp", "type": "string"},
        {"name": "nonce", "type": "uint256"},
        {"name": "message", "type": "string"},
    ],
}

_CLOB_AUTH_DOMAIN = {"name": "ClobAuthDomain", "version": "1", "chainId": CHAIN_ID}
_CLOB_AUTH_MESSAGE = "This message attests that I control the given wallet"


def sign_clob_auth(address: str, private_key: str,
                   timestamp: int | None = None, nonce: int | None = None) -> str:
    """ClobAuth L1 签名（EIP-712 ClobAuthDomain），用于派生/创建 L2 API 凭证。"""
    ts = timestamp if timestamp is not None else int(time.time())
    n = nonce if nonce is not None else int(time.time() * 1000) % (2 ** 63)
    full = {
        "types": _CLOB_AUTH_TYPES,
        "primaryType": "ClobAuth",
        "domain": _CLOB_AUTH_DOMAIN,
        "message": {
            "address": address,
            "timestamp": str(ts),
            "nonce": n,
            "message": _CLOB_AUTH_MESSAGE,
        },
    }
    msg = encode_typed_data(full_message=full)
    sig = Account.sign_message(msg, private_key)
    sig_hex = sig.signature.hex()
    return sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex


def build_order_with_funder(order: dict, private_key: str,
                            funder: str | None = None) -> dict:
    """构建可提交订单；deposit wallet 模式下 funder 与 maker 不同。

    signatureType 由调用方在请求体携带（3=deposit wallet），不参与 EIP-712 签名。
    """
    signed = dict(order)
    if funder:
        signed["funder"] = funder
    signed["signature"] = sign_typed_order(order, private_key)
    return signed


# ---- CLOB V2 L2 认证（新协议：POLY_* 头 + HMAC-SHA256）----
def derive_api_key_headers(address: str, signature: str,
                           timestamp: int, nonce: int = 0) -> dict:
    """GET /auth/derive-api-key 的请求头（POLY_* 4 头）。"""
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": signature,
        "POLY_TIMESTAMP": str(timestamp),
        "POLY_NONCE": str(nonce),
    }


def l2_headers_new(address: str, api_key: str, passphrase: str,
                   secret_b64: str, method: str, path: str,
                   body: str = "") -> dict:
    """新版 L2 认证头（5 个 POLY_* 头）。

    message = timestamp + METHOD + path + body
    signature = urlsafeBase64WithPadding(HMAC-SHA256(base64decode(secret), message))
    """
    import base64
    import hashlib
    import hmac
    ts = int(time.time())
    message = f"{ts}{method.upper()}{path}{body}"
    key = base64.urlsafe_b64decode(secret_b64)   # secret 为 urlsafe base64
    digest = hmac.new(key, message.encode(), hashlib.sha256).digest()
    sig = base64.urlsafe_b64encode(digest).decode()
    return {
        "POLY_ADDRESS": address,
        "POLY_SIGNATURE": sig,
        "POLY_TIMESTAMP": str(ts),
        "POLY_API_KEY": api_key,
        "POLY_PASSPHRASE": passphrase,
    }


def derive_api_credentials(private_key: str) -> dict:
    """从私钥派生 API 凭证三元组（与官方 clob-client 一致）。

    返回 {"api_key": "pk_...", "api_secret": "0x...", "api_passphrase": "poly"}
    """
    acct = Account.from_key(private_key)
    sig = Account.sign_message(encode_defunct(text=_DERIVE_MESSAGE), acct.key)
    sig_hex = sig.signature.hex()          # 兼容带/不带 0x 前缀
    sig_hex = sig_hex[2:] if sig_hex.startswith("0x") else sig_hex
    return {
        "api_key": "pk_" + sig_hex[:40],   # 签名 hex 前 40 字符
        "api_secret": "0x" + sig_hex,      # 统一带 0x 存储
        "api_passphrase": "poly",
    }


def l2_auth_headers(api_key: str, api_secret: str,
                    api_passphrase: str, private_key: str) -> dict:
    """构建 CLOB L2 认证头（每次请求重新签名，时间戳毫秒）。"""
    ts_ms = int(time.time() * 1000)
    ts_hex = hex(ts_ms)[2:]
    secret_hex = api_secret[2:] if api_secret.startswith("0x") else api_secret
    sig = Account.sign_message(encode_defunct(hexstr=ts_hex + "00" + secret_hex),
                               private_key)
    sig_hex = sig.signature.hex()
    return {
        "POLYMARKET-API-KEY": api_key,
        "POLYMARKET-SIGNATURE": sig_hex if sig_hex.startswith("0x") else "0x" + sig_hex,
        "POLYMARKET-TIMESTAMP": ts_hex,
        "POLYMARKET-PASSPHRASE": api_passphrase,
    }


def build_order(order: dict, private_key: str) -> dict:
    """订单字典 + EIP-712 签名 → 可提交 CLOB 的完整订单。"""
    signed = dict(order)
    signed["signature"] = sign_typed_order(order, private_key)
    return signed


def usd_to_maker_amount(usd: float) -> int:
    """USD → makerAmount（USDC 6 位小数）。"""
    return int(round(usd * 1_000_000))


def shares_to_taker_amount(shares: float) -> int:
    """份额 → takerAmount（6 位小数）。"""
    return int(round(shares * 1_000_000))


def asset_id(token_id: str) -> str:
    """规范化 CLOB assetId：0x + 64 hex（Polymarket token_id 即 assetId）。"""
    t = token_id.lower()
    t = t[2:] if t.startswith("0x") else t
    if len(t) != 64:
        raise ValueError(f"token_id must be 32-byte hex, got: {token_id}")
    return "0x" + t
