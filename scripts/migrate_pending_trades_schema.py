"""pending_trades 表结构迁移：旧沙箱结构 → 项目 db.py ensure_schema 新结构。

背景（2026-08-15 实测）：旧环境遗留的表为旧结构
（window varchar(8)/slug varchar(80)/mode varchar(10) 等），而项目
ensure_schema 用 CREATE TABLE IF NOT EXISTS，已存在的旧表不会被迁移——
导致 'copytrade' 被截断为 'copytrad'、长 slug 截断等数据损坏。

本脚本幂等：逐列对比 information_schema 与目标定义，仅 ALTER 有差异的列，
不丢失已有数据。迁移后 settle_worker / 跟单循环均可正常写入。

用法: .venv/bin/python scripts/migrate_pending_trades_schema.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader import db

# 目标列定义（与 db.py ensure_schema 一致）
TARGET_COLUMNS = {
    "slug": "VARCHAR(160) NOT NULL",
    "coin": "VARCHAR(32)",
    "window": "VARCHAR(16)",
    "entry_price": "DECIMAL(12,6)",
    "size_usd": "DECIMAL(14,4)",
    "mode": "VARCHAR(16) DEFAULT 'simulate'",
    "order_id": "VARCHAR(80)",
    "order_status": "VARCHAR(32)",
    "fill_price": "DECIMAL(12,6)",
    "fill_tx": "VARCHAR(80)",
    "llm_p": "DECIMAL(8,4)",
    "ref_price": "DECIMAL(12,6)",
    "edge": "DECIMAL(8,4)",
    "llm_model": "VARCHAR(64)",
    "status": "VARCHAR(16) DEFAULT 'pending'",
    "pnl": "DECIMAL(14,4)",
}

# 长度差异导致截断风险的列（历史截断数据可在此修正）
TRUNCATED_VALUES = {"window": {"copytrad": "copytrade"}}


def main() -> int:
    conn = db.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SHOW COLUMNS FROM pending_trades")
            existing = {r["Field"]: r["Type"].upper() for r in cur.fetchall()}

            altered = 0
            for col, target in TARGET_COLUMNS.items():
                if col not in existing:
                    print(f"  [skip] {col} 不存在（跳过，由 ensure_schema 兜底）")
                    continue
                cur_type = existing[col]
                target_type = target.split(" ")[0].split("(")[0]
                # 仅当类型或宽度有实质差异时 ALTER
                if cur_type.startswith(target_type) and cur_type != target.split(" ")[0]:
                    print(f"  [alter] {col}: {cur_type} -> {target}")
                    cur.execute(f"ALTER TABLE pending_trades MODIFY `{col}` {target}")
                    altered += 1
                else:
                    print(f"  [ok] {col}: {cur_type} 无需变更")

            # 修正历史截断数据
            fixed = 0
            for col, mapping in TRUNCATED_VALUES.items():
                for bad, good in mapping.items():
                    cur.execute(
                        f"UPDATE pending_trades SET `{col}`=%s WHERE `{col}`=%s",
                        (good, bad))
                    fixed += cur.rowcount
            if fixed:
                print(f"  [fix] 修正截断数据 {fixed} 行")

            print(f"\n迁移完成：ALTER {altered} 列，修正 {fixed} 行")
            return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
