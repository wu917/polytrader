"""汇总已结束挂机轮次的收益率统计（统一 $1/笔口径对比）。"""
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 轮次 1：挂机 3 轮（14:27-14:45，$100/笔）—— 用 llm_updown_sim_145132/150012/150855
# 轮次 2：20 轮（17:01-17:54，$1/笔）—— llm_results_170142.jsonl + backfill_181602.csv
# 轮次 3：10 轮 5m（18:42-19:25，$1/笔）—— llm_results_184238.jsonl + backfill_20260813_101714.csv

def summarize(name, trades):
    settled = [t for t in trades if t.get("pnl") is not None]
    wins = sum(1 for t in settled if t["pnl"] > 0)
    total = sum(t["pnl"] for t in settled)
    invest = sum(t["size_usd"] for t in settled)
    ret = total / invest if invest else 0
    print(f"{name:28s} trades={len(trades):3d} settled={len(settled):3d} "
          f"W/L={wins}/{len(settled)-wins}  win_rate={wins/max(len(settled),1):6.1%}  "
          f"invest=${invest:7.2f}  pnl=${total:+8.2f}  return={ret:+.1%}")
    return settled, wins, total, invest

# 轮次 1：读 3 个 sim JSON（$100/笔），换算 $1 口径
t1 = []
for f in ["backtest_results/llm_updown_sim_20260812_145132.json",
          "backtest_results/llm_updown_sim_20260812_150012.json",
          "backtest_results/llm_updown_sim_20260812_150855.json"]:
    try:
        d = json.loads(open(f))
        for t in d.get("trades", []):
            if t.get("pnl") is not None:
                t = dict(t, size_usd=1.0, pnl=round(t["pnl"] / 100.0, 4))
            t1.append(t)
    except Exception:
        pass

# 轮次 2：bash-90（18:12，1 轮）—— llm_results_181642.jsonl
t2 = []
for line in Path("backtest_results/llm_results_20260812_181642.jsonl").read_text().splitlines():
    try:
        rec = json.loads(line)
    except Exception:
        continue
    if rec.get("type") == "round":
        t2.extend(rec.get("trades", []))

# 轮次 3：10 轮 5m（18:42）—— llm_results_184238.jsonl + backfill
t3 = []
for line in Path("backtest_results/llm_results_20260812_184238.jsonl").read_text().splitlines():
    try:
        rec = json.loads(line)
    except Exception:
        continue
    if rec.get("type") == "round":
        t3.extend(rec.get("trades", []))
for f in ["backtest_results/backfill_20260813_101714.csv"]:
    with open(f, newline="") as fh:
        for row in csv.DictReader(fh):
            t3.append({"slug": row["slug"], "side": row["side"],
                       "pnl": float(row["pnl"]), "size_usd": float(row["size_usd"])})

print("=== 各轮收益率（轮次 1 已按 $1 口径换算） ===")
s1, w1, p1, i1 = summarize("轮1: 3轮混合(14:27)", t1)
s2, w2, p2, i2 = summarize("轮2: bash-90 1轮(18:12)", t2)
s3, w3, p3, i3 = summarize("轮3: 10轮5m-only(18:42)", t3)
print("\n注：17:01 的 20 轮（$1/笔，8 结算 4W4L -$0.55）结果文件已缺失，仅凭运行记录。")

all_t = t1 + t2 + t3
settled = [t for t in all_t if t.get("pnl") is not None]
wins = sum(1 for t in settled if t["pnl"] > 0)
total = sum(t["pnl"] for t in settled)
invest = sum(t["size_usd"] for t in settled)
print(f"\n=== 总计（$1 统一口径，不含缺失的 17:01 轮） ===")
print(f"settled={len(settled)} W/L={wins}/{len(settled)-wins} "
      f"win_rate={wins/len(settled):.1%} invest=${invest:.0f} "
      f"pnl=${total:+.2f} return={total/invest:+.1%}")
print(f"\n按时间顺序：早期 3 轮盈利(+41%)是 $100 仓位小样本运气；")
print(f"后 $1 口径（轮2+轮3）: {len(s2)+len(s3)} 笔 "
      f"{w2+w3}W/{len(s2)+len(s3)-w2-w3}L "
      f"pnl=${p2+p3:+.2f} return={(p2+p3)/(i2+i3):+.1%}")
