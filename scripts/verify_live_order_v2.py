"""真实下单测试（CLOB V2，deposit wallet，signatureType=3）。

用法：POLYMARKET_PRIVATE_KEY/RELAYER 等从 .env 读取。
默认 BUY $1 FOK 当前 btc updown 5m 窗口 YES 份额。
"""
import json
import os
import time

import requests
from dotenv import load_dotenv
from eth_account import Account
from pathlib import Path

from polytrader.execution import order_v2, signer

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def req(method: str, url: str, **kw):
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


def current_btc_yes() -> tuple[str, float, str]:
    """当前 btc 5m 窗口的 YES token + 价格 + 结束时间。"""
    now = int(time.time())
    w5 = (now // 300) * 300
    slug = f"btc-updown-5m-{w5}"
    r = req("GET", "https://gamma-api.polymarket.com/events/keyset?"
            + f"slug={slug}&limit=10&locale=en")
    events = r.json() if isinstance(r.json(), list) else r.json().get("events", [])
    for ev in events:
        for m in ev.get("markets", []) or []:
            if m.get("slug") != slug:
                continue
            toks = json.loads(m.get("clobTokenIds") or "[]")
            prices = json.loads(m.get("outcomePrices") or "[]")
            return toks[0], float(prices[0]), m.get("endDate", "")
    raise RuntimeError(f"未找到市场 {slug}")


def main() -> int:
    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "").strip()
    eoa = Account.from_key(pk).address
    token, price, end = current_btc_yes()
    print(f"市场: btc-updown-5m | YES@{price} | end={end} | deposit={deposit}")

    # L2 凭证
    ts = int(time.time())
    sig = signer.sign_clob_auth(eoa, pk, timestamp=ts, nonce=0)
    cred = req("GET", "https://clob.polymarket.com/auth/derive-api-key",
               headers=signer.derive_api_key_headers(eoa, sig, ts)).json()

    # 订单：BUY $1 FOK @ 当前 YES 价（向上取整保证成交）
    buy_price = min(0.99, round((price + 0.005) / 0.005) * 0.005)
    maker_amt, taker_amt = order_v2.calc_amounts(order_v2.BUY, 1.0, buy_price)
    ts_ms = str(time.time_ns() // 1_000_000)
    td = order_v2.build_order_typed_data(
        maker=deposit, signer=deposit, token_id=token,
        maker_amount=maker_amt, taker_amount=taker_amt,
        side=order_v2.BUY, signature_type=order_v2.POLY_1271,
        timestamp_ms=ts_ms, contract=order_v2.CTF_EXCHANGE_V2)
    sig1271 = order_v2.sign_order_poly1271(td, pk, order_v2.CTF_EXCHANGE_V2, 137)
    order = {**td["message"], "signature": sig1271,
             "salt": str(td["message"]["salt"]), "timestamp": ts_ms,
             "metadata": order_v2.ZERO_BYTES32, "builder": order_v2.ZERO_BYTES32}
    payload = order_v2.order_to_json_v2(order, owner=cred["apiKey"], order_type="FOK")
    print(f"BUY YES ${maker_amt / 1e6:.2f} @ {buy_price} -> {taker_amt / 1e6:.4f} shares")

    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    h2 = signer.l2_headers_new(eoa, cred["apiKey"], cred["passphrase"],
                               cred["secret"], "POST", "/order", body=serialized)
    h2["Content-Type"] = "application/json"
    r = req("POST", "https://clob.polymarket.com/order", data=serialized, headers=h2)
    print("POST /order:", r.status_code)
    print("响应:", r.text[:900])
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    raise SystemExit(main())
