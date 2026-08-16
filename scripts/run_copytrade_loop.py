"""跟单交易循环：官方月度排行榜聪明钱 → 活动流轮询 → 镜像信号 → 成交入库。

数据链路（官方文档 2026-08 确认的公开端点）：
- 目标发现：data-api /v1/leaderboard?timePeriod=MONTH&orderBy=PNL（每月排行榜）
  → OfficialLeaderboardProvider（pnl 为官方口径期间盈亏）
- 实时监听：data-api /activity?user=<wallet> 每 --poll 秒轮询
  （TRADE 事件含 transactionHash，可靠去重）
- 信号：MirrorEngine.scan_activity（BUY 侧 / YES-only / 滑点容忍 / 价格带过滤）
- 执行：默认 paper（DryRunBroker 模拟成交，绝不真实下单）；
  --live 时 FOK 真实下单（需用户显式授权 + --max-live-orders 上限）
  → 入库 MySQL pending_trades（window='copytrade'）→ settle_worker 自动结算

⚠️ 真实资金铁律：本脚本默认模拟。--live 开启即真实下单（FOK 吃单），
   必须用户单独确认金额与授权（见 AGENTS.md 第 5 节）；--max-live-orders
   为实盘总开单硬上限，开满即停止开单。

用法:
    .venv/bin/python scripts/run_copytrade_loop.py --rounds 3 --log logs/copytrade.log
    # 实盘试跑（2 笔：首轮启动即开 1 笔 + 后续扫描开 1 笔）
    .venv/bin/python scripts/run_copytrade_loop.py --live --max-live-orders 2 --rounds 5
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
from polytrader.execution import order_v2
from polytrader.risk.risk_manager import RiskManager

# 坏单过滤价格带（沿用 run_live_loop 语义：空壳盘口极端价格不成交）
DEFAULT_MIN_PRICE = 0.25
DEFAULT_MAX_PRICE = 0.90
# 实盘单笔硬上限（与 run_live_loop 一致，不可放大）
MAX_ORDER_USD = 1.0


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
                    help="活动年龄每多 1 分钟，滑点容忍 +x（默认每分钟 +0.01）")
    ap.add_argument("--slippage-cap", type=float, default=0.15,
                    help="动态滑点容忍封顶")
    ap.add_argument("--max-age-seconds", type=int, default=600,
                    help="活动超龄不跟（默认 600s：10 分钟前的买入视为信息已消化）")
    ap.add_argument("--no-wash-filter", action="store_true",
                    help="关闭套利/冲单过滤（默认开启：同市场买卖往返或双侧买入不跟）")
    ap.add_argument("--wash-window", type=int, default=1800,
                    help="套利/冲单判断时间窗秒（默认 1800s）")
    ap.add_argument("--min-buy-price", type=float, default=DEFAULT_MIN_PRICE)
    ap.add_argument("--max-buy-price", type=float, default=DEFAULT_MAX_PRICE)
    ap.add_argument("--fetch-books", action="store_true",
                    help="拉盘口做滑点过滤（默认直接跟目标成交价）")
    ap.add_argument("--out-dir", type=str, default="backtest_results",
                    help="结果 jsonl 输出目录")
    ap.add_argument("--max-open-positions", type=int, default=10,
                    help="未结算持仓上限（按 DB copytrade pending 数控制，结算释放后自动恢复开单）")
    ap.add_argument("--live", action="store_true",
                    help="实盘模式：FOK 真实下单（默认 paper 模拟；需用户显式授权）")
    ap.add_argument("--max-live-orders", type=int, default=2,
                    help="实盘总开单硬上限（开满即停止开单，默认 2）")
    ap.add_argument("--no-db", action="store_true", help="不入库（只打印信号）")
    ap.add_argument("--log", type=str, default="", help="日志文件路径")
    args = ap.parse_args()

    if args.size <= 0 or args.size > 100:
        print(f"!! --size {args.size} 非法：须 (0, 100] 区间")
        return 2
    if args.live and args.size > MAX_ORDER_USD:
        print(f"!! --live 模式单笔硬上限 ${MAX_ORDER_USD}，--size 不能超（当前 {args.size}）")
        return 2
    if args.live and args.max_live_orders <= 0:
        print("!! --live 模式 --max-live-orders 必须 ≥ 1")
        return 2
    if args.live:
        print(f"!! ⚠️  实盘模式：FOK 真实下单，单笔 ${args.size}，总上限 "
              f"{args.max_live_orders} 笔（最多 ${args.size * args.max_live_orders:.2f}）")
        print(f"!! 启动即开 1 笔（首轮立即扫描）+ 后续扫描开单")

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
        wash_filter=not args.no_wash_filter,
        wash_window_s=args.wash_window,
    )
    engine._seen_trade_ids = seen  # 载入历史去重
    risk = RiskManager(mode="paper", max_position_usd=args.max_size_usd * 4)
    broker = DryRunBroker()

    # ---- 实盘模式初始化（凭证 + 资金预检 + FOK 下单器）----
    live_ctx = None
    if args.live:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        from scripts.run_live_loop import derive_creds, place_order, get_order_auth
        from eth_account import Account
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
        deposit = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "").strip()
        if not pk or not deposit:
            print("!! .env 缺 POLYMARKET_PRIVATE_KEY / POLYMARKET_DEPOSIT_WALLET")
            return 1
        eoa = Account.from_key(pk).address
        # 启动认证容错：CLOB 网络抖动时重试（最多 5 次 × 10s），
        # 避免"启动即崩"导致挂机任务反复失效（2026-08-16 实测）
        creds = None
        for attempt in range(5):
            try:
                creds = derive_creds(eoa, pk)
                break
            except Exception as e:  # noqa: BLE001
                print(f"  ! 认证失败第 {attempt + 1} 次: {str(e)[:60]}，10s 后重试")
                time.sleep(10)
        if creds is None:
            print("!! 认证连续失败（网络不通？），退出")
            return 1
        from polytrader.execution import chain
        bal = chain.call_balance(chain.PUSD, deposit) / 1e6
        need = args.size + 0.05
        print(f"  EOA={eoa[:12]}... deposit={deposit[:12]}... "
              f"pUSD=${bal:.4f} 需要=${need:.2f}")
        if bal < need:
            print(f"!! 资金不足：需要 ${need:.2f}，只有 ${bal:.2f}（先充值 fund_deposit.py）")
            return 1
        live_ctx = {"pk": pk, "deposit": deposit, "eoa": eoa,
                    "creds": creds, "place_order": place_order,
                    "get_order_auth": get_order_auth,
                    "tick_cache": {},  # token_id -> (tick, 时间)
                    "negrisk_cache": {},  # token_id -> (neg_risk, 时间)
                    "pending_fills": [],  # [(trade_id, order_id, first_seen_ts)]
                    }
        # 启动恢复：DB 中 order_status='delayed' 的历史单重新登记追踪
        # （pending_fills 是内存态，重启后丢失曾导致孤儿订单——无法回填成交价）
        for tid, oid in _load_delayed_orders(db):
            live_ctx["pending_fills"].append((tid, oid, time.time()))
        if live_ctx["pending_fills"]:
            print(f"  恢复 {len(live_ctx['pending_fills'])} 笔 delayed 单追踪")
    log(f"copytrade loop | period={args.period} category={args.category} "
        f"top_n={args.top_n} size=${args.size} poll={args.poll}s "
        f"seen={len(seen)} refresh={args.refresh_interval}s "
        f"mode={'LIVE' if args.live else 'paper'}"
        f"{f' max_live={args.max_live_orders}' if args.live else ''}")
    emit({"type": "startup", "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
          "period": args.period, "category": args.category, "top_n": args.top_n,
          "size_usd": args.size, "mode": "live" if args.live else "paper"})

    round_no = 0
    last_refresh = 0.0
    while args.rounds == 0 or round_no < args.rounds:
        round_no += 1
        ts0 = time.time()
        rec = {"type": "round", "round": round_no, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}

        # 0) 待确认成交回填：delayed 单轮询 CLOB，MATCHED 后回填实际成交价
        if args.live and live_ctx["pending_fills"]:
            _reconcile_pending_fills(live_ctx, log, emit, round_no)

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
        #    持仓上限：paper 按全部 copytrade pending；live 按未结算 live 单数
        #    （结算释放后自动恢复开单，保持同时持仓 ≤ 上限）
        try:
            if not args.no_db:
                open_cnt = (_count_live_open(db) if args.live
                            else _count_open(db))
                limit = _hot_limit(args, args.live)
                if open_cnt >= limit:
                    log(f"  waiting: {'live' if args.live else 'copytrade'} "
                        f"pending {open_cnt} >= max {limit}（结算释放后自动恢复）")
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

        # 3) 过滤 + 风控 + 成交 + 入库
        #    每市场仅跟 1 笔：同 slug 已有 live 单则跳过加仓（集中度受控）
        live_slugs = _live_slugs(db) if (args.live and not args.no_db) else set()
        opened = 0
        for s in signals:
            if not args.no_db:
                cur_open = open_cnt + opened
                limit = _hot_limit(args, args.live)
                if cur_open >= limit:
                    log(f"  round cap reached ({cur_open}/{limit})，"
                        f"本轮剩余信号暂停（下轮释放后继续）")
                    break
            price = s.market_price
            # 每市场仅跟 1 笔：同 slug 已开过 live 单（含本轮）→ 跳过
            if args.live and s.market.slug in live_slugs:
                log(f"  skip: {s.market.slug[:40]} 已跟过（每市场仅一单）")
                continue
            if price < args.min_buy_price or price > args.max_buy_price:
                log(f"  filter: {s.market.slug[:40]:42s} price={price:.3f} "
                    f"∉ [{args.min_buy_price}, {args.max_buy_price}]")
                continue
            allowed, why = risk.check(s, args.size)
            if not allowed:
                log(f"  risk: {s.market.slug[:40]:42s} blocked ({why})")
                continue

            if args.live:
                # ---- 实盘：FOK 吃单（跟单要快）----
                token_id = s.outcome.token_id if s.outcome else ""
                if not token_id:
                    log(f"  {s.market.slug[:40]} 无 token_id，跳过")
                    continue
                # 吃单侧 ask 预检（同 run_live_loop 修复后逻辑：勿用 1-bid 估算）
                try:
                    b = clob.get_book(token_id)
                    expect_fill = b.best_ask().price if b and b.best_ask() else None
                except Exception:
                    expect_fill = None
                fill_base = expect_fill if expect_fill is not None else price
                if not (args.min_buy_price <= fill_base <= args.max_buy_price):
                    log(f"  filter(live): {s.market.slug[:40]} 预期成交价 "
                        f"{fill_base:.3f} ∉ [{args.min_buy_price}, "
                        f"{args.max_buy_price}]（空壳盘口）")
                    continue
                px = round(min(args.max_buy_price, max(0.01, fill_base + 0.01)), 2)
                log(f"  >>> LIVE BUY YES {s.market.slug[:40]:42s} FOK@{px} "
                    f"${args.size} (mirror {str(s.extra.get('mirror_wallet', ''))[:10]}...)")
                resp = live_ctx["place_order"](
                    live_ctx["creds"], live_ctx["eoa"], live_ctx["pk"],
                    live_ctx["deposit"], token_id, order_v2.BUY,
                    args.size, px, order_type="FOK",
                    tick_cache=live_ctx["tick_cache"],
                    negrisk_cache=live_ctx["negrisk_cache"])
                print(f"    POST /order: {resp['status_code']}")
                print(f"    {resp['body'][:200]}")
                try:
                    body = json.loads(resp["body"])
                except Exception:
                    body = {}
                if resp["status_code"] == 200 and body.get("success"):
                    try:
                        fill_price = round(
                            float(body.get("makingAmount", 0)) /
                            float(body.get("takingAmount", 1)), 4)
                    except (TypeError, ValueError, ZeroDivisionError):
                        fill_price = None
                    txs = body.get("transactionsHashes") or []
                    tx_hash = txs[0] if txs else None
                    entry = fill_price if fill_price else px
                    rec_row = {
                        "trade_id": str(uuid.uuid4())[:8],
                        "slug": s.market.slug,
                        "coin": s.market.slug.split("-")[0] if s.market.slug else "?",
                        "window": "copytrade",
                        "side": "YES",
                        "entry_price": round(float(entry), 4),
                        "size_usd": args.size,
                        "round": round_no,
                        "results_file": str(results_file),
                        "mode": "live",
                        "order_id": body.get("orderID"),
                        "order_status": body.get("status"),
                        "fill_price": round(float(fill_price), 4) if fill_price else None,
                        "fill_tx": tx_hash,
                        "llm_p": None,
                        "ref": round(float(price), 4),
                        "edge": round(float(s.edge), 4),
                        "llm_reason": s.reason,
                        "model": None,
                        "mirror_wallet": s.extra.get("mirror_wallet"),
                        "mirror_trade_id": s.extra.get("mirror_trade_id"),
                    }
                    # delayed 响应不含成交信息：登记待轮询回填（下一轮起查）
                    if body.get("status") == "delayed" and rec_row["order_id"]:
                        live_ctx["pending_fills"].append(
                            (rec_row["trade_id"], rec_row["order_id"], time.time()))
                    if not args.no_db:
                        from scripts.simulate_equity_updown import build_db_rec
                        try:
                            db.insert_pending([build_db_rec(rec_row, mode="live")])
                            if fill_price is not None:
                                db.mark_filled(rec_row["trade_id"], fill_price, tx_hash)
                        except Exception as e:  # noqa: BLE001
                            log(f"  !! db insert FAILED {s.market.slug}: {e}")
                    opened += 1
                    live_slugs.add(s.market.slug)  # 每市场仅一单
                    emit({"type": "trade_open", "round": round_no, **rec_row})
                    # 成交后立即落 seen（防崩溃重启重复镜像同一笔 FOK）
                    if not args.no_db:
                        tid = str(s.extra.get("mirror_trade_id") or "")
                        if tid:
                            _save_seen(db, [(tid, "", "")])
                    log(f"    ✅ 成交 {body.get('orderID','')[:20]}... "
                        f"status={body.get('status')} fill=${fill_price} "
                        f"tx={tx_hash[:18] if tx_hash else '?'}...")
                else:
                    log(f"    ❌ 下单失败: {resp['body'][:200]}")
                continue

            # ---- paper：模拟成交 ----
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


def _reconcile_pending_fills(live_ctx: dict, log, emit, round_no: int) -> None:
    """轮询 delayed 订单：MATCHED 后回填实际成交价（mark_filled + order_status）。

    delayed 响应不含 making/taking——FOK 排队成交后 CLOB 才有成交信息；
    超时（10 分钟）仍未确认的移除并告警（可能被拒/取消，不阻塞循环）。
    """
    import time as _t
    now = _t.time()
    remain = []
    for trade_id, order_id, first_ts in live_ctx["pending_fills"]:
        if not order_id:
            continue
        if now - first_ts > 600:
            # 超时：做最终状态确认——MATCHED 则回填成交；否则释放 DB 占坑
            # （delayed 假单曾永久 pending 占持仓名额，只能人工清理）
            try:
                od = live_ctx["get_order_auth"](
                    live_ctx["creds"], live_ctx["eoa"], order_id)
            except Exception:  # noqa: BLE001 查询失败下轮重试，不误释放
                remain.append((trade_id, order_id, first_ts))
                continue
            if isinstance(od, dict) and od.get("status") == "MATCHED":
                _fill_and_release(live_ctx, trade_id, od, log, emit, round_no)
                log(f"  [reconcile] {trade_id} 超时后确认 MATCHED，已回填成交")
            else:
                final_st = od.get("status") if isinstance(od, dict) else "unknown"
                _release_pending(trade_id, final_st)
                log(f"  [reconcile] {trade_id} 超时未成交（{final_st}），"
                    f"已释放占坑名额")
            continue
        try:
            od = live_ctx["get_order_auth"](
                live_ctx["creds"], live_ctx["eoa"], order_id)
        except Exception as e:  # noqa: BLE001 网络抖动下轮重试
            remain.append((trade_id, order_id, first_ts))
            continue
        if not isinstance(od, dict) or od.get("status") != "MATCHED":
            remain.append((trade_id, order_id, first_ts))  # 未成交继续等
            continue
        # MATCHED：回填成交价。fill 查询可能因 /data/trades 索引延迟失败
        # （下单后几秒内查询常查不到）——失败则保留队列下轮重试
        ok = _fill_and_release(live_ctx, trade_id, od, log, emit, round_no)
        if not ok:
            remain.append((trade_id, order_id, first_ts))
    live_ctx["pending_fills"] = remain


def _fill_and_release(live_ctx: dict, trade_id: str, od: dict,
                      log, emit, round_no: int) -> bool:
    """MATCHED 订单：回填成交价 + 更新 order_status。返回是否回填成功。

    注意：/data/order 端点（get_order_auth）不含 making/taking 成交金额——
    用它解析 fill 恒为 0（2026-08-16 实测，曾致 4 笔 fill_price=0.0）。
    正确数据源是认证 /data/trades（按 taker_order_id 匹配实际成交价）；
    索引有延迟（下单后几秒查不到）——失败返回 False，调用方保留重试。
    """
    fill, tx = None, None
    try:
        making = od.get("makingAmount")
        taking = od.get("takingAmount")
        if making is not None and taking is not None:
            fill = round(float(making) / float(taking), 4)
    except (TypeError, ValueError, ZeroDivisionError):
        fill = None
    if fill is None or fill == 0:
        # /data/order 无成交金额：从 /data/trades 按 order_id 匹配
        fill, tx = _find_fill_by_order(live_ctx, od.get("orderID"))
    if fill is None:
        return False  # 索引延迟未查到：调用方保留队列下轮重试
    if tx is None:
        txs = od.get("transactionsHashes") or []
        tx = txs[0] if txs else None
    db.mark_filled(trade_id, fill, tx)
    try:
        conn = db.connect()
        with conn.cursor() as cur:
            cur.execute("UPDATE pending_trades SET order_status='matched' "
                        "WHERE trade_id=%s", (trade_id,))
        conn.close()
    except Exception:  # noqa: BLE001
        pass
    log(f"  [reconcile] {trade_id} MATCHED fill=${fill} tx={str(tx)[:20] if tx else '?'}")
    emit({"type": "fill_confirmed", "round": round_no, "trade_id": trade_id,
          "fill_price": fill, "fill_tx": tx})
    return True


def _find_fill_by_order(live_ctx: dict, order_id: str | None) -> tuple[float | None, str | None]:
    """从认证 /data/trades 按 taker_order_id 匹配实际成交价（fill, tx）。"""
    if not order_id:
        return None, None
    try:
        import requests
        proxies = {"http": "http://127.0.0.1:7897",
                   "https": "http://127.0.0.1:7897"}
        from polytrader.execution.signer import (sign_clob_auth,
                                                 derive_api_key_headers,
                                                 l2_headers_new)
        from eth_account import Account
        pk = live_ctx["pk"]
        eoa = live_ctx["eoa"]
        ts = int(time.time())
        sig = sign_clob_auth(eoa, pk, timestamp=ts, nonce=0)
        creds = requests.get(
            "https://clob.polymarket.com/auth/derive-api-key",
            headers=derive_api_key_headers(eoa, sig, ts),
            proxies=proxies, timeout=20).json()
        h2 = l2_headers_new(eoa, creds["apiKey"], creds["passphrase"],
                            creds["secret"], "GET", "/data/trades")
        r = requests.get("https://clob.polymarket.com/data/trades",
                         params={"limit": 50, "sort": "desc"},
                         headers=h2, proxies=proxies, timeout=20)
        for t in r.json().get("data", []):
            if t.get("taker_order_id") == order_id and \
                    t.get("status") == "CONFIRMED":
                return float(t["price"]), t.get("transaction_hash")
    except Exception:  # noqa: BLE001 下轮重试
        pass
    return None, None


def _release_pending(trade_id: str, final_status: str) -> None:
    """释放占坑：订单最终未成交 → 从 pending 队列移除（释放持仓名额）。"""
    try:
        conn = db.connect()
        with conn.cursor() as cur:
            cur.execute("UPDATE pending_trades SET status='cancelled', "
                        "order_status=%s WHERE trade_id=%s AND status='pending'",
                        (str(final_status)[:32], trade_id))
        conn.close()
    except Exception:  # noqa: BLE001
        pass


def _count_open(dbmod) -> int:
    """DB 中未结算 copytrade 单数（paper 持仓上限控制用）。"""
    try:
        return len([r for r in dbmod.fetch_pending()
                    if r.get("window") == "copytrade"])
    except Exception:  # noqa: BLE001
        return 0


def _hot_limit(args, is_live: bool) -> int:
    """持仓上限热更新：每轮读 logs/copytrade_limit.txt（数字）覆盖启动值。

    仅对实盘（live）生效；paper 用启动参数。运行中改文件即生效
    （无需重启）：改小立即收紧（waiting），改大立即放行。
    文件缺失/非法时回退启动参数。
    """
    if not is_live:
        return args.max_open_positions
    try:
        f = ROOT / "logs" / "copytrade_limit.txt"
        if f.exists():
            v = f.read_text(encoding="utf-8").strip()
            if v:
                n = int(v)
                if n >= 1:
                    return n
    except (ValueError, OSError):
        pass
    return args.max_live_orders


def _load_delayed_orders(dbmod) -> list[tuple[str, str]]:
    """DB 中 order_status='delayed' 的 live 单（重启后恢复回填追踪）。"""
    try:
        conn = dbmod.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT trade_id, order_id FROM pending_trades "
                        "WHERE mode='live' AND `window`='copytrade' "
                        "AND order_status='delayed' AND status='pending'")
            rows = cur.fetchall()
        conn.close()
        return [(str(r["trade_id"]), str(r["order_id"]))
                for r in rows if r.get("order_id")]
    except Exception:  # noqa: BLE001
        return []
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _live_slugs(dbmod) -> set[str]:
    """已开过 live 跟单的市场 slug 集合（每市场仅跟 1 笔的判定依据）。"""
    try:
        conn = dbmod.connect()
        with conn.cursor() as cur:
            cur.execute("SELECT slug FROM pending_trades "
                        "WHERE mode='live' AND `window`='copytrade'")
            return {str(r["slug"]) for r in cur.fetchall()}
    except Exception:  # noqa: BLE001
        return set()
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _count_live_open(dbmod) -> int:
    """DB 中未结算 live 跟单单数（实盘持仓上限控制用）。"""
    try:
        return len([r for r in dbmod.fetch_pending()
                    if r.get("window") == "copytrade"
                    and r.get("mode") == "live"])
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
    # 外层兜底：任何未捕获异常（含非 Exception 的 BaseException）记录完整
    # traceback 到 logs/copytrade_crash.log（防止 stderr 被 nohup 丢弃导致
    # 崩溃原因不可见——2026-08-16 连续 3 次下单后静默死亡的排查发现）
    import traceback
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except BaseException:
        try:
            crash_log = ROOT / "logs" / "copytrade_crash.log"
            crash_log.parent.mkdir(parents=True, exist_ok=True)
            with open(crash_log, "a", encoding="utf-8") as fh:
                fh.write(f"\n=== {time.strftime('%Y-%m-%dT%H:%M:%S')} ===\n")
                fh.write(traceback.format_exc())
        except Exception:
            pass
        raise
