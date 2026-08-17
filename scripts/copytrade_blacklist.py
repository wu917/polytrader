#!/usr/bin/env python
"""跟单大牛钱包黑名单维护 + per-wallet 画像查询。

黑名单（用户手动维护，扫描时剔除）：
  add    <wallet> [reason]   拉黑（幂等）
  remove <wallet>            移出黑名单
  list                       查看黑名单

画像（pending_trades.mirror_wallet 聚合，跟谁赚/跟谁亏）：
  stats [wallet]             全部/指定钱包战绩（笔数/胜率/pnl）

示例：
  .venv/bin/python scripts/copytrade_blacklist.py add 0xfe787d2d... "5笔40%胜率持续亏"
  .venv/bin/python scripts/copytrade_blacklist.py stats
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader import db  # noqa: E402


def _norm(w: str) -> str:
    w = w.strip()
    if not w.lower().startswith("0x"):
        print(f"!! 钱包地址须 0x 开头: {w}")
        sys.exit(2)
    return w.lower()


def cmd_add(wallet: str, reason: str) -> None:
    w = _norm(wallet)
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO copytrade_wallet_blacklist (wallet, reason) "
                "VALUES (%s, %s) ON DUPLICATE KEY UPDATE reason=%s",
                (w, reason, reason))
        conn.commit()
        print(f"✅ 已拉黑 {w}（reason: {reason}）——下轮扫描即剔除其信号")
    finally:
        conn.close()


def cmd_remove(wallet: str) -> None:
    w = _norm(wallet)
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM copytrade_wallet_blacklist WHERE wallet=%s", (w,))
        conn.commit()
        print(f"✅ 已移出黑名单 {w}（影响行数 {cur.rowcount}）")
    finally:
        conn.close()


def cmd_list() -> None:
    bl = db.fetch_wallet_blacklist()
    if not bl:
        print("黑名单为空（copytrade_wallet_blacklist 表）")
        return
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT wallet, reason, created_at "
                        "FROM copytrade_wallet_blacklist ORDER BY created_at")
            print(f"黑名单 {len(bl)} 个:")
            for r in cur.fetchall():
                print(f"  {r['wallet']} | {str(r['created_at'])[:16]} | {r['reason'] or ''}")
    finally:
        conn.close()


def cmd_stats(wallet: str | None) -> None:
    """per-wallet 画像：已结算跟单单按来源钱包聚合（笔数/胜率/pnl）。"""
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            sql = ("SELECT mirror_wallet w, COUNT(*) n, COALESCE(SUM(win),0) wins, "
                   "COALESCE(SUM(pnl),0) pnl FROM pending_trades "
                   "WHERE mode='live' AND `window`='copytrade' "
                   "AND status='settled' AND mirror_wallet IS NOT NULL ")
            args_ = []
            if wallet:
                sql += "AND mirror_wallet=%s "
                args_.append(_norm(wallet))
            sql += "GROUP BY mirror_wallet ORDER BY pnl DESC"
            cur.execute(sql, args_)
            rows = cur.fetchall()
            if not rows:
                print("无画像数据（需跟单单带 mirror_wallet 入库后积累）")
                return
            tot_n = sum(r["n"] for r in rows)
            tot_p = sum(float(r["pnl"]) for r in rows)
            print(f"per-wallet 画像（已结算 {tot_n} 笔，合计 {tot_p:+.2f}）:")
            for r in rows:
                n, wins = r["n"], int(r["wins"])
                print(f"  {str(r['w'])[:12]}  {n:3}笔 "
                      f"胜{wins}/{n}({wins / max(n, 1) * 100:.0f}%) "
                      f"{float(r['pnl']):+8.2f}")
    finally:
        conn.close()


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cmd = sys.argv[1]
    if cmd == "add" and len(sys.argv) >= 3:
        cmd_add(sys.argv[2], " ".join(sys.argv[3:]) or "")
    elif cmd == "remove" and len(sys.argv) >= 3:
        cmd_remove(sys.argv[2])
    elif cmd == "list":
        cmd_list()
    elif cmd == "stats":
        cmd_stats(sys.argv[2] if len(sys.argv) >= 3 else None)
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
