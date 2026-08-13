"""Polygon 链上交易：构建/签名/广播（eth_sendRawTransaction）。

用于充值（swap → pUSD → 转入 deposit wallet）等链上操作。
签名用 eth-account，广播走公共 RPC。
"""
from __future__ import annotations

import time

import requests
from eth_account import Account
from eth_utils import keccak

POLYGON_RPC = "https://polygon-bor-rpc.publicnode.com"
POLYGON_RPCS = [
    "https://polygon-bor-rpc.publicnode.com",
    "https://1rpc.io/matic",
    "https://rpc.ankr.com/polygon",
    "https://polygon.drpc.org",
]
CHAIN_ID = 137

# Uniswap V3 (Polygon)
V3_ROUTER = "0xE592427A0AEce92De3Edee1F18E0157C05861564"   # SwapRouter
V3_FACTORY = "0x1F98431c8aD98523631AE4a59f267346ea31F984"

# USDC 代币
USDC_NATIVE = "0x3c499c542cEF5E3811e1192ce70d8cC03d5c3359"  # Circle 原生 USDC
USDC_E = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"      # 桥接 USDC.e
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"        # Polymarket pUSD
ONRAMP = "0x93070a847efEf7F70739046A929D47a521F5B8ee"      # CollateralOnramp (USDC.e→pUSD)
ZERO_ADDR = "0x" + "00" * 20


class ChainError(RuntimeError):
    pass


def _rpc(method: str, params: list, rpc: str | None = None) -> dict:
    """RPC 调用，多端点轮换 + 代理抖动容错。

    公共 RPC 直连（trust_env=False，不走系统代理）—— 实测代理对多数
    Polygon RPC 域名不稳定（SSL EOF），直连 publicnode 反而稳定。
    """
    candidates = [rpc] if rpc else POLYGON_RPCS
    last_err: Exception | None = None
    for rpc_url in candidates:
        try:
            s = requests.Session()
            s.trust_env = False  # 直连，绕过系统代理
            r = s.post(rpc_url, json={"jsonrpc": "2.0", "id": 1,
                                      "method": method,
                                      "params": params}, timeout=20)
            j = r.json()
            if "error" in j:
                raise ChainError(f"RPC {method}@{rpc_url}: {j['error']}")
            return j["result"]
        except Exception as e:  # noqa: BLE001
            last_err = e
    raise ChainError(f"RPC {method} failed on all endpoints: {last_err}")


def get_nonce(address: str) -> int:
    return int(_rpc("eth_getTransactionCount", [address, "latest"]), 16)


def get_gas_price() -> int:
    return int(_rpc("eth_gasPrice", []), 16)


def estimate_gas(tx: dict) -> int:
    try:
        return int(_rpc("eth_estimateGas", [tx]), 16)
    except ChainError:
        return 300_000


def _erc20_data(method: str, *args: int | str) -> str:
    """ERC-20 函数 calldata（approve/transfer 等，定长参数）。"""
    sig = {
        "approve": "095ea7b3",        # approve(address,uint256)
        "transfer": "a9059cbb",       # transfer(address,uint256)
        "balanceOf": "70a08231",      # balanceOf(address)
    }[method]
    out = "0x" + sig
    for a in args:
        if isinstance(a, int):
            out += a.to_bytes(32, "big").hex()
        elif isinstance(a, str) and a.startswith("0x"):
            out += a[2:].lower().zfill(64)
        else:
            raise ValueError(f"unsupported arg {a!r}")
    return out


def _raw_tx(to: str, data: str, value: int = 0, nonce: int | None = None,
            gas: int | None = None, gas_price: int | None = None) -> dict:
    return {
        "to": to,
        "data": data,
        "value": hex(value),
        "nonce": hex(nonce if nonce is not None else get_nonce(_sender())),
        "gas": hex(gas or 300_000),
        "gasPrice": hex(gas_price or get_gas_price()),
        "chainId": hex(CHAIN_ID),
    }


_sender_addr: str = ""


def _sender() -> str:
    global _sender_addr
    return _sender_addr


def send_transaction(private_key: str, to: str, data: str, value: int = 0,
                     nonce: int | None = None, gas: int | None = None,
                     gas_price: int | None = None) -> dict:
    """签名并广播交易，返回 {txHash, from, to, data}。"""
    global _sender_addr
    acct = Account.from_key(private_key)
    _sender_addr = acct.address
    tx = _raw_tx(to, data, value, nonce, gas, gas_price)
    if gas is None:
        try:
            est = estimate_gas({"from": acct.address, "to": to,
                                "data": data, "value": hex(value)})
            tx["gas"] = hex(est)
        except Exception:  # noqa: BLE001 估算失败用默认
            pass
    signed = acct.sign_transaction(tx)
    raw = signed.raw_transaction.hex()
    if not raw.startswith("0x"):
        raw = "0x" + raw
    tx_hash = _rpc("eth_sendRawTransaction", [raw])
    return {"txHash": tx_hash, "from": acct.address, "to": to, "data": data}


def wait_tx(tx_hash: str, timeout: int = 180, poll: int = 5) -> dict:
    """轮询交易回执（1=成功，0=失败）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        rcpt = _rpc("eth_getTransactionReceipt", [tx_hash])
        if rcpt:
            return rcpt
        time.sleep(poll)
    raise TimeoutError(f"tx {tx_hash} not mined within {timeout}s")


def call_balance(token: str, addr: str) -> int:
    """只读查 ERC-20 余额（base units）。"""
    data = _erc20_data("balanceOf", addr)
    res = _rpc("eth_call", [{"to": token, "data": data}, "latest"])
    return int(res, 16)


def find_v3_pool(token_a: str, token_b: str, fee: int = 100) -> str:
    """Uniswap V3 池地址（factory.getPool）。"""
    sig = "16934fc4"  # getPool(address,address,uint24)
    data = "0x" + sig + token_a[2:].lower().zfill(64) + \
        token_b[2:].lower().zfill(64) + fee.to_bytes(32, "big").hex()
    res = _rpc("eth_call", [{"to": V3_FACTORY, "data": data}, "latest"])
    pool = "0x" + res[-40:]
    if pool == ZERO_ADDR:
        raise ChainError(f"V3 pool not found for fee={fee}")
    return pool
