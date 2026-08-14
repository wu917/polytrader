"""常驻结算进程：持续扫描 MySQL pending 队列，结算完成的单子写回结果文件。

与主任务（run_llm_loop / run_daemon）解耦：
- 主任务只负责开单并写入 MySQL（polytrader.pending_trades 表，多进程共享）
- 本进程独立持续处理结算 —— 主任务退出不影响结算（任务停止后结算不停止）
- 无需手动跑 backfill_settlements.py

用法:
    .venv/bin/python scripts/settle_worker.py start [--poll 30]   # 后台常驻
    .venv/bin/python scripts/settle_worker.py status               # 状态 + pending 数
    .venv/bin/python scripts/settle_worker.py stop                 # 优雅停止
    .venv/bin/python scripts/settle_worker.py run  [--poll 30]     # 前台调试运行
"""
import argparse
import importlib.util
import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = ROOT / "logs"
PID_FILE = ROOT / "settle_worker.pid"
LOG_FILE = LOGS_DIR / "settle_worker.log"

from polytrader import db

# 复用 backfill_settlements.py 的结算查询（单一实现，避免重复拷贝）
_spec = importlib.util.spec_from_file_location(
    "backfill_settlements", ROOT / "scripts" / "backfill_settlements.py")
_backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_backfill)
fetch_settle = _backfill.fetch_settle


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def log(msg: str):
    line = f"{_now()} {msg}"
    print(line, flush=True)
    LOGS_DIR.mkdir(exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")


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


# ---- pending 队列（MySQL polytrader.pending_trades，多进程共享）----
def settle_one(rec: dict) -> bool:
    """结算一条 pending：查到结算则写回结果文件并更新 DB 状态。返回是否已结算。"""
    s = fetch_settle(rec["slug"])
    if s is None:
        return False
    # DB DECIMAL 列读出为 Decimal，统一转 float 参与运算
    size_usd = float(rec["size_usd"])
    # 结算成本价口径：优先实际成交价 fill_price（live 吃单加价/滑点），
    # 无则回退 entry_price（simulate 的 entry_price 即成交价）
    entry_price = float(rec.get("fill_price") or rec["entry_price"])
    win = (rec["side"] == "YES" and s == 1.0) or \
          (rec["side"] == "NO" and s == 0.0)
    pnl = round((size_usd / entry_price) * (1.0 if win else 0.0)
                - size_usd, 2)
    event = {"type": "trade_settled", "round": rec.get("round"),
             "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
             "trade_id": rec["trade_id"], "slug": rec["slug"],
             "coin": rec.get("coin"), "window": rec.get("window"),
             "side": rec["side"], "entry_price": entry_price,
             "size_usd": size_usd, "settle_yes": s,
             "win": 1 if win else 0, "pnl": pnl, "backfilled": True}
    # 写回结果文件：优先原结果文件；被 daemon 合并删除后降级到同目录统一文件
    target = Path(rec["results_file"]) if rec.get("results_file") else None
    if target and not target.exists():
        alt = target.with_name("llm_results.jsonl")
        target = alt if alt.exists() else None
    if target is None:
        log(f"  [settle] {rec['slug']}: settled but results file gone — "
            f"event: {json.dumps(event, ensure_ascii=False)}")
        return True   # 仍视为已处理，避免无限重试
    with open(target, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    # 更新 DB 状态为 settled
    try:
        db.mark_settled(rec["trade_id"], s, 1 if win else 0, pnl)
    except Exception as e:
        log(f"  [db] mark_settled FAILED for {rec['trade_id']}: {e}")
    log(f"  [settle] {rec['slug']}: settle_yes={s} win={win} pnl=${pnl:+.2f} "
        f"-> {target.name}")
    return True


def run_forever(poll: int):
    log("settle worker running (poll every %ds, pending: MySQL "
        "polytrader.pending_trades)" % poll)
    while True:
        try:
            recs = db.fetch_pending()
        except Exception as e:
            log(f"  [db] fetch_pending FAILED: {e}")
            time.sleep(poll)
            continue
        done = 0
        for rec in recs:
            try:
                if settle_one(rec):
                    done += 1
            except Exception as e:
                log(f"  [settle] {rec['slug']} error: {e}")
        if done:
            log(f"  settled {done}, pending left: {db.count_pending()}")
        time.sleep(poll)


def cmd_status():
    pid = _read_pid()
    try:
        n = db.count_pending()
    except Exception as e:
        n = f"DB error: {e}"
    if _alive(pid):
        print(f"settle_worker RUNNING pid={pid} (pid file: {PID_FILE})")
    else:
        if pid:
            print(f"settle_worker NOT running (stale pid {pid})")
        else:
            print("settle_worker NOT running (no pid file)")
    print(f"pending trades: {n} (MySQL polytrader.pending_trades)")
    return 0


def cmd_stop():
    pid = _read_pid()
    if _alive(pid):
        os.kill(pid, signal.SIGTERM)
        for _ in range(50):
            if not _alive(pid):
                PID_FILE.unlink(missing_ok=True)
                print(f"settle_worker stopped (pid {pid})")
                return 0
            time.sleep(0.2)
        os.kill(pid, signal.SIGKILL)
        PID_FILE.unlink(missing_ok=True)
        print(f"settle_worker killed (pid {pid})")
        return 0
    if pid:
        PID_FILE.unlink(missing_ok=True)
    print("settle_worker not running")
    return 1


def cmd_start(args) -> int:
    if _alive(_read_pid()):
        print(f"settle_worker already running pid={_read_pid()}")
        return 1
    pid = os.fork()
    if pid > 0:
        print(f"settle_worker started pid={pid}")
        return 0
    os.setsid()
    # 脱离调用方 stdio，日志走 settle_worker.log（避免 fork 后管道挂住）
    LOGS_DIR.mkdir(exist_ok=True)
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)
    log_fd = os.open(LOG_FILE, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    os.dup2(log_fd, 1)
    os.dup2(log_fd, 2)
    PID_FILE.write_text(str(os.getpid()))
    try:
        run_forever(args.poll)
    except KeyboardInterrupt:
        pass
    finally:
        PID_FILE.unlink(missing_ok=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="action")
    p = sub.add_parser("start")
    p.add_argument("--poll", type=int, default=30)
    sub.add_parser("status")
    sub.add_parser("stop")
    r = sub.add_parser("run")
    r.add_argument("--poll", type=int, default=30)
    args = ap.parse_args()
    if args.action == "status":
        return cmd_status()
    if args.action == "stop":
        return cmd_stop()
    if args.action == "start":
        return cmd_start(args)
    if args.action == "run":
        run_forever(args.poll)
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
