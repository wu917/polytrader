"""分析监控 CSV：跨窗口价差与 DIP 详情。"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

path = sorted(Path("backtest_results").glob("updown_monitor_*.csv"))[-1]
rows = list(csv.DictReader(open(path)))
print(f"file: {path.name} rows={len(rows)}")

# 跨窗口价差：同币 5m vs 15m ref_yes 差异
from collections import defaultdict
by_coin_window = defaultdict(dict)  # (coin, window) -> latest ref_yes
cross = []
for r in rows:
    slug = r["slug"]
    coin = slug.split("-")[0]
    window = r["window"]
    try:
        ref_yes = float(r["ref_yes"])
    except ValueError:
        continue
    key = (coin, window)
    if "5m" in slug:
        by_coin_window[(coin, "5m")] = (ref_yes, r["ts"], r["secs_left"])
    else:
        by_coin_window[(coin, "15m")] = (ref_yes, r["ts"], r["secs_left"])
    other = by_coin_window.get((coin, "5m" if window == "15m" else "15m"))
    if other:
        diff = abs(ref_yes - other[0])
        if diff >= 0.10:
            cross.append((diff, slug, window, ref_yes, other[0], r["secs_left"]))

cross.sort(reverse=True)
print(f"cross-window diffs >= 0.10: {len(cross)}")
for d in cross[:6]:
    print(f"  diff={d[0]:.2f} {d[1]} ({d[2]}) yes={d[3]:.3f} vs {d[4]:.3f} left={d[5]}s")

# DIP 事件
print("\nDIP events (ref_yes jumps):")
prev = {}
for r in rows:
    slug = r["slug"]
    try:
        cur = float(r["ref_yes"])
    except ValueError:
        continue
    if slug in prev and prev[slug] > 0:
        jump = (cur - prev[slug]) / prev[slug]
        if abs(jump) >= 0.10:
            print(f"  {r['ts']} {slug:34s} yes {prev[slug]:.3f} -> {cur:.3f} ({jump:+.1%})")
    prev[slug] = cur
