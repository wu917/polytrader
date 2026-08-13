"""Polygon 链上充值：EOA 的 USDC(原生) -> pUSD -> 转入 deposit wallet。

流程（Paraswap 聚合器 + Onramp 官方封装）：
  1. approve USDC -> tokenTransferProxy
  2. Paraswap swap USDC -> pUSD（几乎 1:1）
  3. transfer pUSD -> deposit wallet（Polymarket deposit 合约自动 approve）

用法：python scripts/fund_deposit.py [--amount 1.10]
"""
import argparse
import os
import sys
import time

import requests
from dotenv import load_dotenv
from eth_account import Account
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from polytrader.execution import chain  # noqa: E402

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

PARASWAP = "https://apiv5.paraswap.io"
TOKEN_TRANSFER_PROXY = "0x216B4B4Ba9F3e719726886d34a177484278BFCae"
USDC = chain.USDC_NATIVE


def _req(method: str, url: str, **kw):
    for i in range(4):
        try:
            r = requests.request(method, url, timeout=20,
                                 headers={"User-Agent": "Mozilla/5.0",
                                          **kw.pop("headers", {})}, **kw)
            return r
        except Exception as e:  # noqa: BLE001 代理抖动重试
            print(f"  重试{i + 1}: {str(e)[:50]}")
            time.sleep(3)
    raise RuntimeError("请求失败")


def quote(amount: int) -> dict:
    r = _req("GET", f"{PARASWAP}/prices",
             params={"network": 137, "srcToken": USDC,
                     "destToken": chain.PUSD, "amount": amount,
                     "srcDecimals": 6, "destDecimals": 6, "side": "SELL"})
    r.raise_for_status()
    return r.json()["priceRoute"]


def build_tx(price_route: dict, user: str) -> dict:
    body = {
        "srcToken": USDC, "destToken": chain.PUSD,
        "srcDecimals": 6, "destDecimals": 6,
        "srcAmount": price_route["srcAmount"],
        "destAmount": price_route["destAmount"],
        "userAddress": user, "side": "SELL", "priceRoute": price_route,
    }
    r = _req("POST", f"{PARASWAP}/transactions/137", json=body)
    r.raise_for_status()
    return r.json()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--amount", type=float, default=1.10, help="充值金额 USDC")
    ap.add_argument("--dry-run", action="store_true", help="只报价不广播")
    args = ap.parse_args()

    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit = os.environ["POLYMARKET_DEPOSIT_WALLET"].strip()
    acct = Account.from_key(pk)
    print(f"EOA: {acct.address} | deposit wallet: {deposit}")

    bal = chain.call_balance(USDC, acct.address) / 1e6
    print(f"EOA USDC: {bal:.6f}")
    if args.amount > bal:
        print(f"余额不足：需要 ${args.amount}，只有 ${bal:.2f}")
        return 1

    amount = int(args.amount * 1e6)
    pr = quote(amount)
    out = int(pr["destAmount"]) / 1e6
    print(f"报价: ${args.amount} USDC -> {out:.6f} pUSD")
    if args.dry_run:
        print("dry-run 结束（未广播）")
        return 0

    # 1. approve USDC -> tokenTransferProxy（幂等：已有额度则跳过）
    allowance = 0
    try:
        # 查 allowance
        data = "0xdd62ed3e" + acct.address[2:].lower().zfill(64) + \
            TOKEN_TRANSFER_PROXY[2:].lower().zfill(64)
        res = chain._rpc("eth_call", [{"to": USDC, "data": data}, "latest"])
        allowance = int(res, 16)
    except Exception:
        allowance = 0
    if allowance < amount:
        print("approve USDC -> tokenTransferProxy ...")
        r = chain.send_transaction(pk, USDC,
                                   chain._erc20_data("approve", TOKEN_TRANSFER_PROXY, 2**256 - 1))
        print(f"  approve tx: {r['txHash']}")
        chain.wait_tx(r["txHash"])
        print("  approved ✓")

    # 2. swap
    tx = build_tx(pr, acct.address)
    print(f"swap via {tx.get('to')} ...")
    r = chain.send_transaction(pk, tx["to"], tx["data"],
                               value=int(tx.get("value", "0"), 16))
    print(f"  swap tx: {r['txHash']}")
    chain.wait_tx(r["txHash"])
    print("  swapped ✓")

    # 3. transfer pUSD -> deposit wallet
    pbal = chain.call_balance(chain.PUSD, acct.address)
    print(f"EOA pUSD 余额: {pbal / 1e6:.6f}")
    if pbal > 0:
        r = chain.send_transaction(pk, chain.PUSD,
                                   chain._erc20_data("transfer", deposit, pbal))
        print(f"transfer pUSD -> deposit: {r['txHash']}")
        chain.wait_tx(r["txHash"])
        print("  transferred ✓")

    print(f"deposit wallet pUSD: {chain.call_balance(chain.PUSD, deposit) / 1e6:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
