"""LLM updown 挂机循环：连续 N 轮模拟测算（subprocess 逐轮，天然跟随新窗口）。

每轮 = 一个 5m/15m 窗口：
- 不对齐整点：启动后立即在当前窗口内扫描（残余窗口照常扫描，窗口时间戳变化即新窗口）
- 窗口内每 --scan-interval 秒（默认 30s）扫描一次，窗口结束前 --stop-before 秒（默认 40s）停止
- 开单统一写入本地 MySQL（polytrader.pending_trades 表，多进程共享）
- 结算由常驻进程 scripts/settle_worker.py 独立处理（本进程启动时自动拉起，退出不影响结算）

用法: .venv/bin/python scripts/run_llm_loop.py --rounds 3 [--min-edge 0.04]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

from polytrader import db

SETTLE_PID_FILE = ROOT / "settle_worker.pid"


def _settle_worker_alive() -> bool:
    pid = None
    if SETTLE_PID_FILE.exists():
        try:
            pid = int(SETTLE_PID_FILE.read_text().strip())
        except ValueError:
            return False
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_settle_worker():
    """结算常驻进程未运行时自动拉起（start 内部 fork，立即返回）。"""
    if _settle_worker_alive():
        return
    subprocess.run([sys.executable, "scripts/settle_worker.py", "start"],
                   cwd=ROOT, capture_output=True, text=True, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--size", type=float, default=1.0,
                    help="每笔固定仓位 USD（透传 simulate，默认 $1）")
    ap.add_argument("--out-dir", type=str, default="backtest_results",
                    help="日志/结果/审计输出目录（pending 队列为全局唯一，不在此目录）")
    ap.add_argument("--windows", type=str, default="5m,15m",
                    help="参与的市场窗口（透传 simulate）")
    ap.add_argument("--scan-interval", type=int, default=30,
                    help="窗口内扫描间隔秒（默认 30；0=每窗口仅扫 1 次）")
    ap.add_argument("--stop-before", type=int, default=40,
                    help="窗口结束前 N 秒停止该窗口的扫描（默认 40）")
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
    # 结果文件预先创建：即使某轮 scan 全部失败（无事件），backfill/汇总也能读
    results_file.touch()

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

    def run_simulate(wait: int) -> subprocess.CompletedProcess | None:
        """启动一轮 simulate（wait=0 快速扫描只开单；>0 等待结算）。

        超时（网络抖动导致子进程卡住）视为该次扫描失败返回 None，
        由主循环跳过收集继续——不允许整体任务崩溃。
        """
        try:
            return subprocess.run(
                [sys.executable, "scripts/simulate_llm_updown.py",
                 "--wait", str(wait), "--min-edge", str(args.min_edge),
                 "--size", str(args.size), "--audit-dir", str(run_dir),
                 "--seen-file", str(out_dir / "seen_slugs.txt"),
                 "--windows", args.windows],
                cwd=ROOT, capture_output=True, text=True,
                timeout=max(wait, 60) + 240)
        except subprocess.TimeoutExpired:
            log(f"  simulate timed out after {max(wait, 60) + 240}s "
                f"(network stall) — treated as failed scan")
            return None

    def add_pending(round_no: int, trades: list[dict]):
        """开单入库（MySQL polytrader.pending_trades，INSERT IGNORE 天然幂等）。"""
        new = []
        for t in trades:
            tid = t.get("trade_id")
            if not tid:
                continue
            new.append({k: t.get(k) for k in (
                "trade_id", "slug", "coin", "window",
                "side", "entry_price", "size_usd")} | {
                "round": round_no,
                "results_file": str(results_file)})
        if not new:
            return
        try:
            n = db.insert_pending(new)
        except Exception as e:
            log(f"  [db] insert_pending FAILED: {e}")
            return
        log(f"  [db] {n} trade(s) inserted -> polytrader.pending_trades")

    def collect_round(i: int):
        """收集本轮 simulate 产物：结果 JSONL 事件 + 审计合并 + 开单并入 pending。"""
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
            add_pending(i, d.get("trades", []))
            sim.unlink()
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
        for a in sorted(run_dir.glob("audit_llm_*.jsonl")):
            with open(a, encoding="utf-8") as fh, \
                    open(audit_all, "a", encoding="utf-8") as out_fh:
                for line in fh:
                    out_fh.write(line)
            a.unlink()

    # 结算常驻进程：启动时确保在跑（主任务退出后由它继续结算）
    ensure_settle_worker()
    log("settle worker ensured (pending storage: MySQL polytrader.pending_trades)")

    # ---- 主循环：每轮 = 一个窗口（当前窗口直接扫描，不对齐整点）----
    win_secs = 300 if "5m" in args.windows else 900
    for i in range(1, args.rounds + 1):
        log(f"=== LOOP {i}/{args.rounds} ===")
        now = int(time.time())
        w_start = (now // win_secs) * win_secs     # 当前窗口起点（不做对齐等待）
        w_end = w_start + win_secs
        log(f"  window {w_start} -> {w_end} (enter mid-window at {now})")

        scans = 0
        while True:
            now = int(time.time())
            if now > w_end - args.stop_before:     # 窗口结束前 stop_before 秒停止
                log(f"  window ends in {w_end - now}s, stop scanning "
                    f"({scans} scans done)")
                break
            scans += 1
            log(f"  scan {scans} @ {time.strftime('%H:%M:%S')}")
            proc = run_simulate(0)                 # 快速扫描：只开单不等待
            if proc is None:
                continue                            # 超时视为本次扫描失败，继续下一轮
            for line in proc.stdout.splitlines():
                log("  " + line)
            if proc.stderr.strip():
                for line in proc.stderr.splitlines()[-20:]:
                    log("  ERR " + line)
            if proc.returncode != 0:
                log(f"  scan failed rc={proc.returncode}")
            collect_round(i)
            if args.scan_interval <= 0:
                break
            time.sleep(args.scan_interval)

        # 窗口结束：睡到下一个窗口开始（结算由常驻 settle_worker 处理，不在此阻塞）
        sleep_to = w_end + 2
        d = sleep_to - int(time.time())
        if d > 0:
            log(f"  window over, waiting {d}s for next window")
            time.sleep(d)

    # ---- 收尾：结算不等待，交给常驻进程；本进程退出不影响结算 ----
    try:
        n_pending = db.count_pending()
    except Exception as e:
        log(f"  [db] count_pending FAILED: {e}")
        n_pending = -1
    if n_pending > 0:
        log(f"  {n_pending} trade(s) still pending — settle_worker (pid file "
            f"{SETTLE_PID_FILE}) will keep settling after this task exits; "
            f"events appended to {results_file}")
    else:
        log("  no pending trades")

    # 最终汇总（从结果文件统计；结算事件由 settle_worker 后续追加）
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
        f"total_pnl=${total:+.2f} (settled so far; pending handled by settle_worker)")
    emit({"type": "summary", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "rounds": args.rounds, "trades": len(trades), "settled": len(settled),
          "wins": wins, "total_pnl": round(total, 2)})
    log(f"log: {log_file}")
    log(f"results: {results_file}")
    log(f"audit: {audit_all}")
    log(f"pending: MySQL polytrader.pending_trades ({n_pending} trades)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
