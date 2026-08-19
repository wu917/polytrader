#!/usr/bin/env python
"""飞书通知 worker：每 2 小时发送交易状态报告。

报告内容：
- 当前持仓（copytrade + daily 的 live pending）
- 资金余额（deposit wallet pUSD）
- 跟单各钱包最新画像（已结算：笔数/胜率/pnl）
- 对比上一次：持仓变化 / 资金变化 / 钱包画像变化 / 新结算

webhook URL 从 .env 读取（FEISHU_WEBHOOK_URL），不落代码。

用法：
  .venv/bin/python scripts/notify_worker.py start|status|stop
  .venv/bin/python scripts/notify_worker.py once        # 立即生成并发送一条
  .venv/bin/python scripts/notify_worker.py once --dry  # 只生成不发送
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

PID_FILE = ROOT / "logs" / "notify_worker.pid"
STATE_FILE = ROOT / "logs" / "notify_state.json"
INTERVAL = 7200  # 2 小时
MAX_TEXT = 4000  # 飞书 text 上限（超长截断）


# ---------- 数据采集 ----------

def get_balance() -> float | None:
    """deposit wallet pUSD 余额。"""
    try:
        from polytrader.execution import chain
        dep = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "").strip()
        if not dep:
            return None
        return chain.call_balance(chain.PUSD, dep) / 1e6
    except Exception:  # noqa: BLE001
        return None


def get_positions() -> list[dict]:
    """当前 live pending 持仓（copytrade + daily）。"""
    try:
        from polytrader import db
        conn = db.connect()
        with conn.cursor() as cur:
            cur.execute("""SELECT slug, `window`, side, fill_price, mirror_wallet,
                           account, created_at FROM pending_trades
                           WHERE mode='live' AND status='pending'
                           ORDER BY `window`, created_at""")
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


def get_wallet_stats() -> list[dict]:
    """跟单钱包画像：已结算按 mirror_wallet 聚合。"""
    try:
        from polytrader import db
        conn = db.connect()
        with conn.cursor() as cur:
            cur.execute("""SELECT mirror_wallet w, COUNT(*) n,
                           COALESCE(SUM(win),0) wins, COALESCE(SUM(pnl),0) pnl
                           FROM pending_trades
                           WHERE mode='live' AND `window`='copytrade'
                           AND status='settled' AND mirror_wallet IS NOT NULL
                           GROUP BY mirror_wallet ORDER BY pnl DESC""")
            rows = cur.fetchall()
        conn.close()
        return [{"wallet": str(r["w"]), "n": r["n"],
                 "wins": int(r["wins"]), "pnl": float(r["pnl"] or 0)}
                for r in rows]
    except Exception:  # noqa: BLE001
        return []


def get_recent_settled(since_ts: float) -> list[dict]:
    """自 since_ts 以来的新结算（对比用）。"""
    try:
        from polytrader import db
        conn = db.connect()
        with conn.cursor() as cur:
            cur.execute("""SELECT slug, `window`, side, win, pnl, settled_at
                           FROM pending_trades
                           WHERE mode='live' AND status='settled'
                           AND settled_at IS NOT NULL
                           AND UNIX_TIMESTAMP(settled_at) > %s
                           ORDER BY settled_at""", (since_ts,))
            rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []


# ---------- 报告生成 ----------

def _fmt_slug(s: str, n: int = 40) -> str:
    return s[:n]


def build_report() -> dict:
    """生成报告（含与上次的对比）。"""
    bal = get_balance()
    positions = get_positions()
    stats = get_wallet_stats()

    # 上次状态
    prev: dict = {}
    if STATE_FILE.exists():
        try:
            prev = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            prev = {}

    lines = []
    lines.append(f"⏰ PolyTrader 状态报告 {time.strftime('%m-%d %H:%M')}")

    # 资金
    prev_bal = prev.get("balance")
    bal_txt = "--" if bal is None else f"${bal:.2f}"
    if prev_bal is not None and bal is not None:
        diff = bal - prev_bal
        bal_txt += f"（{'↑' if diff > 0 else '↓' if diff < 0 else '='}{diff:+.2f}）"
    lines.append(f"\n💰 资金余额: {bal_txt}")

    # 持仓
    prev_pos = prev.get("positions", [])
    prev_keys = {(p.get("slug", ""), p.get("window", "")) for p in prev_pos}
    now_keys = {(p.get("slug", ""), p.get("window", "")) for p in positions}
    opened = now_keys - prev_keys
    closed = prev_keys - now_keys
    if positions:
        lines.append(f"\n📦 当前持仓 {len(positions)} 笔:")
        for p in positions:
            w = p.get("window", "?")
            fill = p.get("fill_price")
            fill_txt = f"@{fill}" if fill is not None else ""
            lines.append(f"  [{w}] {_fmt_slug(p.get('slug', ''))} "
                         f"{p.get('side', '?')} {fill_txt} "
                         f"({str(p.get('created_at'))[5:16]})")
    else:
        lines.append("\n📦 当前持仓: 无")
    if opened:
        lines.append(f"  🆕 新开 {len(opened)} 笔")
    if closed:
        lines.append(f"  ✅ 平仓 {len(closed)} 笔")

    # 钱包画像
    prev_stats = {s.get("wallet"): s for s in prev.get("wallet_stats", [])}
    if stats:
        lines.append(f"\n👛 钱包画像（已结算）:")
        for s in stats:
            w = s["wallet"][:10]
            n, wins = s["n"], s["wins"]
            rate = wins / n * 100 if n else 0
            pnl = s["pnl"]
            pv = prev_stats.get(s["wallet"])
            chg = ""
            if pv:
                d = pnl - float(pv.get("pnl", 0))
                if abs(d) > 0.001:
                    chg = f"（{'+' if d > 0 else ''}{d:+.2f}）"
            lines.append(f"  {w} {n:>3}笔 胜{wins}/{n}({rate:.0f}%) "
                         f"{pnl:+.2f}{chg}")
    else:
        lines.append("\n👛 钱包画像: 暂无已结算")

    # 新结算
    since_ts = prev.get("ts", 0)
    new_settled = get_recent_settled(since_ts) if since_ts else []
    if new_settled:
        lines.append(f"\n✅ 新增结算 {len(new_settled)} 笔:")
        for r in new_settled[:15]:
            lines.append(f"  {_fmt_slug(r.get('slug', ''))} "
                         f"{'胜' if r.get('win') else '负'} "
                         f"{float(r.get('pnl') or 0):+.2f}")
        if len(new_settled) > 15:
            lines.append(f"  ...共 {len(new_settled)} 笔")

    text = "\n".join(lines)
    report = {
        "ts": time.time(),
        "balance": bal,
        "positions": positions,
        "wallet_stats": stats,
        "text": text[:MAX_TEXT],
    }
    return report


# ---------- 发送 ----------

def send(report: dict, dry: bool = False) -> bool:
    """POST 飞书 webhook（text 消息）。返回是否成功。"""
    url = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not url:
        print("!! .env 缺 FEISHU_WEBHOOK_URL")
        return False
    payload = {"msg_type": "text", "content": {"text": report["text"]}}
    if dry:
        print("---- DRY（不发送）----")
        print(report["text"])
        return True
    try:
        import requests
        proxies = {"http": "http://127.0.0.1:7897",
                   "https": "http://127.0.0.1:7897"}
        r = requests.post(url, json=payload, proxies=proxies, timeout=15)
        ok = r.status_code == 200 and r.json().get("code") == 0
        print(f"发送飞书: {r.status_code} {r.text[:120]}")
        return ok
    except Exception as e:  # noqa: BLE001
        print(f"!! 发送失败: {e}")
        return False


def save_state(report: dict) -> None:
    """保存上次报告状态（对比用，存结构化字段）。"""
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(
            {"ts": report["ts"], "balance": report["balance"],
             "positions": report["positions"],
             "wallet_stats": report["wallet_stats"]},
            ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def run_once(dry: bool = False) -> int:
    """生成 + 发送一次（成功保存状态）。"""
    report = build_report()
    ok = send(report, dry=dry)
    if ok:
        save_state(report)
        print(f"状态已保存（ts={report['ts']:.0f}）")
    return 0 if ok else 1


def loop() -> None:
    """常驻：每 INTERVAL 秒发一次。"""
    print(f"notify worker: 每 {INTERVAL // 3600}h 发送一次飞书报告")
    while True:
        try:
            run_once()
        except Exception as e:  # noqa: BLE001
            print(f"!! 本轮失败: {e}")
        time.sleep(INTERVAL)


# ---------- 进程管理 ----------

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


def cmd_start() -> None:
    pid = _read_pid()
    if pid and _alive(pid):
        print(f"已运行（PID {pid}）")
        return
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "loop"],
        cwd=str(ROOT), start_new_session=True,
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
                    choices=["start", "status", "stop", "once", "loop"])
    ap.add_argument("--dry", action="store_true", help="只生成不发送")
    args = ap.parse_args()
    if args.cmd == "start":
        cmd_start()
    elif args.cmd == "status":
        cmd_status()
    elif args.cmd == "stop":
        cmd_stop()
    elif args.cmd == "once":
        return run_once(dry=args.dry)
    elif args.cmd == "loop":
        loop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
