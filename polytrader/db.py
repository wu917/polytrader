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
                    trade_id     VARCHAR(32)  NOT NULL PRIMARY KEY COMMENT '交易唯一ID（UUID 前8位，主键）',
                    slug         VARCHAR(160) NOT NULL COMMENT 'Polymarket 市场 slug（如 nvda-up-or-down-...、chi-sgr-hen-2026-08-15-first-half-total-1pt5）',
                    coin         VARCHAR(32) COMMENT '标的/前缀（btc/eth/dota2/el2 等，取 slug 首段）',
                    `window`     VARCHAR(16) COMMENT '盘口窗口枚举：5m/15m=加密 updown、daily=股票商品日级、event=事件盘、copytrade=跟单盘',
                    side         VARCHAR(8)   NOT NULL COMMENT '方向枚举：YES/NO（均按 YES 结算价 settle_yes 判定输赢）',
                    entry_price  DECIMAL(12,6) COMMENT '入场价：simulate=模拟成交价，live=fill_price 实际成交价（delayed 未确认时为限价）',
                    size_usd     DECIMAL(14,4) COMMENT '下单金额（USD，实盘单笔 $1 硬上限）',
                    `round`      INT COMMENT '轮次（实盘循环第几轮，0=手动补录）',
                    results_file VARCHAR(255) COMMENT '结果文件路径（settle_worker 结算事件追加处）',
                    mode         VARCHAR(16)  DEFAULT 'simulate' COMMENT '模式枚举：simulate=模拟、live=实盘（真实资金）',
                    order_id     VARCHAR(80) COMMENT 'CLOB 订单ID（live，0x 开头哈希）',
                    order_status VARCHAR(32) COMMENT 'CLOB 订单状态枚举：delayed=排队确认中、matched=已成交、cancelled=已取消、expired=已过期',
                    fill_price   DECIMAL(12,6) COMMENT '实际成交价（live 吃单成本，结算成本优先用此价，delayed 回填）',
                    fill_tx      VARCHAR(80) COMMENT '成交交易哈希（live，链上 0x 哈希）',
                    llm_p        DECIMAL(8,4) COMMENT 'LLM 判断的概率 P(事件)（跟单为 NULL）',
                    ref_price    DECIMAL(12,6) COMMENT '市场隐含概率（Gamma outcomePrices，跟单=镜像成交价）',
                    edge         DECIMAL(8,4) COMMENT 'edge = |LLM 概率 - 市场概率|（跟单为 0）',
                    llm_reason   TEXT COMMENT 'LLM 判断理由 / 跟单为 mirror 来源钱包与价格',
                    llm_model    VARCHAR(64) COMMENT 'LLM 模型名（跟单为 NULL）',
                    status       VARCHAR(16)  DEFAULT 'pending' COMMENT '结算状态枚举：pending=待结算、settled=已结算、cancelled=订单未成交释放占坑',
                    settle_yes   DECIMAL(8,4) COMMENT '结算结果 YES 价格：1.0=事件发生（YES 赢），0.0=未发生（NO 赢）',
                    win          TINYINT COMMENT '是否盈利：1/0（由 side 与 settle_yes 推导）',
                    pnl          DECIMAL(14,4) COMMENT '盈亏（USD，正=盈利；= size/entry_price - size 或 -size）',
                    created_at   TIMESTAMP    DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间（下单/补录时间）',
                    settled_at   TIMESTAMP    NULL DEFAULT NULL COMMENT '结算时间（settle_worker mark_settled 写入）',
                    INDEX idx_status (status),
                    INDEX idx_slug (slug)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COMMENT='已开单待结算队列：5m/15m 加密 updown、daily 股票商品盘、event 事件盘、copytrade 跟单盘共用；开单 INSERT(pending)→settle_worker 轮询→mark_settled(settled)'
            """)
            # 2026-08-17 per-wallet 画像：跟单单记录来源大牛钱包
            # （幂等加列：MySQL 8 无 ADD COLUMN IF NOT EXISTS，查 information_schema）
            cur.execute(
                "SELECT COUNT(*) n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='pending_trades' "
                "AND COLUMN_NAME='mirror_wallet'")
            if cur.fetchone()["n"] == 0:
                cur.execute(
                    "ALTER TABLE pending_trades ADD COLUMN mirror_wallet "
                    "VARCHAR(66) NULL COMMENT '跟单来源大牛钱包（per-wallet 画像/黑名单用）' "
                    "AFTER llm_model")
            # 2026-08-19 多账户：下单账号名（区分任务策略/金额收益率统计）
            cur.execute(
                "SELECT COUNT(*) n FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='pending_trades' "
                "AND COLUMN_NAME='account'")
            if cur.fetchone()["n"] == 0:
                cur.execute(
                    "ALTER TABLE pending_trades ADD COLUMN account "
                    "VARCHAR(32) NULL COMMENT '下单账户名（config/accounts.yaml，按任务策略区分统计）' "
                    "AFTER mirror_wallet")
            # 黑名单表：用户手动维护，扫描时剔除
            cur.execute("""
                CREATE TABLE IF NOT EXISTS copytrade_wallet_blacklist (
                    wallet      VARCHAR(66) NOT NULL PRIMARY KEY COMMENT '大牛 proxyWallet 地址（0x 开头）',
                    reason      VARCHAR(255) COMMENT '拉黑原因（手动指定时填写）',
                    created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP COMMENT '拉黑时间',
                    INDEX idx_created (created_at)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COMMENT='跟单黑名单：用户手动维护，每次扫描剔除这些钱包的活动'
            """)
        _schema_checked = True
    finally:
        conn.close()


def fetch_wallet_blacklist() -> set[str]:
    """当前黑名单钱包集合（空表/查询失败返回空集，不阻塞扫描）。"""
    try:
        ensure_schema()
        conn = connect()
        with conn.cursor() as cur:
            cur.execute("SELECT wallet FROM copytrade_wallet_blacklist")
            return {str(r["wallet"]).lower() for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        return set()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def insert_pending(recs: list[dict]) -> int:
    """批量插入待结算单（trade_id 已存在则忽略）。返回实际插入数。"""
    if not recs:
        return 0
    ensure_schema()
    sql = ("INSERT IGNORE INTO pending_trades "
           "(trade_id, slug, coin, `window`, side, entry_price, size_usd, "
           "round, results_file, mode, order_id, order_status, "
           "fill_price, fill_tx, llm_p, ref_price, edge, llm_reason, llm_model, "
           "mirror_wallet, account) "
           "VALUES (%(trade_id)s, %(slug)s, %(coin)s, %(window)s, %(side)s, "
           "%(entry_price)s, %(size_usd)s, %(round)s, %(results_file)s, "
           "%(mode)s, %(order_id)s, %(order_status)s, "
           "%(fill_price)s, %(fill_tx)s, %(llm_p)s, %(ref_price)s, %(edge)s, "
           "%(llm_reason)s, %(llm_model)s, %(mirror_wallet)s, %(account)s)")
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
            "mirror_wallet": r.get("mirror_wallet"),
            "account": r.get("account"),
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


# ---- 跟单去重（copytrade_seen：已镜像交易，替代 seen 文件持久化）----

_seen_schema_checked = False


def ensure_copytrade_seen_schema() -> None:
    """幂等建表：copytrade_seen 不存在则创建（进程内只检查一次）。

    存已镜像的 transactionHash（含钱包与 token），跟单循环重启不重复跟单。
    """
    global _seen_schema_checked
    if _seen_schema_checked:
        return
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS copytrade_seen (
                    trade_id    VARCHAR(96)  NOT NULL PRIMARY KEY COMMENT '镜像交易唯一 ID（transactionHash 等）',
                    wallet      VARCHAR(64)  COMMENT '目标钱包地址',
                    asset       VARCHAR(96)  COMMENT 'outcome token_id',
                    seen_at     TIMESTAMP    DEFAULT CURRENT_TIMESTAMP COMMENT '首次发现时间',
                    INDEX idx_seen_wallet (wallet)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                  COMMENT='跟单已镜像交易去重（跨轮持久，替代 seen 文件）'
            """)
        _seen_schema_checked = True
    finally:
        conn.close()


def load_copytrade_seen() -> set[str]:
    """加载全部已镜像交易 ID（跟单循环启动时恢复去重集）。"""
    ensure_copytrade_seen_schema()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT trade_id FROM copytrade_seen")
            return {str(r["trade_id"]) for r in cur.fetchall()}
    finally:
        conn.close()


def add_copytrade_seen(entries: list[tuple[str, str, str]]) -> int:
    """批量记录已镜像交易（INSERT IGNORE 幂等）。entries: [(trade_id, wallet, asset)]。"""
    if not entries:
        return 0
    ensure_copytrade_seen_schema()
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT IGNORE INTO copytrade_seen (trade_id, wallet, asset) "
                "VALUES (%s, %s, %s)", entries)
        return cur.rowcount
    finally:
        conn.close()
