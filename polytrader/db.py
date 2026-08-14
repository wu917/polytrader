"""已开单队列的 MySQL 存储（替代 pending_trades.jsonl）。

连接参数从项目根 .env 读取（POLY_DB_*），也可用环境变量覆盖：
  POLY_DB_HOST / POLY_DB_PORT / POLY_DB_USER / POLY_DB_PASS / POLY_DB_NAME
凭证绝不硬编码在代码中。
"""
import os
from pathlib import Path

from dotenv import load_dotenv

# 加载项目根 .env（脚本可能从任意 cwd 启动，用绝对路径）
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import pymysql
import pymysql.cursors

DB_HOST = os.environ.get("POLY_DB_HOST", "127.0.0.1")
DB_PORT = int(os.environ.get("POLY_DB_PORT", "3306"))
DB_USER = os.environ.get("POLY_DB_USER", "root")
DB_PASS = os.environ.get("POLY_DB_PASS", "")
DB_NAME = os.environ.get("POLY_DB_NAME", "polytrader")


def connect():
    return pymysql.connect(
        host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASS,
        database=DB_NAME, charset="utf8mb4", autocommit=True,
        cursorclass=pymysql.cursors.DictCursor,
    )


_schema_checked = False  # 进程内只做一次建表检查


def ensure_schema() -> None:
    """幂等建表：pending_trades 不存在则创建（进程内只检查一次）。

    5m/15m 加密盘与 daily 股票/商品盘共用同一张表（window 区分）。
    CREATE TABLE IF NOT EXISTS 保证多进程并发启动安全。
    """
    global _schema_checked
    if _schema_checked:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS pending_trades (
                    trade_id     VARCHAR(32)  NOT NULL PRIMARY KEY,
                    slug         VARCHAR(160) NOT NULL,
                    coin         VARCHAR(32),
                    `window`     VARCHAR(16),
                    side         VARCHAR(8)   NOT NULL,
                    entry_price  DECIMAL(12,6),
                    size_usd     DECIMAL(14,4),
                    `round`      INT,
                    results_file VARCHAR(255),
                    mode         VARCHAR(16)  DEFAULT 'simulate',
                    order_id     VARCHAR(80),
                    order_status VARCHAR(32),
                    fill_price   DECIMAL(12,6),
                    fill_tx      VARCHAR(80),
                    llm_p        DECIMAL(8,4),
                    ref_price    DECIMAL(12,6),
                    edge         DECIMAL(8,4),
                    llm_reason   TEXT,
                    llm_model    VARCHAR(64),
                    status       VARCHAR(16)  DEFAULT 'pending',
                    settle_yes   DECIMAL(8,4),
                    win          TINYINT,
                    pnl          DECIMAL(14,4),
                    created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP,
                    settled_at   TIMESTAMP    NULL DEFAULT NULL,
                    INDEX idx_status (status),
                    INDEX idx_slug (slug)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """)
        _schema_checked = True
    finally:
        conn.close()


def insert_pending(recs: list[dict]) -> int:
    """批量插入待结算单（trade_id 已存在则忽略）。返回实际插入数。"""
    if not recs:
        return 0
    ensure_schema()
    sql = ("INSERT IGNORE INTO pending_trades "
           "(trade_id, slug, coin, `window`, side, entry_price, size_usd, "
           "round, results_file, mode, order_id, order_status, "
           "fill_price, fill_tx, llm_p, ref_price, edge, llm_reason, llm_model) "
           "VALUES (%(trade_id)s, %(slug)s, %(coin)s, %(window)s, %(side)s, "
           "%(entry_price)s, %(size_usd)s, %(round)s, %(results_file)s, "
           "%(mode)s, %(order_id)s, %(order_status)s, "
           "%(fill_price)s, %(fill_tx)s, %(llm_p)s, %(ref_price)s, %(edge)s, "
           "%(llm_reason)s, %(llm_model)s)")
    rows = []
    for r in recs:
        rows.append({
            "trade_id": r["trade_id"], "slug": r["slug"],
            "coin": r.get("coin"), "window": r.get("window"),
            "side": r["side"], "entry_price": r["entry_price"],
            "size_usd": r["size_usd"], "round": r.get("round"),
            "results_file": r.get("results_file"),
            "mode": r.get("mode", "simulate"),
            "order_id": r.get("order_id"),
            "order_status": r.get("order_status"),
            "fill_price": r.get("fill_price"),
            "fill_tx": r.get("fill_tx"),
            "llm_p": r.get("llm_p"),
            "ref_price": r.get("ref_price"),
            "edge": r.get("edge"),
            "llm_reason": r.get("llm_reason"),
            "llm_model": r.get("llm_model"),
        })
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, rows)
        return cur.rowcount
    finally:
        conn.close()


def mark_filled(trade_id: str, fill_price: float | None,
                fill_tx: str | None = None) -> int:
    """更新实盘单实际成交价/成交 tx（成交后回写）。"""
    sql = ("UPDATE pending_trades SET fill_price=%(fp)s, fill_tx=%(tx)s "
           "WHERE trade_id=%(tid)s")
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, {"fp": fill_price, "tx": fill_tx, "tid": trade_id})
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def fetch_pending() -> list[dict]:
    """全部待结算单（status='pending'）。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM pending_trades WHERE status='pending'")
            return list(cur.fetchall())
    finally:
        conn.close()


def mark_settled(trade_id: str, settle_yes: float, win: int, pnl: float):
    """结算完成：更新状态与结果。"""
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE pending_trades SET status='settled', settle_yes=%s, "
                "win=%s, pnl=%s, settled_at=NOW() WHERE trade_id=%s",
                (settle_yes, win, pnl, trade_id))
    finally:
        conn.close()


def count_pending() -> int:
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM pending_trades "
                        "WHERE status='pending'")
            return int(cur.fetchone()["n"])
    finally:
        conn.close()
