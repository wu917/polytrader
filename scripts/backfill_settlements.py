"""补结算：对 run_llm_loop 结果文件中未结算（pnl=null）的交易补查结算并追加事件。

用法: .venv/bin/python scripts/backfill_settlements.py [--results backtest_results/llm_results_*.jsonl]
输出: backtest_results/backfill_<ts>.csv + 追加 trade_settled 事件到结果文件
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.data.http_client import HttpClient

HTTP = HttpClient(proxy="socks5h://127.0.0.1:7890", timeout=15)


def fetch_settle(slug: str) -> float | None:
    try:
        resp = HTTP.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                             f"slug={slug}&limit=10&locale=en")
    except Exception:
        return None
    events = resp if isinstance(resp, list) else resp.get("events", [])
    for ev in events:
        for m in ev.get("markets", []) or []:
            if m.get("slug") != slug:
                continue
            prices = m.get("outcomePrices") or ""
            try:
                prices = json.loads(prices) if isinstance(prices, str) else prices
            except Exception:
                return None
            if not prices:
                return None
            yes = float(prices[0])
            if yes in (0.0, 1.0):
                return yes
            return None
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=str, default="")
    args = ap.parse_args()

    if args.results:
        results_path = Path(args.results)
    else:
        results_path = sorted(Path("backtest_results").glob("llm_results_*.jsonl"))[-1]
    print(f"results: {results_path.name}")

    # 收集未结算交易（round 事件里的 trades + pnl=null），
    # 跳过已 backfill 过的（results 中已有 trade_settled 事件）
    pending = {}
    settled_ids = set()
    for line in results_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "round":
            for t in rec.get("trades", []):
                if t.get("pnl") is None:
                    pending[t["slug"]] = t
        elif rec.get("type") == "trade_settled":
            if rec.get("trade_id"):
                settled_ids.add(rec["trade_id"])
    pending = {s: t for s, t in pending.items()
               if t.get("trade_id") not in settled_ids}
    print(f"pending unsettled trades: {len(pending)}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_csv = Path("backtest_results") / f"backfill_{ts}.csv"
    fixed = []
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "trade_id", "slug", "coin", "window", "side",
                    "entry_price", "size_usd", "settle_yes", "win", "pnl"])
        for slug, t in sorted(pending.items()):
            settle = fetch_settle(slug)
            if settle is None:
                print(f"  still unsettled: {slug}")
                continue
            win = (t["side"] == "YES" and settle == 1.0) or \
                  (t["side"] == "NO" and settle == 0.0)
            pnl = round((t["size_usd"] / t["entry_price"]) * (1.0 if win else 0.0)
                        - t["size_usd"], 2)
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        t.get("trade_id"), slug, t.get("coin"),
                        t.get("window"), t["side"], t["entry_price"],
                        t["size_usd"], settle, 1 if win else 0, pnl])
            # 追加事件到结果文件
            with open(results_path, "a", encoding="utf-8") as rf:
                rf.write(json.dumps({
                    "type": "trade_settled", "round": t.get("round"),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "trade_id": t.get("trade_id"), "slug": slug,
                    "coin": t.get("coin"), "window": t.get("window"),
                    "side": t["side"], "entry_price": t["entry_price"],
                    "size_usd": t["size_usd"], "settle_yes": settle,
                    "win": 1 if win else 0, "pnl": pnl,
                    "backfilled": True}, ensure_ascii=False) + "\n")
            fixed.append((slug, settle, win, pnl))
            print(f"  fixed {slug}: settle_yes={settle} win={win} pnl=${pnl:+.2f}")

    wins = sum(1 for _, _, w, _ in fixed if w)
    total = sum(p for _, _, _, p in fixed)
    print(f"\nbackfilled={len(fixed)} wins={wins} total_pnl=${total:+.2f}")
    print(f"saved: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
