"""分析 lag_measure CSV：Binance 变动 → Polymarket 修正的滞后。

- 事件匹配：Binance 变动 >0.03% 的时刻 → 找 60s 内 Polymarket ref_yes 变动 >0.01 的首次时刻
- 互相关：全序列滞后 -60..+60 步的相关系数峰值
"""
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

path = sorted(Path("backtest_results").glob("lag_measure_*.csv"))[-1]
rows = []
for r in csv.DictReader(open(path)):
    try:
        rows.append({"ts": float(r["ts_unix"]),
                     "price": float(r["btc_price"]) if r["btc_price"] else None,
                     "y5": float(r["btc_5m_yes"]) if r["btc_5m_yes"] else None})
    except ValueError:
        continue
print(f"file: {path.name} rows={len(rows)} span={rows[-1]['ts'] - rows[0]['ts']:.0f}s")

# 事件匹配：Binance 变动
events = []
for i in range(1, len(rows)):
    if rows[i]["price"] and rows[i - 1]["price"]:
        chg = abs(rows[i]["price"] - rows[i - 1]["price"]) / rows[i - 1]["price"]
        if chg >= 0.0003:
            events.append({"t": rows[i]["ts"], "chg": chg,
                           "dir": 1 if rows[i]["price"] > rows[i - 1]["price"] else -1})
print(f"binance move events (>0.03%): {len(events)}")

# 每个事件后找 Polymarket 首次变动
lags = []
for ev in events:
    t0 = ev["t"]
    found = None
    for r in rows:
        if r["ts"] <= t0 or r["y5"] is None:
            continue
        if r["ts"] - t0 > 60:
            break
        # 与事件前最后采样对比
        prev_y = None
        for rp in rows:
            if rp["ts"] < r["ts"] - 1.0 and rp["y5"] is not None:
                prev_y = rp["y5"]
            elif rp["ts"] >= r["ts"] - 1.0 and rp["ts"] < r["ts"] and rp["y5"] is not None:
                prev_y = rp["y5"]
        if prev_y and abs(r["y5"] - prev_y) >= 0.01:
            found = r["ts"] - t0
            break
    if found is not None:
        lags.append(found)
print(f"polymarket follow-ups within 60s: {len(lags)}/{len(events)}")
if lags:
    lags_sorted = sorted(lags)
    print(f"lag: min={lags_sorted[0]:.0f}s med={lags_sorted[len(lags_sorted)//2]:.0f}s "
          f"max={lags_sorted[-1]:.0f}s")

# 互相关（y5 vs price 差分符号一致性：滞后 k 步的相关系数）
import math
n = len(rows)
ys = [r["y5"] for r in rows if r["y5"] is not None]
ps = [r["price"] for r in rows if r["price"] is not None]
m = min(len(ys), len(ps))
dy = [ys[i + 1] - ys[i] for i in range(m - 1)]
dp = [ps[i + 1] - ps[i] for i in range(m - 1)]


def corr(a, b):
    if len(a) < 5:
        return 0.0
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((x - ma) * (y - mb) for x, y in zip(a, b))
    den = math.sqrt(sum((x - ma) ** 2 for x in a) * sum((y - mb) ** 2 for y in b))
    return num / den if den else 0.0


best = (0, 0.0)
for lag in range(0, 31):  # 0..60s（2s 步）
    a = dy[lag:]
    b = dp[:len(dp) - lag] if lag else dp[:len(dy) - lag]
    c = corr(a, b)
    if abs(c) > abs(best[1]):
        best = (lag, c)
print(f"cross-corr: best lag={best[0] * 2}s corr={best[1]:.3f}")
print(f"corr@0s={corr(dy[:], dp[:]):.3f} corr@{'6s'}={corr(dy[3:], dp[:len(dy)-3]):.3f}")
