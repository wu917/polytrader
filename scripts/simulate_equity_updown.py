"""股票/商品盘日级模拟回测：扫描当日盘 → LLM 评估 → 模拟成交 → 入库 → 结算。

与 5m 盘 simulate_llm_updown.py 对齐：
- 信号 → 模拟成交（吃单侧盘口价，空壳回退 ref）→ 过滤坏价 [0.25, 0.85]
- 每笔写入 MySQL pending_trades（window='daily'），由 settle_worker 自动结算
  （settle_worker 按 slug 查 Gamma keyset 结算，对日级盘同样适用）
- 本地结果 JSON + 审计 JSONL

用法:
  PYTHONPATH=. .venv/bin/python scripts/simulate_equity_updown.py \
      [--min-edge 0.05] [--min-liquidity 200] [--size 100] [--no-db]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.config import load_config
from polytrader.data.clob_client import ClobClient
from polytrader.data.http_client import HttpClient
from polytrader.strategies.equity_context import SYMBOL_MAP
from polytrader.strategies.equity_updown import EquityUpdownStrategy

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "backtest_results"

from scripts.scan_equity_updown import (  # noqa: E402
    PROXY, discover_daily_updown, to_market)

# 模拟成交价过滤（吃单成本过高/空壳盘口不成交）
MIN_FILL, MAX_FILL = 0.25, 0.85


def audit(rec: dict, path: str | None):
    """写审计 JSONL（与 5m 盘同风格）。"""
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def fetch_settlement(http, slug: str) -> float | None:
    """结算结果：YES 结算价（1.0=涨, 0.0=跌, None=未结算）。"""
    try:
        resp = http.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                             f"slug={slug}&limit=10&locale=en")
    except Exception:
        return None
    events = resp if isinstance(resp, list) else resp.get("events", [])
    for ev in events:
        for m in ev.get("markets", []) or []:
            if m.get("slug") != slug:
                continue
            prices = m.get("outcomePrices") or ""
            try:
                prices = json.loads(prices) if isinstance(prices, str) else prices
            except Exception:
                return None
            if not prices:
                return None
            yes = float(prices[0])
            if yes in (0.0, 1.0):
                return yes
            return None
    return None


def sim_market_price(book: dict | None, side: str, ref: float) -> float | None:
    """模拟成交价：YES→ask；NO→1-bid。无盘口回退 ref，cap 0.97。"""
    px = None
    if book:
        if side == "YES":
            px = book.get("ask")
        else:
            bid = book.get("bid")
            px = (1.0 - bid) if bid is not None else None
    if px is None:
        px = float(ref)
    return round(min(px, 0.97), 4)


def build_db_rec(t: dict, mode: str = "simulate") -> dict:
    """构造 pending_trades 入库记录（与 db.insert_pending 列对齐）。

    t: 模拟成交记录（trade_id/slug/coin/window/side/entry_price/size_usd/...）
       live 模式可带 order_id / order_status / fill_price / fill_tx
    mode: simulate | live
    """
    rec = {
        "trade_id": t["trade_id"], "slug": t["slug"],
        "coin": t["coin"], "window": t.get("window", "daily"),
        "side": t["side"], "entry_price": round(float(t["entry_price"]), 4),
        "size_usd": t["size_usd"], "round": 1,
        "results_file": t.get("results_file"),
        "mode": mode,
        "llm_p": t.get("llm_p"), "ref_price": t.get("ref"),
        "edge": t.get("edge"), "llm_reason": t.get("llm_reason"),
        "llm_model": t.get("model"),
    }
    if mode == "live":
        rec["order_id"] = t.get("order_id")
        rec["order_status"] = t.get("order_status")
        rec["fill_price"] = t.get("fill_price")
        rec["fill_tx"] = t.get("fill_tx")
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--min-liquidity", type=float, default=200.0)
    ap.add_argument("--size", type=float, default=100.0, help="每笔 USD（默认 $100）")
    ap.add_argument("--max-markets", type=int, default=10)
    ap.add_argument("--no-db", action="store_true",
                    help="不入库（默认入库 pending_trades 由 settle_worker 结算）")
    ap.add_argument("--wait", type=int, default=0,
                    help="等待结算秒数（0=不等，日级盘建议由 settle_worker 结算）")
    ap.add_argument("--log", type=str, default="", help="日志文件路径")
    args = ap.parse_args()

    if args.log:
        sys.stdout = _Tee(args.log)  # type: ignore[assignment]

    http = HttpClient(proxy=PROXY, timeout=15)
    mkts = discover_daily_updown(http)
    mkts = [m for m in mkts if float(m.get("liquidity") or 0) >= args.min_liquidity]
    print(f"discovered {len(mkts)} tradable daily up-or-down markets "
          f"(liq>={args.min_liquidity:.0f})")

    cfg = load_config()
    scorer = LLMScorer(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
                       model=cfg.llm_model)
    if not scorer.enabled:
        print("!! LLM not configured (LLM_API_KEY missing)")
        return 1
    strat = EquityUpdownStrategy(scorer, min_edge=args.min_edge,
                                 max_markets=args.max_markets)
    markets = [to_market(m) for m in mkts]

    # 盘口快照（模拟成交用吃单侧价）
    clob = ClobClient(http=http)
    books = {}
    for m in markets:
        try:
            b = clob.get_book(m.outcomes[0].token_id)
            if b:
                books[m.condition_id] = {
                    "bid": b.best_bid().price if b.best_bid() else None,
                    "ask": b.best_ask().price if b.best_ask() else None}
        except Exception:
            pass

    print(f"\nevaluating {len(markets)} markets (LLM, 并发 4)...")
    signals = strat.scan(markets)
    print(f"signals: {len(signals)}")

    audit_path = str(OUT_DIR / f"audit_equity_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")

    trades, skipped = [], 0
    seen = set()
    for s in signals:
        if len(trades) >= args.max_markets:
            break
        side = s.extra.get("side")
        if side not in ("YES", "NO"):
            continue
        m = s.market
        if m.slug in seen:
            continue
        fill = sim_market_price(books.get(m.condition_id), side, s.market_price)
        if fill is None or not (MIN_FILL <= fill <= MAX_FILL):
            skipped += 1
            print(f"  -- {m.slug[:48]:48s} {side:3s} 成交价{fill} 超范围"
                  f"[{MIN_FILL},{MAX_FILL}] 过滤")
            audit({"ts": _ts(), "event": "trade_skipped_bad_price",
                   "slug": m.slug, "side": side, "fill": fill}, audit_path)
            continue
        trade_id = str(uuid.uuid4())[:8]
        t = {
            "trade_id": trade_id, "slug": m.slug,
            "condition_id": m.condition_id,
            "coin": m.slug.split("-")[0], "window": "daily",
            "side": side,
            "llm_p": round(float(s.extra.get("llm_p", 0)), 4),
            "ref": round(float(s.market_price), 4),
            "edge": round(float(s.edge), 4),
            "size_usd": args.size, "entry_price": fill,
            "llm_reason": s.extra.get("llm_reason"),
            "model": s.extra.get("model"),
            "results_file": str(OUT_DIR / f"equity_results_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"),
        }
        trades.append(t)
        seen.add(m.slug)
        audit({"ts": _ts(), "event": "trade_open", "trade_id": trade_id,
               "slug": m.slug, "coin": t["coin"], "window": "daily",
               "side": side, "llm_p": t["llm_p"], "ref": t["ref"],
               "edge": t["edge"], "size_usd": args.size,
               "entry_price": fill, "llm_reason": t["llm_reason"]}, audit_path)
        print(f"  [+] {m.slug[:48]:48s} {side:3s} llm_p={t['llm_p']:.3f} "
              f"ref={t['ref']:.3f} edge={t['edge']:+.3f} fill={fill:.3f}")

        # 入库：settle_worker 自动结算（与 5m 盘同一管道）
        if not args.no_db:
            try:
                from polytrader import db
                rec = build_db_rec(t, mode="simulate")
                n = db.insert_pending([rec])
                print(f"      -> db inserted={n} (trade_id={trade_id})")
            except Exception as e:
                print(f"      !! db insert FAILED: {e}")

    print(f"\ntrades: {len(trades)} (skipped_bad_price: {skipped})")

    # 等待结算（可选；日级盘建议由 settle_worker 结算，--wait 用于联调）
    if args.wait > 0 and trades:
        import csv as _csv
        settle_csv = OUT_DIR / f"equity_settlements_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        with open(settle_csv, "w", newline="", encoding="utf-8") as fh:
            w = _csv.writer(fh)
            w.writerow(["ts", "trade_id", "slug", "coin", "window", "side",
                        "entry_price", "size_usd", "settle_yes", "win", "pnl"])
            deadline = time.time() + args.wait
            remaining = {t["slug"]: t for t in trades}
            while remaining and time.time() < deadline:
                time.sleep(10)
                for slug in list(remaining):
                    settle = fetch_settlement(http, slug)
                    if settle is None:
                        continue
                    t = remaining.pop(slug)
                    win = (t["side"] == "YES" and settle == 1.0) or \
                          (t["side"] == "NO" and settle == 0.0)
                    t["settle_yes"] = settle
                    t["win"] = 1 if win else 0
                    t["pnl"] = round((args.size / t["entry_price"]) *
                                     (1.0 if win else 0.0) - args.size, 2)
                    w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                t["trade_id"], slug, t["coin"], "daily",
                                t["side"], t["entry_price"], args.size,
                                settle, 1 if win else 0, t["pnl"]])
                    fh.flush()
                    print(f"  settled {slug}: {t['side']} win={win} pnl=${t['pnl']:+.2f}")
        print(f"settlements csv: {settle_csv}")

    # 结果文件（供 backfill / 对账）
    out_path = OUT_DIR / f"equity_sim_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(
        {"signals": len(signals), "trades": trades,
         "evaluations": strat.last_evaluations,
         "config": {"min_edge": args.min_edge, "min_liquidity": args.min_liquidity,
                    "size_usd_per_trade": args.size}},
        indent=2, ensure_ascii=False))
    settled = [t for t in trades if t.get("pnl") is not None]
    if settled:
        total = sum(t["pnl"] for t in settled)
        wins = sum(1 for t in settled if t["win"])
        print(f"settled={len(settled)}/{len(trades)} wins={wins} "
              f"total_pnl=${total:+.2f}")
    print(f"saved: {out_path}")
    print(f"audit:  {audit_path}")
    if not args.no_db and trades:
        print("→ pending_trades 已入库，由 settle_worker 自动结算 "
              "(`.venv/bin/python scripts/settle_worker.py status`)")
    return 0


def _ts() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _Tee:
    """同时写 stdout 和日志文件。"""

    def __init__(self, path: str):
        self.fh = open(path, "a", encoding="utf-8")

    def write(self, s: str):
        sys.__stdout__.write(s)
        self.fh.write(s)
        self.fh.flush()

    def flush(self):
        sys.__stdout__.flush()
        self.fh.flush()


if __name__ == "__main__":
    raise SystemExit(main())
