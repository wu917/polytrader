"""LLM updown 挂机循环：连续 N 轮模拟测算（subprocess 逐轮，天然跟随新窗口）。

每轮：simulate_llm_updown.py --wait 330（当前窗口 → LLM → 模拟成交 $100/笔 → 结算验证）
轮间间隔由结算等待自然形成；结果累计汇总。
用法: .venv/bin/python scripts/run_llm_loop.py --rounds 3 [--min-edge 0.04]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--wait", type=int, default=330)
    ap.add_argument("--size", type=float, default=1.0,
                    help="每笔固定仓位 USD（透传 simulate，默认 $1）")
    ap.add_argument("--out-dir", type=str, default="backtest_results",
                    help="所有输出目录（日志/结果/审计/临时轮次）")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts0 = time.strftime("%Y%m%d_%H%M%S")
    # 单日志文件：本进程全部输出 + 每轮 subprocess stdout/stderr
    log_file = out_dir / f"llm_loop_{ts0}.log"
    # 单结果文件：JSONL 事件流（round / trade_settled / summary）
    results_file = out_dir / f"llm_results_{ts0}.jsonl"
    # 单审计文件：合并各轮调用级审计
    audit_all = out_dir / f"audit_all_{ts0}.jsonl"
    run_dir = out_dir / "rounds_tmp"
    run_dir.mkdir(exist_ok=True)

    def log(msg: str):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
        print(line, flush=True)
        with open(log_file, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def emit(rec: dict):
        with open(results_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # 清空临时轮次目录
    for p in run_dir.glob("*"):
        p.unlink()

    for i in range(1, args.rounds + 1):
        log(f"=== LOOP {i}/{args.rounds} ===")
        proc = subprocess.run(
            [sys.executable, "scripts/simulate_llm_updown.py",
             "--wait", str(args.wait), "--min-edge", str(args.min_edge),
             "--size", str(args.size), "--audit-dir", str(run_dir),
             "--seen-file", str(out_dir / "seen_slugs.txt")],
            cwd=ROOT, capture_output=True, text=True, timeout=args.wait + 240,
        )
        # 本轮输出全部写入日志文件
        for line in proc.stdout.splitlines():
            log("  " + line)
        if proc.stderr.strip():
            for line in proc.stderr.splitlines()[-20:]:
                log("  ERR " + line)
        if proc.returncode != 0:
            log(f"  round failed rc={proc.returncode}")
            continue
        # 收集本轮结果（simulate 写入 run_dir 的 JSON）
        for sim in sorted(run_dir.glob("llm_updown_sim_*.json"),
                          key=lambda p: p.stat().st_mtime):
            try:
                d = json.loads(sim.read_text())
            except Exception:
                continue
            emit({"type": "round", "round": i,
                  "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                  "trades": d.get("trades", []),
                  "evaluations": d.get("evaluations", []),
                  "config": d.get("config", {})})
            sim.unlink()
        # 结算 CSV 行并入结果文件（保留 CSV 原始文件）
        for s_csv in sorted(run_dir.glob("settlements_*.csv")):
            import csv as _csv
            with open(s_csv, newline="", encoding="utf-8") as fh:
                for row in _csv.reader(fh):
                    if not row or row[0] == "ts":
                        continue
                    emit({"type": "trade_settled", "round": i,
                          "ts": row[0], "trade_id": row[1], "slug": row[2],
                          "coin": row[3], "window": row[4], "side": row[5],
                          "entry_price": row[6], "size_usd": row[7],
                          "settle_yes": row[8] or None, "win": row[9] or None,
                          "pnl": row[10] or None})
            s_csv.unlink()
        # 审计合并到单审计文件
        for a in sorted(run_dir.glob("audit_llm_*.jsonl")):
            with open(a, encoding="utf-8") as fh, \
                    open(audit_all, "a", encoding="utf-8") as out_fh:
                for line in fh:
                    out_fh.write(line)
            a.unlink()
        if i < args.rounds:
            log("  waiting for next 5m window...")
            time.sleep(30)

    # 最终汇总（从结果文件统计）
    trades = []
    settled = []
    for line in results_file.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "round":
            trades.extend(rec.get("trades", []))
        elif rec.get("type") == "trade_settled" and rec.get("pnl") is not None:
            settled.append(rec)
    wins = sum(1 for s in settled if float(s["win"] or 0) == 1)
    total = sum(float(s["pnl"]) for s in settled)
    log(f"\n=== SUMMARY: rounds={args.rounds} trades={len(trades)} "
        f"settled={len(settled)} win_rate={wins / max(len(settled), 1):.1%} "
        f"total_pnl=${total:+.2f}")
    emit({"type": "summary", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "rounds": args.rounds, "trades": len(trades), "settled": len(settled),
          "wins": wins, "total_pnl": round(total, 2)})
    log(f"log: {log_file}")
    log(f"results: {results_file}")
    log(f"audit: {audit_all}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
