"""PolyTrader LLM updown 守护进程：持续性后台执行 + 统一日志目录。

用法:
    .venv/bin/python scripts/run_daemon.py start [--rounds N] [--scan-interval 60] ...
    .venv/bin/python scripts/run_daemon.py stop
    .venv/bin/python scripts/run_daemon.py status

设计:
- 无限循环执行 run_llm_loop 主体逻辑（--rounds 0 = 无限，直到 stop）
- 每轮异常自动重启（连续失败指数退避，最多 300s）
- 所有日志/结果/审计写入统一目录 logs/llm_daemon_<ts>/：
    daemon.log      守护进程心跳 + 每轮摘要（一个地方看全部日志）
    llm_results.jsonl  结果事件流（round/trade_settled/summary）
    audit_all.jsonl    调用级审计
    status.json        最新状态快照（供 web 面板读取）
- PID 文件 logs/llm_daemon.pid；SIGTERM/SIGINT 优雅停止
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
PID_FILE = LOGS_DIR / "llm_daemon.pid"


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def log(msg: str, fh=None):
    line = f"{_now()} {msg}"
    print(line, flush=True)
    if fh:
        fh.write(line + "\n")
        fh.flush()


def _read_pid() -> int | None:
    if PID_FILE.exists():
        try:
            return int(PID_FILE.read_text().strip())
        except ValueError:
            return None
    return None


def _alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def cmd_status():
    pid = _read_pid()
    if _alive(pid):
        print(f"daemon RUNNING pid={pid} (pid file: {PID_FILE})")
        # 展示最新状态快照
        snaps = sorted(LOGS_DIR.glob("llm_daemon_*/status.json"),
                       key=lambda p: p.stat().st_mtime)
        if snaps:
            st = json.loads(snaps[-1].read_text())
            print(f"  session: {st.get('session_dir')}")
            print(f"  rounds: {st.get('rounds')}  trades: {st.get('trades')}  "
                  f"settled: {st.get('settled')}  win_rate: {st.get('win_rate')}  "
                  f"pnl: ${st.get('total_pnl')}")
            print(f"  last_event: {st.get('ts')}  last_error: {st.get('last_error')}")
    else:
        if pid:
            print(f"daemon NOT running (stale pid {pid})")
        else:
            print("daemon NOT running (no pid file)")
    return 0


def cmd_stop():
    pid = _read_pid()
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _alive(pid):
                PID_FILE.unlink(missing_ok=True)
                print(f"daemon stopped (pid {pid})")
                return 0
            time.sleep(0.2)
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        print(f"daemon killed (pid {pid})")
        return 0
    if pid:
        PID_FILE.unlink(missing_ok=True)   # 清理 stale pid 文件
    print("daemon not running")
    return 1


def cmd_start(args) -> int:
    if _alive(_read_pid()):
        print(f"daemon already running pid={_read_pid()}; stop first or use status")
        return 1
    # 以守护方式启动：fork 后父进程退出
    pid = os.fork()
    if pid > 0:
        print(f"daemon started pid={pid}")
        return 0

    # ---- 子进程：守护循环 ----
    os.setsid()
    PID_FILE.write_text(str(os.getpid()))
    session_dir = LOGS_DIR / f"llm_daemon_{time.strftime('%Y%m%d_%H%M%S')}"
    session_dir.mkdir(parents=True, exist_ok=True)
    log_file = session_dir / "daemon.log"
    # 重定向 stdout/stderr 到 daemon.log（避免继承父管道导致 WaitDelay/阻塞）
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 0)
    log_fd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    os.close(devnull)
    os.close(log_fd)

    results_file = session_dir / "llm_results.jsonl"
    audit_all = session_dir / "audit_all.jsonl"
    status_file = session_dir / "status.json"
    run_dir = session_dir / "rounds_tmp"
    run_dir.mkdir(exist_ok=True)

    stop_flag = {"stop": False}

    def _sigterm(signum, frame):
        stop_flag["stop"] = True
        log("SIGTERM received, stopping after current round...")

    signal.signal(signal.SIGTERM, _sigterm)
    signal.signal(signal.SIGINT, _sigterm)

    log(f"daemon session started (pid {os.getpid()})")
    log(f"session dir: {session_dir}")

    def emit(rec: dict):
        with open(results_file, "a", encoding="utf-8") as rf:
            rf.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def write_status(**extra):
        stats = dict(session_dir=str(session_dir.name), ts=_now(), **extra)
        status_file.write_text(json.dumps(stats, ensure_ascii=False))

    # 汇总 helper：从 results 文件统计（供心跳/status）
    def tally():
        trades, settled, wins = 0, 0, 0
        for line in results_file.read_text().splitlines() if results_file.exists() else []:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "round":
                trades += len(rec.get("trades", []))
            elif rec.get("type") == "trade_settled":
                settled += 1
                if rec.get("win") in (1, "1"):
                    wins += 1
        total = 0.0
        for line in results_file.read_text().splitlines() if results_file.exists() else []:
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("type") == "trade_settled" and rec.get("pnl") is not None:
                total += float(rec["pnl"])
        return trades, settled, wins, total

    # ---- 主循环：每轮 = run_llm_loop 一个 round（窗口内高频扫描逻辑复用）----
    round_no = 0
    consecutive_fail = 0
    while not stop_flag["stop"]:
        if args.max_rounds and round_no >= args.max_rounds:
            log(f"reached max_rounds={args.max_rounds}, exiting")
            break
        round_no += 1
        try:
            log(f"=== ROUND {round_no} ===")
            # 复用 run_llm_loop（--rounds 1：当前窗口内多次扫描→开单→收尾；
            # 结算由常驻 settle_worker 处理，本 daemon 不等待结算）
            cmd = [sys.executable, "scripts/run_llm_loop.py",
                   "--rounds", "1", "--min-edge", str(args.min_edge),
                   "--windows", args.windows, "--scan-interval", str(args.scan_interval),
                   "--size", str(args.size),
                   "--out-dir", str(session_dir)]
            proc = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True,
                                  timeout=3600)
            # 合并轮次结果到统一 results 文件
            for line in proc.stdout.splitlines():
                log("  " + line)
            if proc.stderr.strip():
                for line in proc.stderr.splitlines()[-10:]:
                    log("  ERR " + line)
            if proc.returncode != 0:
                consecutive_fail += 1
                log(f"  round failed rc={proc.returncode}")
            else:
                consecutive_fail = 0
            # 把 run_llm_loop 生成的 results 并入统一文件
            for rf in session_dir.glob("llm_results_*.jsonl"):
                if rf.name == "llm_results.jsonl":
                    continue
                with open(rf, encoding="utf-8") as src, \
                        open(results_file, "a", encoding="utf-8") as dst:
                    for line in src:
                        dst.write(line)
                rf.unlink()
            for af in session_dir.glob("audit_all_*.jsonl"):
                if af.name == "audit_all.jsonl":
                    continue
                with open(af, encoding="utf-8") as src, \
                        open(audit_all, "a", encoding="utf-8") as dst:
                    for line in src:
                        dst.write(line)
                af.unlink()
            trades, settled, wins, total = tally()
            write_status(rounds=round_no, trades=trades, settled=settled,
                         wins=wins, win_rate=f"{wins / max(settled, 1):.1%}",
                         total_pnl=round(total, 2),
                         last_error=None if proc.returncode == 0
                         else f"round {round_no} rc={proc.returncode}")
            log(f"  [status] trades={trades} settled={settled} "
                f"win_rate={wins / max(settled, 1):.1%} pnl=${total:+.2f}")
            # 心跳
            emit({"type": "heartbeat", "ts": _now(), "round": round_no,
                  "trades": trades, "settled": settled,
                  "total_pnl": round(total, 2)})
            time.sleep(10)
        except Exception as e:
            consecutive_fail += 1
            backoff = min(300, 10 * (2 ** min(consecutive_fail - 1, 5)))
            log(f"  EXCEPTION: {e} (retry in {backoff}s, fail#{consecutive_fail})")
            write_status(last_error=str(e))
            time.sleep(backoff)

    log(f"daemon exiting after {round_no} rounds")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("action", choices=["start", "stop", "status"])
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--windows", type=str, default="5m")
    ap.add_argument("--scan-interval", type=int, default=30)
    ap.add_argument("--settle-wait", type=int, default=180,
                    help="[已弃用] 结算已由常驻 settle_worker 处理，此参数不再生效")
    ap.add_argument("--size", type=float, default=1.0)
    ap.add_argument("--max-rounds", type=int, default=0,
                    help="0=无限（默认），>0 跑完自动退出（测试用）")
    args = ap.parse_args()
    if args.action == "status":
        return cmd_status()
    if args.action == "stop":
        return cmd_stop()
    return cmd_start(args)


if __name__ == "__main__":
    sys.exit(main())
