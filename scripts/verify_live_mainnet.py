"""主网小额真实成交验证：认证 → 拉真实市场 → $1 taker 单真实下单 → 确认成交。

⚠️ 真金白银（小额）。使用前：
  1. .env 填好 POLYMARKET_PRIVATE_KEY + API_KEY/SECRET/PASSPHRASE
  2. 钱包有 USDC（≥ $1）+ POL（gas，taker 成交时支付）
  3. 运行会要求终端输入 yes 确认（防误操作）

用法: .venv/bin/python scripts/verify_live_mainnet.py [--usd 1.0] [--confirm]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from polytrader.data.clob_client import ClobClient
from polytrader.data.http_client import HttpClient
from polytrader.execution import signer


def fetch_updown_market(http, ts: int):
    """拉当前 5m 窗口的 updown 市场（取第一个有盘口的）。"""
    import requests
    slug = f"btc-updown-5m-{ts}"
    r = requests.get("https://gamma-api.polymarket.com/events/keyset?" +
                     f"slug={slug}&limit=10&locale=en", timeout=15)
    events = r.json()
    markets = []
    for ev in events if isinstance(events, list) else events.get("events", []):
        for m in ev.get("markets", []) or []:
            markets.append(m)
    if not markets:
        return None
    return markets[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--usd", type=float, default=1.0, help="下单金额 USD")
    ap.add_argument("--confirm", action="store_true", help="跳过交互确认（危险，谨慎使用）")
    args = ap.parse_args()

    import os
    pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    ak = os.environ.get("POLYMARKET_API_KEY", "").strip()
    sk = os.environ.get("POLYMARKET_API_SECRET", "").strip()
    ph = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
    if not (pk and ak and sk and ph):
        print("✗ 凭证缺失：请在 .env 填 POLYMARKET_PRIVATE_KEY / "
              "POLYMARKET_API_KEY / API_SECRET / API_PASSPHRASE")
        return 1

    from eth_account import Account
    maker = Account.from_key(pk).address
    print(f"== 主网小额真实成交验证 ==")
    print(f"  钱包: {maker}  | 金额: ${args.usd:.2f}")

    clob = ClobClient(http=HttpClient(), api_key=ak, api_secret=sk,
                      api_passphrase=ph, private_key=pk)

    # 1. 认证只读验证
    try:
        bal = clob.get_usdc_balance()
        print(f"✓ L2 认证通过，可用 USDC: ${bal:.2f}")
    except Exception as e:
        print(f"✗ 认证失败: {e}")
        return 1
    if bal < args.usd:
        print(f"✗ 可用 USDC ${bal:.2f} < 下单 ${args.usd:.2f}")
        return 1

    # 2. 拉真实 updown 市场
    ts = (int(time.time()) // 300) * 300
    m = fetch_updown_market(None, ts)
    if not m:
        print(f"✗ 拉取市场失败（窗口 {ts}）")
        return 1
    token_id = (m.get("clobTokenIds") or [""])[0]
    if not token_id:
        token_id = (m.get("outcomes") or [{}])[0].get("token_id", "")
    print(f"✓ 市场: {m.get('slug')}  token: {token_id[:20]}...")

    # 3. 取 ask 价
    book = clob.get_book(token_id)
    ask = book.best_ask() if book else None
    if ask is None:
        print("✗ 无盘口 ask（市场可能无流动性）")
        return 1
    print(f"✓ 盘口 ask: {ask.price}")

    # 4. 构造 $usd taker 单（买 YES）
    asset = signer.asset_id(token_id)
    maker_amount = signer.usd_to_maker_amount(args.usd)
    taker_amount = signer.shares_to_taker_amount(args.usd / ask.price)
    order = {
        "maker": maker,
        "taker": signer.ZERO_ADDRESS,
        "tokenId": int(asset, 16),
        "makerAmount": maker_amount,
        "takerAmount": taker_amount,
        "id": int(time.time() * 1000) % (2 ** 63),
        "feeRateBps": 0,
        "nonce": 0,
        "expiration": int(time.time()) + 120,
    }
    signed = signer.build_order(order, pk)
    print(f"✓ EIP-712 签名完成: {signed['signature'][:24]}...")

    # 5. 确认
    print(f"\n⚠️ 即将真实下单: {m.get('slug')} 买 YES ${args.usd:.2f} @ ~{ask.price}")
    if not args.confirm:
        if not sys.stdin or not sys.stdin.isatty():
            print("✗ 需要交互确认但无终端（用 --confirm 显式跳过）")
            return 1
        ans = input("输入 yes 确认下单: ").strip().lower()
        if ans != "yes":
            print("已取消")
            return 0

    # 6. 下单 + 轮询
    try:
        resp = clob.place_order(signed)
        oid = str(resp.get("orderID") or resp.get("order_id") or "")
        print(f"✓ 订单提交: {oid}  resp={ {k: v for k, v in resp.items() if k in ('status','orderID','success')} }")
        if not oid:
            print(f"✗ 无 order_id: {resp}")
            return 1
    except Exception as e:
        print(f"✗ 下单失败: {e}")
        return 1

    for i in range(15):
        time.sleep(2)
        try:
            st = clob.get_order(oid)
        except Exception as e:
            print(f"  [query] {e}")
            continue
        status = str(st.get("status") or "").lower()
        print(f"  订单状态 [{i*2}s]: {status}")
        if status in ("matched", "filled", "done"):
            print(f"\n✓✓ 成交确认！order={oid}")
            print(f"  详情: { {k: st.get(k) for k in ('status','original_size','size_matched','price','side','asset_id')} }")
            return 0
        if status in ("canceled", "cancelled", "expired"):
            print(f"✗ 订单 {status}（未成交）")
            return 1

    print("✗ 15 次轮询未确认成交（订单可能仍在撮合，用 get_order 手动复查）")
    return 1


if __name__ == "__main__":
    sys.exit(main())
