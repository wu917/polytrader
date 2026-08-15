"""跟单交易循环：官方月度排行榜聪明钱 → 活动流轮询 → 镜像信号 → 模拟成交入库。

数据链路（官方文档 2026-08 确认的公开端点）：
- 目标发现：data-api /v1/leaderboard?timePeriod=MONTH&orderBy=PNL（每月排行榜）
  → OfficialLeaderboardProvider（pnl 为官方口径期间盈亏）
- 实时监听：data-api /activity?user=<wallet> 每 --poll 秒轮询
  （TRADE 事件含 transactionHash，可靠去重）
- 信号：MirrorEngine.scan_activity（BUY 侧 / YES-only / 滑点容忍 / 价格带过滤）
- 执行：默认 paper（DryRunBroker 模拟成交，绝不真实下单）
  → 入库 MySQL pending_trades（window='copytrade'）→ settle_worker 自动结算

⚠️ 真实资金铁律：本脚本默认模拟。任何实盘化改动须经 quant-guard 审查，
   且真实下单前必须用户单独确认金额与授权（见 AGENTS.md 第 5 节）。

用法:
    .venv/bin/python scripts/run_copytrade_loop.py --rounds 3 --log logs/copytrade.log
    .venv/bin/python scripts/run_copytrade_loop.py --rounds 0   # 无限循环
"""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent

from polytrader import db
from polytrader.config import load_config
from polytrader.copytrade.leaderboard import OfficialLeaderboardProvider
from polytrader.copytrade.mirror import MirrorEngine
from polytrader.data.clob_client import ClobClient
from polytrader.data.data_api import DataApiClient
from polytrader.data.http_client import HttpClient
from polytrader.execution.broker import DryRunBroker
from polytrader.risk.risk_manager import RiskManager

# 坏单过滤价格带（沿用 run_live_loop 语义：空壳盘口极端价格不成交）
DEFAULT_MIN_PRICE = 0.30
DEFAULT_MAX_PRICE = 0.90


def _load_seen(db) -> set[str]:
    """从 MySQL copytrade_seen 表恢复已镜像去重集（替代 seen 文件）。"""
    try:
        return db.load_copytrade_seen()
    except Exception as e:  # noqa: BLE001
        print(f"  !! load copytrade_seen FAILED: {e}（以空集继续）")
        return set()


def _save_seen(db, new_entries: list[tuple[str, str, str]]) -> None:
    """把新增的已镜像交易批量入库（INSERT IGNORE 幂等）。"""
    if not new_entries:
        return
    try:
        db.add_copytrade_seen(new_entries)
    except Exception as e:  # noqa: BLE001
        print(f"  !! save copytrade_seen FAILED: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="跟单交易循环（paper 模拟，默认不碰真实资金）")
    ap.add_argument("--rounds", type=int, default=0, help="轮数（0=无限循环）")
    ap.add_argument("--poll", type=int, default=20, help="活动流轮询间隔秒")
    ap.add_argument("--refresh-interval", type=int, default=1800,
                    help="排行榜目标刷新间隔秒（默认 30 分钟）")
    ap.add_argument("--top-n", type=int, default=20, help="排行榜取前 N 个钱包")
    ap.add_argument("--period", type=str, default="MONTH",
                    choices=["DAY", "WEEK", "MONTH", "ALL"], help="排行榜周期（默认月榜）")
    ap.add_argument("--category", type=str, default="OVERALL",
                    choices=["OVERALL", "POLITICS", "SPORTS", "ESPORTS", "CRYPTO",
                             "CULTURE", "MENTIONS", "WEATHER", "ECONOMICS", "TECH",
                             "FINANCE"])
    ap.add_argument("--order-by", type=str, default="PNL", choices=["PNL", "VOL"])
    ap.add_argument("--min-profit", type=float, default=0.0,
                    help="目标钱包最低期间盈亏 USD（排行榜 pnl 口径）")
    ap.add_argument("--min-trades", type=int, default=0,
                    help="目标钱包最低交易数（排行榜源无此字段，默认 0 跳过）")
    ap.add_argument("--size", type=float, default=1.0, help="每笔固定仓位 USD")
    ap.add_argument("--max-size-usd", type=float, default=5.0,
                    help="镜像单笔金额上限（按目标成交金额 min 计算）")
    ap.add_argument("--max-slippage", type=float, default=0.05,
                    help="基础滑点容忍（锚点=目标成交价；随活动年龄动态放宽）")
    ap.add_argument("--slippage-per-min", type=float, default=0.01,
                    help="活动年龄每多 1 分钟，滑点容忍 +x（默认 +1%/分钟）")
    ap.add_argument("--slippage-cap", type=float, default=0.15,
                    help="动态滑点容忍封顶")
    ap.add_argument("--max-age-seconds", type=int, default=600,
                    help="活动超龄不跟（默认 600s：10 分钟前的买入视为信息已消化）")
    ap.add_argument("--min-buy-price", type=float, default=DEFAULT_MIN_PRICE)
    ap.add_argument("--max-buy-price", type=float, default=DEFAULT_MAX_PRICE)
    ap.add_argument("--fetch-books", action="store_true",
                    help="拉盘口做滑点过滤（默认直接跟目标成交价）")
    ap.add_argument("--out-dir", type=str, default="backtest_results",
                    help="结果 jsonl 输出目录")
    ap.add_argument("--max-open-positions", type=int, default=10,
                    help="未结算持仓上限（按 DB copytrade pending 数控制，结算释放后自动恢复开单）")
    ap.add_argument("--no-db", action="store_true", help="不入库（只打印信号）")
    ap.add_argument("--log", type=str, default="", help="日志文件路径")
    args = ap.parse_args()

    if args.size <= 0 or args.size > 100:
        print(f"!! --size {args.size} 非法：须 (0, 100] 区间")
        return 2

    if args.log:
        log_file = Path(args.log)
        log_file.parent.mkdir(parents=True, exist_ok=True)
        sys.stdout = open(log_file, "a", encoding="utf-8")  # type: ignore[assignment]

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    results_file = out_dir / f"copytrade_results_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    results_file.touch()
    seen = _load_seen(db)

    def log(msg: str):
        line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {msg}"
        print(line, flush=True)

    def emit(rec: dict):
        with open(results_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    cfg = load_config()
    http = HttpClient(proxy=cfg.effective_proxy())
    data_api = DataApiClient(http=http)
    clob = ClobClient(http=http)

    provider = OfficialLeaderboardProvider(
        data_api, time_period=args.period, order_by=args.order_by,
        category=args.category, top_n=args.top_n)
    engine = MirrorEngine(
        provider, data_api,
        min_profit_usd=args.min_profit,
        min_trades=args.min_trades,
        max_slippage=args.max_slippage,
        slippage_per_min=args.slippage_per_min,
        slippage_cap=args.slippage_cap,
        max_age_seconds=args.max_age_seconds,
        mirror_yes_only=True,
        max_size_usd=args.max_size_usd,
        require_activity=False,  # 排行榜源无活跃时间字段
    )
    engine._seen_trade_ids = seen  # 载入历史去重
    risk = RiskManager(mode="paper", max_position_usd=args.max_size_usd * 4)
    broker = DryRunBroker()

    log(f"copytrade loop | period={args.period} category={args.category} "
        f"top_n={args.top_n} size=${args.size} poll={args.poll}s "
        f"seen={len(seen)} refresh={args.refresh_interval}s")
    emit({"type": "startup", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "period": args.period, "category": args.category, "top_n": args.top_n,
          "size_usd": args.size})

    round_no = 0
    last_refresh = 0.0
    while args.rounds == 0 or round_no < args.rounds:
        round_no += 1
        ts0 = time.time()
        rec = {"type": "round", "round": round_no, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}

        # 1) 目标刷新（排行榜）
        if time.time() - last_refresh >= args.refresh_interval or round_no == 1:
            try:
                targets = engine.refresh_targets()
                last_refresh = time.time()
                rec["targets"] = len(targets)
                log(f"  targets refreshed: {len(targets)} wallets "
                    f"(period={args.period} min_profit=${args.min_profit})")
            except Exception as e:  # noqa: BLE001
                log(f"  !! leaderboard refresh FAILED: {e}")
                rec["error"] = f"refresh: {e}"

        # 2) 活动流扫描 → 信号
        #    持仓上限按 DB 未结算数控制（RiskManager 内存态不感知结算释放）
        try:
            if not args.no_db:
                open_cnt = _count_open(db)
                if open_cnt >= args.max_open_positions:
                    log(f"  waiting: copytrade pending {open_cnt} >= "
                        f"max {args.max_open_positions}（结算释放后自动恢复）")
                    emit({**rec, "trades": 0, "waiting_open_positions": open_cnt})
                    if args.rounds != 0 and round_no >= args.rounds:
                        break
                    time.sleep(max(1, args.poll))
                    continue
            books = None
            if args.fetch_books:
                books = _fetch_books_for_signals(engine, clob)
            signals = engine.scan_activity(books)
        except Exception as e:  # noqa: BLE001
            log(f"  !! scan FAILED: {e}")
            rec["error"] = f"scan: {e}"
            signals = []

        # 3) 过滤 + 风控 + 模拟成交 + 入库
        opened = 0
        for s in signals:
            price = s.market_price
            if price < args.min_buy_price or price > args.max_buy_price:
                log(f"  filter: {s.market.slug[:40]:42s} price={price:.3f} "
                    f"∉ [{args.min_buy_price}, {args.max_buy_price}]")
                continue
            allowed, why = risk.check(s, args.size)
            if not allowed:
                log(f"  risk: {s.market.slug[:40]:42s} blocked ({why})")
                continue
            trade = broker.place(s)
            risk.record_trade(trade)
            opened += 1
            entry = trade.price if trade.price else price
            trade_rec = {
                "trade_id": str(uuid.uuid4())[:8],
                "slug": s.market.slug,
                "coin": s.market.slug.split("-")[0] if s.market.slug else "?",
                "window": "copytrade",
                "side": "YES",
                "entry_price": round(float(entry), 4),
                "size_usd": args.size,
                "llm_p": None,
                "ref": round(float(price), 4),
                "edge": round(float(s.edge), 4),
                "llm_reason": s.reason,
                "results_file": str(results_file),
            }
            rec_row = {
                "trade_id": trade_rec["trade_id"],
                "slug": trade_rec["slug"],
                "coin": trade_rec["coin"],
                "window": "copytrade",
                "side": trade_rec["side"],
                "entry_price": trade_rec["entry_price"],
                "size_usd": trade_rec["size_usd"],
                "round": round_no,
                "results_file": str(results_file),
                "mode": "simulate",
                "llm_p": None,
                "ref": trade_rec["ref"],
                "edge": trade_rec["edge"],
                "llm_reason": trade_rec["llm_reason"],
                "model": None,
                "mirror_wallet": s.extra.get("mirror_wallet"),
                "mirror_trade_id": s.extra.get("mirror_trade_id"),
            }
            if not args.no_db:
                from scripts.simulate_equity_updown import build_db_rec
                try:
                    db.insert_pending([build_db_rec(rec_row, mode="simulate")])
                except Exception as e:  # noqa: BLE001
                    log(f"  !! db insert FAILED {s.market.slug}: {e}")
            log(f"  >>> BUY YES {s.market.slug[:46]:46s} @${price:.3f} "
                f"size=${args.size} (mirror {str(s.extra.get('mirror_wallet', ''))[:10]}...)")
            emit({"type": "trade_open", "round": round_no, **rec_row})

        # 4) 持久化去重（MySQL copytrade_seen，差集批量入库）+ 摘要
        if len(engine._seen_trade_ids) != len(seen):
            new_seen = engine._seen_trade_ids - seen
            seen = set(engine._seen_trade_ids)
            if not args.no_db:
                _save_seen(db, [(tid, "", "") for tid in new_seen])
        rec["trades"] = opened
        rec["duration_s"] = round(time.time() - ts0, 1)
        emit(rec)
        if opened:
            log(f"  round {round_no} done: {opened} trade(s) opened in {rec['duration_s']}s")

        if args.rounds != 0 and round_no >= args.rounds:
            break
        time.sleep(max(1, args.poll))

    log(f"copytrade loop finished after {round_no} rounds (results: {results_file})")
    return 0


def _count_open(dbmod) -> int:
    """DB 中未结算 copytrade 单数（持仓上限控制用）。"""
    try:
        return len([r for r in dbmod.fetch_pending()
                    if r.get("window") == "copytrade"])
    except Exception:  # noqa: BLE001
        return 0


def _fetch_books_for_signals(engine: MirrorEngine, clob: ClobClient) -> dict:
    """为待镜像信号拉盘口（滑点过滤用）。失败降级为空 dict（直接跟成交价）。"""
    from polytrader.models import OrderBook
    books: dict = {}
    try:
        for wallet in engine._target_wallets:
            acts = engine.data_api.get_user_activity(wallet, limit=10)
            for a in acts:
                asset = str(a.get("asset", "") or "")
                if not asset or asset in books:
                    continue
                b = clob.get_book(asset)
                if b:
                    books[asset] = b
                if len(books) >= 50:
                    return books
    except Exception:  # noqa: BLE001
        pass
    return books


if __name__ == "__main__":
    sys.exit(main())
