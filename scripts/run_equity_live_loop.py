"""股票/商品盘日级实盘循环：LLM 信号 → FOK 真实下单 → 入库 → settle_worker 结算。

与 5m 盘 run_live_loop.py 对齐：
- 扫描当日股票/商品 up-or-down 盘 → LLM 评估（日 K + 大盘局势）
- 首个 |edge|>=min_edge 信号 FOK $size 真实下单（CLOB V2，复用 run_live_loop 下单函数）
- 成交写入 pending_trades（window='daily'），由 settle_worker 常驻进程自动结算

⚠️ 真实资金。默认每轮最多 1 笔、$1/笔；资金预检（deposit wallet pUSD）。
日级盘每天一个结算窗口，一轮跑完即当日结束（无需 5m 那种循环等待）。

用法: PYTHONPATH=. .venv/bin/python scripts/run_equity_live_loop.py \
          [--size 1] [--min-edge 0.05] [--min-liquidity 200] [--per-round 1]
"""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from eth_account import Account

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from polytrader import db  # noqa: E402
from polytrader.ai.llm_scorer import LLMScorer  # noqa: E402
from polytrader.config import load_config  # noqa: E402
from polytrader.data.http_client import HttpClient  # noqa: E402
from polytrader.execution import order_v2  # noqa: E402
from scripts.run_live_loop import _Tee, _req, derive_creds, place_order  # noqa: E402
from scripts.scan_equity_updown import (  # noqa: E402
    PROXY, discover_daily_updown, to_market)
from scripts.simulate_equity_updown import build_db_rec, _parse_symbols  # noqa: E402
from polytrader.strategies.equity_updown import EquityUpdownStrategy  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=1.0, help="每笔 USD（默认 $1）")
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--min-liquidity", type=float, default=200.0)
    ap.add_argument("--per-round", type=int, default=1, help="本轮最多开几笔")
    ap.add_argument("--symbols", type=str, default="",
                    help="标的白名单（逗号分隔，如 nvda,spy,tsla；空=全部 17 个）")
    ap.add_argument("--account", type=str, default="default",
                    help="账户名（config/accounts.yaml，入库 account 列统计）")
    ap.add_argument("--log", type=str, default="",
                    help="日志文件路径（输出同时写文件，默认只输出到 stdout）")
    args = ap.parse_args()
    if args.log:
        sys.stdout = _Tee(args.log)  # type: ignore[assignment]
        # logging 输出也进同一日志文件（统一集合到 log）
        from polytrader.logging_setup import setup_logging
        setup_logging(level="INFO", log_file=args.log)

    # ---- 凭证与资金预检（账户配置化：config/accounts.yaml，env 兜底）----
    from polytrader.accounts import get_account
    acct = get_account(args.account)
    pk = acct.private_key
    deposit = acct.deposit_wallet
    if not pk or not deposit:
        print(f"!! 账户 '{args.account}' 缺 private_key / deposit_wallet"
              f"（config/accounts.yaml 或 .env）")
        return 1
    eoa = Account.from_key(pk).address
    creds = derive_creds(eoa, pk)
    tick_cache: dict = {}  # token_id -> (tick, 时间)
    negrisk_cache: dict = {}  # token_id -> (neg_risk, 时间)
    print(f"EOA={eoa} deposit={deposit} 认证 OK")

    from polytrader.execution import chain
    bal = chain.call_balance(chain.PUSD, deposit) / 1e6
    need = args.size * args.per_round + 0.05 * args.per_round
    print(f"deposit wallet pUSD: ${bal:.4f} | 本轮预算: ${need:.2f}")
    if bal < need:
        print(f"!! 资金不足：需要 ${need:.2f}，只有 ${bal:.2f}（先充值）")
        return 1

    # ---- LLM 评估 ----
    cfg = load_config()
    scorer = LLMScorer(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
                       model=cfg.llm_model)
    if not scorer.enabled:
        print("!! LLM not configured (LLM_API_KEY missing)")
        return 1
    strat = EquityUpdownStrategy(scorer, min_edge=args.min_edge)

    http = HttpClient(proxy=PROXY, timeout=15)
    mkts = discover_daily_updown(http, symbols=_parse_symbols(args.symbols))
    mkts = [m for m in mkts if float(m.get("liquidity") or 0) >= args.min_liquidity]
    print(f"discovered {len(mkts)} tradable daily up-or-down markets")
    if not mkts:
        print("  今日无盘口（或全部已结算/无流动性），退出")
        return 0

    # 去重：过滤已开单盘口
    already = {r["slug"] for r in db.fetch_pending()}
    mkts = [m for m in mkts if m.get("slug") not in already]
    print(f"after dedup (already pending: {len(already)}): {len(mkts)} markets")
    if not mkts:
        print("  全部已开单，退出")
        return 0

    markets = [to_market(m) for m in mkts]
    print(f"\nevaluating {len(markets)} markets (LLM, 并发 4)...")
    signals = strat.scan(markets)
    print(f"signals: {len(signals)}")

    # ---- FOK 真实下单 ----
    placed = 0
    results = []
    for s in signals:
        if placed >= args.per_round:
            break
        side = s.extra.get("side")
        if side not in ("YES", "NO"):
            continue
        m = s.market
        idx = 0 if side == "YES" else 1
        token_id = m.outcomes[idx].token_id
        # 预期成交价（吃单侧盘口价）：过滤坏单 [0.25, 0.85]（空壳盘口保护）
        expect_fill = None
        try:
            from polytrader.data.clob_client import ClobClient
            from polytrader.data.http_client import HttpClient
            b = ClobClient(http=HttpClient(proxy=PROXY, timeout=15)).get_book(token_id)
            if b:
                if side == "YES":
                    expect_fill = b.best_ask().price if b.best_ask() else None
                else:
                    expect_fill = (1.0 - b.best_bid().price
                                   if b.best_bid() else None)
        except Exception:
            expect_fill = None
        if expect_fill is not None and not (0.25 <= expect_fill <= 0.85):
            print(f"  {m.slug} {side} 预期成交价{expect_fill:.3f} "
                  f"超范围[0.25,0.85] 过滤（空壳盘口）")
            continue
        price = round(min(0.99, float(s.market_price) + 0.01), 3)
        print(f"  {m.slug} {side} llm_p={s.extra.get('llm_p', 0):.3f} "
              f"ref={s.market_price:.3f} edge={s.edge:+.3f} BUY@{price} ${args.size}")
        resp = place_order(creds, eoa, pk, deposit, token_id,
                         order_v2.BUY, args.size, price, tick_cache=tick_cache,
                         negrisk_cache=negrisk_cache)
        print(f"    POST /order: {resp['status_code']}")
        print(f"    {resp['body'][:220]}")
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
            tx_hash = None
            txs = body.get("transactionsHashes") or []
            if txs:
                tx_hash = txs[0]
            rec = build_db_rec({
                "trade_id": str(uuid.uuid4())[:8],
                "slug": m.slug,
                "coin": m.slug.split("-")[0],
                "window": "daily",
                "side": side,
                "entry_price": round(float(s.market_price), 4),
                "size_usd": args.size,
                "llm_p": round(float(s.extra.get("llm_p", 0)), 4),
                "ref": round(float(s.market_price), 4),
                "edge": round(float(s.edge), 4),
                "llm_reason": s.extra.get("llm_reason"),
                "model": s.extra.get("model"),
                "order_id": body.get("orderID"),
                "order_status": body.get("status"),
                "fill_price": fill_price,
                "fill_tx": tx_hash,
                "results_file": str(ROOT / "backtest_results"
                                    / f"equity_results_live_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"),
                "account": args.account,
            }, mode="live")
            try:
                db.insert_pending([rec])
                if fill_price is not None:
                    db.mark_filled(rec["trade_id"], fill_price, tx_hash)
            except Exception as e:
                # 订单已真实成交，DB 异常不能丢记录：落本地兜底文件
                print(f"    ⚠️ db insert FAILED（订单已成交，写兜底文件）: {e}")
                fallback = ROOT / "backtest_results" / "equity_live_fallback.jsonl"
                fallback.parent.mkdir(parents=True, exist_ok=True)
                with open(fallback, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                print(f"    → 兜底记录: {fallback}")
            results.append({"slug": m.slug, "side": side,
                            "llm_p": s.extra.get("llm_p"),
                            "ref": s.market_price, "edge": s.edge,
                            "orderID": body.get("orderID"),
                            "status": body.get("status"),
                            "fill_price": fill_price,
                            "tx": tx_hash})
            placed += 1
            print(f"    ✅ 成交 {body.get('orderID', '')[:20]}... "
                  f"status={body.get('status')} "
                  f"fill_price=${fill_price} tx={tx_hash[:18] if tx_hash else '?'}...")
        else:
            print(f"    ❌ 下单失败: {resp['body'][:200]}")

    print("\n=== 汇总 ===")
    print(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"pending 总数: {db.count_pending()}")
    if placed:
        print("→ 已入库 pending_trades，由 settle_worker 自动结算"
              "（每日 20:00Z 收盘后出结果）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
