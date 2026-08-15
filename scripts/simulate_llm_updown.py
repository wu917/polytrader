"""LLM updown 模拟测算：最新盘口 → LLM 判断 → 模拟成交 → 结算验证。

流程：
1. 拉当前 5m/15m 窗口市场（全部币种，/events/keyset）+ 盘口
2. 每市场 LLM 判断 P(涨)，edge = |P - ref|，> 阈值 → 模拟成交（$100/笔，ref 价近似）
3. 等待窗口结算（最长 wait_s），拉结算结果，计算每笔盈亏
4. 保留 backtest_results/llm_updown_sim_<ts>.json

用法: .venv/bin/python scripts/simulate_llm_updown.py [--wait 330] [--min-edge 0.05]
"""
import argparse
import json
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.data.clob_client import ClobClient
from polytrader.data.http_client import HttpClient
from polytrader.models import Market, Outcome
from polytrader.strategies.llm_updown import LLMUpdownStrategy

COINS = ["btc", "eth", "bnb", "sol", "hype"]  # 限定交易币种
SIZE_USD = 1.0  # 每笔固定仓位（--size 可覆盖）


def load_seen(seen_file: str | None) -> set:
    """读取已交易 slug 集合（跨轮持久去重）。"""
    if not seen_file or not Path(seen_file).exists():
        return set()
    return {line.strip() for line in Path(seen_file).read_text().splitlines() if line.strip()}


def save_seen(seen_file: str | None, slugs: set):
    """写回已交易 slug 集合。"""
    if not seen_file:
        return
    Path(seen_file).parent.mkdir(parents=True, exist_ok=True)
    Path(seen_file).write_text("\n".join(sorted(slugs)) + "\n")


def audit(rec: dict, path: str | None):
    """写审计 JSONL（与 HTTP/LLM 审计同文件）。"""
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass


def fetch_windows(http, coin_map, min_secs_left: int = 30,
                  windows: tuple = ("5m", "15m")):
    """当前窗口市场 dict[slug] = Market + coin/window。

    过滤已结束/即将结束的窗口（剩余 < min_secs_left 秒跳过——避免对
    已结束窗口评估导致 LLM"窗口已结束取默认值"的垃圾判断）。
    windows: ("5m",) 仅 5 分钟市场；("15m",) 仅 15 分钟。
    """
    now = int(time.time())
    w5 = (now // 300) * 300
    w15 = (now // 900) * 900
    slugs = [f"{c}-updown-{w}-{ts}" for c in COINS
             for w, ts in (("5m", w5), ("15m", w15)) if w in windows]
    resp = http.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                         "&".join(f"slug={s}" for s in slugs) + "&limit=100&locale=en")
    events = resp if isinstance(resp, list) else resp.get("events", [])
    out = {}
    for ev in events:
        for m in ev.get("markets", []) or []:
            slug = m.get("slug", "")
            if "updown" not in slug:
                continue
            end_date = m.get("endDate", "")
            end_ts = 0
            if "T" in end_date:
                try:
                    import datetime as _dt
                    end_ts = int(_dt.datetime.strptime(
                        end_date[:19], "%Y-%m-%dT%H:%M:%S")
                        .replace(tzinfo=_dt.timezone.utc).timestamp())  # endDate 为 UTC
                except (ValueError, OSError):
                    end_ts = 0
            if end_ts and end_ts - now < min_secs_left:
                continue  # 窗口已结束/即将结束，跳过
            prices = m.get("outcomePrices") or ""
            tokens = m.get("clobTokenIds") or ""
            try:
                prices = json.loads(prices) if isinstance(prices, str) else prices
                tokens = json.loads(tokens) if isinstance(tokens, str) else tokens
            except Exception:
                continue
            if len(tokens) < 2 or len(prices) < 2:
                continue
            out[slug] = Market(
                condition_id=m.get("conditionId", ""), question=m.get("question", ""),
                slug=slug, end_date=end_date,
                liquidity=float(m.get("liquidity") or 0), closed=False, active=True,
                outcomes=[Outcome(outcome_id="o0", token_id=tokens[0],
                                  price=str(prices[0]), name="Yes"),
                          Outcome(outcome_id="o1", token_id=tokens[1],
                                  price=str(prices[1]), name="No")],
            )
    return out


def fetch_settlement(http, slug: str) -> float | None:
    """结算结果：YES 结算价（1.0=涨, 0.0=跌, None=未结算）——用 keyset 端点。"""
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--wait", type=int, default=330, help="等待结算秒数（0=不等）")
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--loop", type=int, default=1, help="连续轮数（每 5m 窗口一轮）")
    ap.add_argument("--size", type=float, default=SIZE_USD,
                    help="每笔固定仓位 USD（默认 $1）")
    ap.add_argument("--audit-dir", type=str, default="backtest_results",
                    help="审计 JSONL 输出目录")
    ap.add_argument("--seen-file", type=str,
                    default="backtest_results/seen_slugs.txt",
                    help="已交易 slug 持久化文件（跨轮去重）")
    ap.add_argument("--settle-csv", type=str, default="",
                    help="结算 CSV 路径（默认 audit-dir/settlements_<ts>.csv）")
    ap.add_argument("--windows", type=str, default="5m,15m",
                    help="参与的市场窗口：5m / 15m / 5m,15m")
    args = ap.parse_args()
    win_filter = tuple(w.strip() for w in args.windows.split(",") if w.strip())
    size_usd = args.size
    audit_path = str(Path(args.audit_dir) /
                     f"audit_llm_{time.strftime('%Y%m%d_%H%M%S')}.jsonl")
    settle_csv = args.settle_csv or str(
        Path(args.audit_dir) / f"settlements_{time.strftime('%Y%m%d_%H%M%S')}.csv")
    seen = load_seen(args.seen_file)
    coins = [c for c in args.coins.split(",") if c]
    coin_map = {c: c for c in coins}

    http = HttpClient(proxy=args.proxy, timeout=15, audit_path=audit_path)
    clob = ClobClient(http=http)
    from polytrader.config import load_config
    cfg = load_config()
    scorer = LLMScorer(
        api_key=cfg.llm_api_key, base_url=cfg.llm_base_url, model=cfg.llm_model,
        audit_path=audit_path)
    if not scorer.enabled:
        print("LLM not configured (LLM_API_KEY missing)")
        return 1
    strat = LLMUpdownStrategy(scorer, min_edge=args.min_edge, max_markets=20,
                              coin_map=coin_map)

    markets = fetch_windows(http, coin_map, windows=win_filter)
    # 去重：过滤已交易盘口（跨轮持久）
    already = sum(1 for s in markets if s in seen)
    markets = {slug: m for slug, m in markets.items() if slug not in seen}
    print(f"windows: {len(markets)} markets (after dedup, already traded: {already})")
    # 盘口快照（模拟市价单成交：吃单侧盘口价，总是成交——与实盘市价化一致）
    books = {}
    for m in markets.values():
        try:
            b = clob.get_book(m.outcomes[0].token_id)
            if b:
                books[m.condition_id] = {"bid": b.best_bid().price if b.best_bid() else None,
                                         "ask": b.best_ask().price if b.best_ask() else None}
        except Exception:
            pass

    # 只接受合理成交价（过滤空壳盘口坏价：0.97 吃单赢只赚 3%）
    MIN_FILL, MAX_FILL = 0.25, 0.85

    def sim_market_price(book: dict | None, side: str, ref: float) -> float:
        """模拟市价单成交价：YES→ask；NO→1-bid（总成交，反映吃单成本）。
        盘口无数据时回退 ref 价。
        """
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

    signals = strat.scan(list(markets.values()))
    print(f"signals: {len(signals)}")
    trades = []
    skipped_bad = 0
    for s in signals:
        side = s.extra.get("side")
        fill = sim_market_price(books.get(s.market.condition_id), side,
                                s.market_price) if side else None
        if fill is None:
            continue
        if not (MIN_FILL <= fill <= MAX_FILL):
            skipped_bad += 1
            print(f"  {s.market.slug:34s} {side:3s} 成交价{fill} 超范围[{MIN_FILL},{MAX_FILL}] 过滤")
            audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                   "event": "trade_skipped_bad_price", "slug": s.market.slug,
                   "side": side, "fill": fill,
                   "reason": f"fill not in [{MIN_FILL},{MAX_FILL}]"}, audit_path)
            continue
        trade_id = str(uuid.uuid4())[:8]
        trades.append({"trade_id": trade_id,
                       "slug": s.market.slug, "condition_id": s.market.condition_id,
                       "coin": s.market.slug.split("-")[0],
                       "window": "5m" if "-5m-" in s.market.slug else "15m",
                       "side": side, "llm_p": round(s.extra.get("llm_p", 0), 4),
                       "ref": round(s.market_price, 4), "edge": round(s.edge, 4),
                       "size_usd": size_usd, "entry_price": fill,
                       "reason": s.reason,
                       "llm_reason": s.extra.get("llm_reason"),
                       "model": s.extra.get("model"),
                       "book": books.get(s.market.condition_id)})
        audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
               "event": "trade_open", "trade_id": trade_id,
               "slug": trades[-1]["slug"], "coin": trades[-1]["coin"],
               "window": trades[-1]["window"], "side": trades[-1]["side"],
               "llm_p": trades[-1]["llm_p"], "ref": trades[-1]["ref"],
               "edge": trades[-1]["edge"], "size_usd": size_usd,
               "entry_price": trades[-1]["entry_price"],
               "llm_reason": trades[-1]["llm_reason"]}, audit_path)
        seen.add(trades[-1]["slug"])
        save_seen(args.seen_file, seen)
        print(f"  {trades[-1]['slug']:34s} {trades[-1]['side']:3s} "
              f"llm_p={trades[-1]['llm_p']:.3f} ref={trades[-1]['ref']:.3f} "
              f"edge={trades[-1]['edge']:+.3f} book={trades[-1]['book']}")
        if trades[-1]["llm_reason"]:
            print(f"      llm reason: {trades[-1]['llm_reason']}")
    evaluations = [dict(e, round=1) for e in strat.last_evaluations]

    # 等待结算并验证
    csv_fh = None
    if args.wait > 0 and trades:
        print(f"\nwaiting up to {args.wait}s for settlement...")
        # 结算单独 CSV（不与审计/交易 JSON 混在一起）
        import csv as _csv
        csv_fh = open(settle_csv, "w", newline="", encoding="utf-8")
        _csv.writer(csv_fh).writerow(
            ["ts", "trade_id", "slug", "coin", "window", "side",
             "entry_price", "size_usd", "settle_yes", "win", "pnl"])
        deadline = time.time() + args.wait
        remaining = {t["slug"]: t for t in trades}
        while remaining and time.time() < deadline:
            time.sleep(10)
            for slug in list(remaining):
                settle = fetch_settlement(http, slug)
                if settle is not None:
                    t = remaining.pop(slug)
                    win = (t["side"] == "YES" and settle == 1.0) or \
                          (t["side"] == "NO" and settle == 0.0)
                    t["settle_yes"] = settle
                    t["pnl"] = round((size_usd / t["entry_price"]) * (1.0 if win else 0.0)
                                     - size_usd, 2)
                    audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                           "event": "trade_settled", "trade_id": t.get("trade_id"),
                           "slug": slug, "side": t["side"],
                           "settle_yes": settle, "win": win,
                           "entry_price": t["entry_price"],
                           "size_usd": size_usd, "pnl": t["pnl"]}, audit_path)
                    _csv.writer(csv_fh).writerow(
                        [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         t.get("trade_id"), slug, t["coin"], t["window"],
                         t["side"], t["entry_price"], size_usd, settle,
                         1 if win else 0, t["pnl"]])
                    csv_fh.flush()
                    print(f"  settled {t['slug']}: {t['side']} win={win} "
                          f"pnl=${t['pnl']:+.2f}")
        for cid, t in remaining.items():
            t["settle_yes"] = None
            t["pnl"] = None
            # 未结算也写入 CSV（settle_yes 空）
            _csv.writer(csv_fh).writerow(
                [time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 t.get("trade_id"), cid, t["coin"], t["window"],
                 t["side"], t["entry_price"], size_usd, "", "", ""])
            csv_fh.flush()
            print(f"  unsettled: {t['slug']}")
        csv_fh.close()
        print(f"  settlements csv: {settle_csv}")

    out_dir = Path(args.audit_dir)
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"llm_updown_sim_{time.strftime('%Y%m%d_%H%M%S')}.json"
    path.write_text(json.dumps(
        {"signals": len(signals), "trades": trades, "evaluations": evaluations,
         "config": {"min_edge": args.min_edge, "wait": args.wait,
                    "size_usd_per_trade": size_usd}},
        indent=2, ensure_ascii=False))
    settled = [t for t in trades if t.get("pnl") is not None]
    if settled:
        total = sum(t["pnl"] for t in settled)
        wins = sum(1 for t in settled if t["pnl"] > 0)
        print(f"\nsettled={len(settled)}/{len(trades)} wins={wins} "
              f"total_pnl=${total:+.2f}")
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
