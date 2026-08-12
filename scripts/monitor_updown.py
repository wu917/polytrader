"""updown 5/15 分钟市场综合监控器（实盘验证多策略）。

每轮（默认 10s）：
1. 拉当前 5M/15M 窗口全部币种市场（gamma /events/keyset）
2. 每市场取 CLOB 订单簿 → 共识价（gamma ref）/ 盘口和 / 距结算秒数
3. 事件检测：
   - ARB：YES+NO 共识价和 < 1 - min_edge（二元套利）
   - DIP：价格相对上一轮跳变幅度 > dip_pct（DipArb 暴跌事件）
   - TAIL：距结算 < tail_s 且价格动量 > tail_momentum（尾段动量）
   - CROSS：同币 5m 与 15m 共识价差 > cross_diff（跨窗口分歧）
4. 累积记录追加到 CSV；事件写入 JSON

结果保留: backtest_results/updown_monitor_<ts>.csv / .json
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.data.clob_client import ClobClient
from polytrader.data.http_client import HttpClient
from polytrader.ai.features import _parse_ts

COINS = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]


def fetch_markets(http, coins):
    """拉当前 5M/15M 窗口市场，返回 {slug: dict}。"""
    now = int(time.time())
    w5 = (now // 300) * 300
    w15 = (now // 900) * 900
    slugs = [f"{c}-updown-{w}-{ts}" for c in coins for w, ts in (("5m", w5), ("15m", w15))]
    resp = http.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                         "&".join(f"slug={s}" for s in slugs) + "&limit=100&locale=en")
    events = resp if isinstance(resp, list) else resp.get("events", [])
    out = {}
    for ev in events:
        for m in ev.get("markets", []) or []:
            slug = m.get("slug", "")
            if not slug or "updown" not in slug:
                continue
            prices = m.get("outcomePrices") or ""
            tokens = m.get("clobTokenIds") or ""
            try:
                prices = json.loads(prices) if isinstance(prices, str) else prices
                tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            except Exception:
                continue
            if len(tokens) < 2 or len(prices) < 2:
                continue
            out[slug] = {
                "condition_id": m.get("conditionId", ""),
                "end_ts": _parse_ts(m.get("endDate", "")) or 0,
                "liquidity": float(m.get("liquidity") or 0),
                "ref_yes": float(prices[0]), "ref_no": float(prices[1]),
                "token_yes": tokens[0], "token_no": tokens[1],
            }
    return out, w5, w15


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:7890")
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--interval", type=float, default=10.0, help="轮间隔秒")
    ap.add_argument("--rounds", type=int, default=30, help="最大轮数（0=无限）")
    ap.add_argument("--min-edge", type=float, default=0.005, help="套利边缘阈值")
    ap.add_argument("--dip-pct", type=float, default=0.15, help="DipArb 跳变阈值")
    ap.add_argument("--tail-s", type=int, default=120, help="尾段窗口（距结算秒数）")
    ap.add_argument("--tail-momentum", type=float, default=0.10, help="尾段动量阈值")
    ap.add_argument("--cross-diff", type=float, default=0.15, help="跨窗口价差阈值")
    args = ap.parse_args()
    coins = [c for c in args.coins.split(",") if c]

    http = HttpClient(proxy=args.proxy)
    clob = ClobClient(http=http)

    ts0 = time.strftime("%Y%m%d_%H%M%S")
    out_dir = Path("backtest_results")
    out_dir.mkdir(exist_ok=True)
    csv_path = out_dir / f"updown_monitor_{ts0}.csv"
    json_path = out_dir / f"updown_monitor_{ts0}.json"

    events = []
    prev_ref: dict[str, float] = {}   # slug -> 上一轮 ref_yes
    rounds = 0
    with open(csv_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "window", "coin", "slug", "end_ts", "secs_left",
                         "ref_yes", "ref_no", "ref_sum", "liq",
                         "bid_sum", "ask_sum", "events"])
        while args.rounds == 0 or rounds < args.rounds:
            rounds += 1
            markets, w5, w15 = fetch_markets(http, coins)
            now = int(time.time())
            for slug, m in sorted(markets.items()):
                coin = slug.split("-")[0]
                window = "15m" if "-15m-" in slug else "5m"
                secs_left = int(m["end_ts"] - now)
                by = clob.get_book(m["token_yes"])
                bn = clob.get_book(m["token_no"])
                ay = by.best_ask() if by else None
                an = bn.best_ask() if bn else None
                byy = by.best_bid() if by else None
                bnn = bn.best_bid() if bn else None
                bid_sum = round(byy.price + bnn.price, 4) if byy and bnn else None
                ask_sum = round(ay.price + an.price, 4) if ay and an else None
                ref_sum = round(m["ref_yes"] + m["ref_no"], 4)
                edge = round(1.0 - ref_sum, 4)

                ev_flags = []
                # 1) 二元套利
                if edge >= args.min_edge:
                    ev_flags.append(f"ARB+{edge:.3f}")
                # 2) DipArb 暴跌
                prev = prev_ref.get(slug)
                if prev:
                    jump = (m["ref_yes"] - prev) / prev if prev else 0
                    if abs(jump) >= args.dip_pct:
                        ev_flags.append(f"DIP{jump:+.1%}")
                # 3) 尾段动量
                if 0 < secs_left <= args.tail_s:
                    prev_t = prev_ref.get(slug)
                    if prev_t:
                        mom = (m["ref_yes"] - prev_t) / prev_t if prev_t else 0
                        if abs(mom) >= args.tail_momentum:
                            ev_flags.append(f"TAIL{mom:+.1%}")
                # 4) 跨窗口价差
                pair = f"{coin}-updown-{'5m' if window == '15m' else '15m'}-"
                other = [k for k in markets if k.startswith(pair)]
                if other:
                    o = markets[other[0]]
                    diff = abs(m["ref_yes"] - o["ref_yes"])
                    if diff >= args.cross_diff:
                        ev_flags.append(f"CROSS{diff:.2f}")

                writer.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ"), window, coin,
                                 slug, m["end_ts"], secs_left,
                                 m["ref_yes"], m["ref_no"], ref_sum,
                                 m["liquidity"], bid_sum, ask_sum,
                                 "|".join(ev_flags)])
                if ev_flags:
                    events.append({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                   "slug": slug, "window": window, "coin": coin,
                                   "secs_left": secs_left,
                                   "ref_yes": m["ref_yes"], "ref_no": m["ref_no"],
                                   "events": ev_flags})
                prev_ref[slug] = m["ref_yes"]
            fh.flush()
            print(f"  round {rounds}: markets={len(markets)} "
                  f"events={sum(1 for e in events if e['ts'] >= time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - args.interval * 2)))} "
                  f"total_events={len(events)}", flush=True)
            if args.rounds and rounds >= args.rounds:
                break
            time.sleep(args.interval)

    json_path.write_text(json.dumps(
        {"config": {"interval": args.interval, "rounds": rounds,
                    "min_edge": args.min_edge, "dip_pct": args.dip_pct,
                    "tail_s": args.tail_s, "tail_momentum": args.tail_momentum,
                    "cross_diff": args.cross_diff},
         "events": events}, indent=2, ensure_ascii=False))
    print(f"\n  rounds={rounds} total_events={len(events)}")
    print(f"  saved: {csv_path}")
    print(f"  saved: {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
