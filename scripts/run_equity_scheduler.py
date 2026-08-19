#!/usr/bin/env python
"""股票/商品日级涨跌盘定时调度器（常驻）。

按 config/equity.yaml 的每日触发时刻（美东时间）自动跑一轮：
- live: false → subprocess 调 simulate_equity_updown.py（模拟成交入库）
- live: true  → subprocess 调 run_equity_live_loop.py（FOK 真实下单入库）
下单结果统一存 pending_trades（window='daily' + account），由 settle_worker 结算。

与 run_daemon 同模式：调度器只做定时触发，交易链路完全复用既有脚本。

config/equity.yaml 示例：
    schedule:
      timezone: America/New_York
      runs: ["09:30", "12:00", "14:30"]   # 美东每日触发时刻
    symbols: ["nvda","tsla","spy","aapl"] # 标的白名单（空=全部 17 个）
    account: default
    live: false
    size: 1.0
    min_edge: 0.05
    min_liquidity: 200
    per_run: 3

用法：
    .venv/bin/python scripts/run_equity_scheduler.py start|status|stop
    .venv/bin/python scripts/run_equity_scheduler.py run-once   # 立即触发一轮（调试）
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
PID_FILE = ROOT / "logs" / "equity_scheduler.pid"
DEFAULT_CONFIG = ROOT / "config" / "equity.yaml"


def load_equity_config(path: Path | None = None) -> dict:
    """加载 config/equity.yaml（缺失回退内置默认）。"""
    p = path or DEFAULT_CONFIG
    cfg: dict = {}
    if p.exists():
        try:
            cfg = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception as e:  # noqa: BLE001
            print(f"!! equity.yaml 解析失败: {e}")
    # schedule 子字段分别兜底（部分覆盖时不丢默认）
    sch = cfg.get("schedule") or {}
    cfg["schedule"] = {
        "timezone": str(sch.get("timezone") or "America/New_York"),
        "runs": [str(r) for r in (sch.get("runs") or ["09:30", "12:00", "14:30"])],
    }
    cfg.setdefault("symbols", [])
    cfg.setdefault("account", "default")
    cfg.setdefault("live", False)
    cfg.setdefault("size", 1.0)
    cfg.setdefault("min_edge", 0.05)
    cfg.setdefault("min_liquidity", 200.0)
    cfg.setdefault("per_run", 3)
    cfg.setdefault("max_markets", 10)
    return cfg


def build_cmd(cfg: dict) -> list[str]:
    """按配置构造单轮触发命令（复用既有脚本，不重复实现交易逻辑）。"""
    syms = ",".join(str(s) for s in (cfg.get("symbols") or []))
    acct = str(cfg.get("account") or "default")
    common = [
        str(ROOT / ".venv" / "bin" / "python"),
    ]
    if cfg.get("live"):
        script = str(ROOT / "scripts" / "run_equity_live_loop.py")
        cmd = [*common, script,
               "--size", str(cfg.get("size", 1.0)),
               "--min-edge", str(cfg.get("min_edge", 0.05)),
               "--min-liquidity", str(cfg.get("min_liquidity", 200)),
               "--per-round", str(cfg.get("per_run", 3)),
               "--account", acct,
               "--log", str(ROOT / "logs" / "equity_scheduler_run.log")]
    else:
        script = str(ROOT / "scripts" / "simulate_equity_updown.py")
        cmd = [*common, script,
               "--size", str(cfg.get("size", 1.0)),
               "--min-edge", str(cfg.get("min_edge", 0.05)),
               "--min-liquidity", str(float(cfg.get("min_liquidity", 200))),
               "--max-markets", str(cfg.get("max_markets", 10)),
               "--account", acct,
               "--log", str(ROOT / "logs" / "equity_scheduler_run.log")]
    if syms:
        cmd += ["--symbols", syms]
    return cmd


def trigger(cfg: dict, dry: bool = False) -> None:
    """触发一轮（subprocess，后台；超时保护）。"""
    cmd = build_cmd(cfg)
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    script = Path(cmd[1]).name
    args = " ".join(cmd[2:])
    print(f"[{ts}] trigger: {script} {args}")
    if dry:
        return
    try:
        subprocess.Popen(cmd, cwd=str(ROOT),
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:  # noqa: BLE001
        print(f"  !! 触发失败: {e}")


def _et_now(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))


def loop(cfg: dict, rounds: int = 0, dry: bool = False) -> None:
    """常驻循环：每 30s 检查是否到达配置的触发时刻（每日每时刻一次）。"""
    tz = cfg["schedule"]["timezone"]
    runs = sorted(set(cfg["schedule"]["runs"]))
    fired: set[str] = set()  # 已触发的 "YYYY-MM-DD HH:MM"
    print(f"scheduler loop: tz={tz} runs={runs} live={cfg['live']} "
          f"account={cfg['account']} symbols={cfg.get('symbols') or '全部'}")
    done_rounds = 0
    while True:
        now = _et_now(tz)
        slot = now.strftime("%H:%M")       # 触发时刻匹配（纯 HH:MM）
        fired_key = now.strftime("%Y-%m-%d %H:%M")  # 去重键（含日期）
        if slot in runs and fired_key not in fired:
            fired.add(fired_key)
            # 保留当日已触发记录即可（内存态；重启后同刻重触发有
            # pending_trades 去重兜底——同 slug 已有 pending 会被脚本跳过）
            trigger(cfg, dry=dry)
        if rounds > 0:
            done_rounds += 1
            if done_rounds >= rounds:
                break
        time.sleep(30)


def _write_pid(pid: int) -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(pid))


def _read_pid() -> int | None:
    try:
        return int(PID_FILE.read_text().strip())
    except Exception:  # noqa: BLE001
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def cmd_start(cfg: dict, rounds: int) -> None:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"已运行（PID {pid}）")
        return
    import subprocess as sp
    args = [sys.executable, str(Path(__file__).resolve()),
            "run", "--rounds", str(rounds)]
    proc = sp.Popen(args, cwd=str(ROOT), start_new_session=True,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _write_pid(proc.pid)
    print(f"started PID {proc.pid}")


def cmd_status() -> None:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"running（PID {pid}）")
    else:
        print("stopped")


def cmd_stop() -> None:
    pid = _read_pid()
    if pid and _alive(pid):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    if PID_FILE.exists():
        PID_FILE.unlink(missing_ok=True)
    print("stopped")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["start", "status", "stop", "run", "run-once"])
    ap.add_argument("--rounds", type=int, default=0,
                    help="run 模式限定轮数（0=无限）")
    ap.add_argument("--config", type=str, default=str(DEFAULT_CONFIG),
                    help="equity 配置文件路径")
    ap.add_argument("--dry", action="store_true", help="只打印触发命令不执行")
    args = ap.parse_args()
    cfg = load_equity_config(Path(args.config))

    if args.cmd == "start":
        cmd_start(cfg, args.rounds)
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "stop":
        cmd_stop()
    elif args.cmd == "run-once":
        trigger(cfg, dry=args.dry)
    elif args.cmd == "run":
        try:
            loop(cfg, rounds=args.rounds, dry=args.dry)
        except KeyboardInterrupt:
            print("\nstopped by user")
    return 0


if __name__ == "__main__":
    sys.exit(main())
