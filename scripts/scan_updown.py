"""扫描 BTC/ETH（及全币种）5/15 分钟 updown 市场盘口 + 套利检测。

用法: .venv/bin/python scripts/scan_updown.py [--coins btc,eth,sol,xrp,doge,hype,bnb]
结果保留: backtest_results/updown_scan_<ts>.json
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.data.clob_client import ClobClient
from polytrader.data.http_client import HttpClient

COINS = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:7890")
    ap.add_argument("--coins", default=",".join(COINS))
    args = ap.parse_args()
    coins = [c for c in args.coins.split(",") if c]

    http = HttpClient(proxy=args.proxy)
    clob = ClobClient(http=http)

    now = int(time.time())
    w5 = (now // 300) * 300
    w15 = (now // 900) * 900
    slugs = [f"{c}-updown-{w}-{ts}" for c in coins for w, ts in (("5m", w5), ("15m", w15))]
    resp = http.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                         "&".join(f"slug={s}" for s in slugs) + "&limit=100&locale=en")
    events = resp if isinstance(resp, list) else resp.get("events", [])
    print(f"events: {len(events)} (window 5m={w5} 15m={w15})")

    results = []
    for ev in events:
        for m in ev.get("markets", []) or []:
            cid = m.get("conditionId", "")
            slug = m.get("slug", "")
            if not cid or "updown" not in slug:
                continue
            prices = m.get("outcomePrices") or ""
            tokens = m.get("clobTokenIds") or ""
            try:
                prices = json.loads(prices) if isinstance(prices, str) else prices
                tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            except Exception:
                continue
            if len(tokens) < 2:
                continue
            by = clob.get_book(tokens[0])
            bn = clob.get_book(tokens[1])
            ay = by.best_ask() if by else None
            an = bn.best_ask() if bn else None
            byy = by.best_bid() if by else None
            bnn = bn.best_bid() if bn else None
            ref_yes = float(prices[0]) if prices else 0.0
            ref_no = float(prices[1]) if len(prices) > 1 else 0.0
            rec = {
                "slug": slug, "condition_id": cid,
                "end_date": m.get("endDate", ""),
                "liquidity": float(m.get("liquidity") or 0),
                "ref_yes": ref_yes, "ref_no": ref_no,
                "ref_sum": round(ref_yes + ref_no, 4),
                "bid_sum": round((byy.price + bnn.price), 4) if byy and bnn else None,
                "ask_sum": round((ay.price + an.price), 4) if ay and an else None,
                "arb_edge": round(1.0 - (ref_yes + ref_no), 4),
            }
            results.append(rec)
            print(f"  {slug:34s} ref_sum={rec['ref_sum']:.3f} "
                  f"bid_sum={rec['bid_sum']} ask_sum={rec['ask_sum']} "
                  f"edge={rec['arb_edge']:+.3f} liq={rec['liquidity']:.0f}")

    out_dir = Path("backtest_results")
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"updown_scan_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps({"window": {"5m": w5, "15m": w15},
                                "markets": results}, indent=2))
    print(f"\n  saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
