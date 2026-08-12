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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.data.clob_client import ClobClient
from polytrader.data.http_client import HttpClient
from polytrader.models import Market, Outcome
from polytrader.strategies.llm_updown import LLMUpdownStrategy

COINS = ["btc", "eth", "sol", "xrp", "doge", "hype", "bnb"]
SIZE_USD = 1.0  # 每笔固定仓位（--size 可覆盖）


def fetch_windows(http, coin_map):
    """当前 5m/15m 窗口市场 dict[slug] = Market + coin/window。"""
    now = int(time.time())
    w5 = (now // 300) * 300
    w15 = (now // 900) * 900
    slugs = [f"{c}-updown-{w}-{ts}" for c in COINS for w, ts in (("5m", w5), ("15m", w15))]
    resp = http.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                         "&".join(f"slug={s}" for s in slugs) + "&limit=100&locale=en")
    events = resp if isinstance(resp, list) else resp.get("events", [])
    out = {}
    for ev in events:
        for m in ev.get("markets", []) or []:
            slug = m.get("slug", "")
            if "updown" not in slug:
                continue
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
                slug=slug, end_date=m.get("endDate", ""),
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
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:7890")
    ap.add_argument("--wait", type=int, default=330, help="等待结算秒数（0=不等）")
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--coins", default=",".join(COINS))
    ap.add_argument("--loop", type=int, default=1, help="连续轮数（每 5m 窗口一轮）")
    ap.add_argument("--size", type=float, default=SIZE_USD,
                    help="每笔固定仓位 USD（默认 $1）")
    args = ap.parse_args()
    size_usd = args.size
    coins = [c for c in args.coins.split(",") if c]
    coin_map = {c: c for c in coins}

    http = HttpClient(proxy=args.proxy, timeout=15)
    clob = ClobClient(http=http)
    from polytrader.config import load_config
    cfg = load_config()
    scorer = LLMScorer(
        api_key=cfg.llm_api_key, base_url=cfg.llm_base_url, model=cfg.llm_model)
    if not scorer.enabled:
        print("LLM not configured (LLM_API_KEY missing)")
        return 1
    strat = LLMUpdownStrategy(scorer, min_edge=args.min_edge, max_markets=20,
                              coin_map=coin_map)

    markets = fetch_windows(http, coin_map)
    print(f"windows: {len(markets)} markets")
    # 盘口快照（记录，模拟成交用 ref 价近似——空壳盘口 taker 不可行）
    books = {}
    for m in markets.values():
        try:
            b = clob.get_book(m.outcomes[0].token_id)
            if b:
                books[m.condition_id] = {"bid": b.best_bid().price if b.best_bid() else None,
                                         "ask": b.best_ask().price if b.best_ask() else None}
        except Exception:
            pass

    signals = strat.scan(list(markets.values()))
    print(f"signals: {len(signals)}")
    trades = []
    for s in signals:
        trades.append({"slug": s.market.slug, "condition_id": s.market.condition_id,
                       "coin": s.market.slug.split("-")[0],
                       "window": "5m" if "-5m-" in s.market.slug else "15m",
                       "side": s.extra.get("side"), "llm_p": round(s.extra.get("llm_p", 0), 4),
                       "ref": round(s.market_price, 4), "edge": round(s.edge, 4),
                       "size_usd": size_usd, "entry_price": round(s.market_price, 4),
                       "reason": s.reason,
                       "llm_reason": s.extra.get("llm_reason"),
                       "book": books.get(s.market.condition_id)})
        print(f"  {trades[-1]['slug']:34s} {trades[-1]['side']:3s} "
              f"llm_p={trades[-1]['llm_p']:.3f} ref={trades[-1]['ref']:.3f} "
              f"edge={trades[-1]['edge']:+.3f} book={trades[-1]['book']}")
        if trades[-1]["llm_reason"]:
            print(f"      llm reason: {trades[-1]['llm_reason']}")
    evaluations = [dict(e, round=round_no) for e in strat.last_evaluations]

    # 等待结算并验证
    if args.wait > 0 and trades:
        print(f"\nwaiting up to {args.wait}s for settlement...")
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
                    print(f"  settled {t['slug']}: {t['side']} win={win} "
                          f"pnl=${t['pnl']:+.2f}")
        for cid, t in remaining.items():
            t["settle_yes"] = None
            t["pnl"] = None
            print(f"  unsettled: {t['slug']}")

    out_dir = Path("backtest_results")
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
