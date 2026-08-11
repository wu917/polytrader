"""真实 API 冒烟测试（需网络，走代理）：Gamma 市场发现 + CLOB 订单簿。

用法: .venv/bin/python scripts/smoke_data.py [--proxy socks5h://127.0.0.1:7890]
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.data.clob_client import ClobClient
from polytrader.data.data_api import DataApiClient
from polytrader.data.gamma_client import GammaClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:7890")
    ap.add_argument("--limit", type=int, default=20)
    args = ap.parse_args()

    gamma = GammaClient(http=__import__("polytrader.data.http_client", fromlist=["HttpClient"]).HttpClient(proxy=args.proxy))
    markets = gamma.get_markets(limit=args.limit)
    print(f"[gamma] fetched {len(markets)} markets")
    binary = [m for m in markets if m.is_binary]
    print(f"[gamma] binary: {len(binary)}")
    for m in binary[:5]:
        print(f"  - {m.slug}: {m.question[:60]} | liq={m.liquidity:.0f} | outcomes={len(m.outcomes)}")

    if markets:
        m0 = markets[0]
        print(f"[diag] parsed[0]: slug={m0.slug!r} outcomes={len(m0.outcomes)} "
              f"names={[o.name for o in m0.outcomes]} tokens={[o.token_id[:12] + '...' for o in m0.outcomes]}")

    if not binary:
        # 诊断：打印原始返回的 outcomes 结构
        raw = gamma.http.get_json(f"{gamma.api_base}/markets", params={"limit": 2, "active": "true"})
        if isinstance(raw, list) and raw:
            m0 = raw[0]
            print(f"[diag] raw keys: {list(m0.keys())[:20]}")
            print(f"[diag] outcomes raw: {str(m0.get('outcomes'))[:400]}")
            print(f"[diag] outcomePrices: {str(m0.get('outcomePrices'))[:200]}")
            print(f"[diag] outcomeTokens: {str(m0.get('outcomeTokens'))[:200]}")
            print(f"[diag] clobTokenIds: {str(m0.get('clobTokenIds'))[:200]}")
            token_keys = [k for k in m0.keys() if 'token' in k.lower()]
            print(f"[diag] token-like keys: {token_keys}")
            print(f"[diag] question: {m0.get('question')}")
            print(f"[diag] conditionId: {m0.get('conditionId')}")
        print("[gamma] no binary markets, abort")
        return 1

    # CLOB 订单簿
    clob = ClobClient(http=__import__("polytrader.data.http_client", fromlist=["HttpClient"]).HttpClient(proxy=args.proxy))
    token = binary[0].outcomes[0].token_id
    book = clob.get_book(token)
    if book:
        print(f"[clob] {binary[0].slug} YES book: best_bid={book.best_bid()}, best_ask={book.best_ask()}, depth3=${book.depth_usd(3):.2f}")
    else:
        print("[clob] book is None")
        return 1

    # data-api 历史价格
    da = DataApiClient(http=__import__("polytrader.data.http_client", fromlist=["HttpClient"]).HttpClient(proxy=args.proxy))
    hist = da.price_history(binary[0].condition_id, interval="1h")
    print(f"[data-api] price-history rows={len(hist)}, last={hist[-1] if hist else None}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
