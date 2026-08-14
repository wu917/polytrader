"""通用事件盘扫描：全量活跃市场 → 过滤（成交量/二元/ref 区间/非价格非体育）
→ CLOB 盘口 → LLM 评估（方向 + 收益比 RR + 期望值 EV）→ 信号输出。

复用：
- scan_equity_updown.to_market / PROXY（市场转换与代理）
- simulate_equity_updown.build_db_rec（入库记录）
- LLMBookStrategy 评估骨架（经 EventMarketStrategy 加 RR/EV）

用法:
  PYTHONPATH=. .venv/bin/python scripts/scan_event_markets.py [--list-only]
  PYTHONPATH=. .venv/bin/python scripts/scan_event_markets.py --no-db \
      --min-vol 5000 --min-edge 0.05 --min-rr 1.5
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.config import load_config
from polytrader.data.clob_client import ClobClient
from polytrader.data.http_client import HttpClient
from polytrader.models import Market, Outcome
from polytrader.strategies.event_market import EventMarketStrategy

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "backtest_results"
PROXY = "socks5h://127.0.0.1:7890"
GAMMA_MARKETS = "https://gamma-api.polymarket.com/markets"

# 排除：价格类（above/below/reach/hit/updown）与体育/电竞（slug 前缀 + question）
PRICE_RE = re.compile(
    r"above-|below-|reach-|dip-to|up-or-down|updown|closes-above|hit-|total-|over-|under-")
SPORTS_PREFIX = re.compile(
    r"^(lol|dota2|cs2|crint|ere-|atp|wta|nba|nfl|mlb|nhl|soccer|tennis|ufc|f1|chess|"
    r"cricket|golf|boxing|lec-|lck-|lpl-|kbl|kbo|cfl|ncaa|epl|champions|uefa|wc-|sl-|"
    r"fr-|it-|es-|nhl-|clf-)")
# question 层体育特征（slug 前缀未覆盖的，如 "Will X win the 2027 NBA Finals"）
SPORTS_Q_RE = re.compile(
    r"\b(nba|nfl|mlb|nhl|ncaa|finals|championship|matchup|game \d|semifinal|quarterfinal|"
    r"playoff|winner of|to win the (champions|league|cup|title|masters)|grand slam|"
    r"world cup|tournament)\b", re.IGNORECASE)


def is_event_market(slug: str, question: str = "") -> bool:
    """事件盘判定：非价格类、非体育/电竞。"""
    if PRICE_RE.search(slug):
        return False
    if SPORTS_PREFIX.match(slug):
        return False
    if SPORTS_Q_RE.search(question):
        return False
    return True


def fetch_all_active(http: HttpClient, max_pages: int = 10,
                     min_vol: float = 0.0) -> list[dict]:
    """分页拉取全量活跃市场（按 24h 成交量降序），可提前截断。"""
    out: dict[str, dict] = {}
    for page in range(max_pages):
        try:
            resp = http.get_json(GAMMA_MARKETS, params={
                "limit": "100", "active": "true", "closed": "false",
                "order": "volume24hr", "ascending": "false",
                "offset": str(page * 100)})
        except Exception as e:
            print(f"  page {page} fetch failed: {e}")
            time.sleep(3)
            continue
        items = resp if isinstance(resp, list) else []
        if not items:
            break
        stop = False
        for m in items:
            slug = m.get("slug", "")
            if not slug:
                continue
            vol = float(m.get("volume24hr") or 0)
            if vol < min_vol:
                stop = True  # 已排序，后续只会更小
                continue
            out[slug] = m
        print(f"  page {page}: +{len(items)} (累计 {len(out)}, vol 门槛 ${min_vol:,.0f})")
        if stop or len(items) < 100:
            break
        time.sleep(1.0)  # 温和频率，避免 gamma 限流
    return list(out.values())


def to_market(m: dict) -> Market:
    """Gamma market dict → Market（与 scan_equity_updown 同构）。"""
    tokens = json.loads(m.get("clobTokenIds") or "[]") or []
    prices = json.loads(m.get("outcomePrices") or "[]") or []
    outcomes = []
    names = m.get("outcomes") or []
    for i, t in enumerate(tokens):
        outcomes.append(Outcome(
            outcome_id=f"o{i}", token_id=t,
            price=str(prices[i]) if i < len(prices) else "",
            name=names[i] if i < len(names) else ""))
    return Market(
        condition_id=m.get("conditionId", ""),
        question=m.get("question", ""),
        slug=m.get("slug", ""),
        category=m.get("category", ""),
        description=m.get("description", ""),
        end_date=m.get("endDate", ""),
        liquidity=float(m.get("liquidity") or 0),
        volume=float(m.get("volume24hr") or 0),
        closed=bool(m.get("closed")),
        active=bool(m.get("active")),
        outcomes=outcomes,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-vol", type=float, default=5000.0,
                    help="24h 成交量门槛（默认 $5K）")
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--min-rr", type=float, default=1.5)
    ap.add_argument("--max-markets", type=int, default=50,
                    help="LLM 评估的市场数上限")
    ap.add_argument("--no-db", action="store_true",
                    help="不入库（默认入库 pending_trades 由 settle_worker 结算）")
    ap.add_argument("--size", type=float, default=100.0, help="模拟每笔 USD")
    ap.add_argument("--list-only", action="store_true",
                    help="只列候选盘，不调 LLM")
    ap.add_argument("--log", type=str, default="", help="日志文件路径")
    args = ap.parse_args()

    if args.log:
        sys.stdout = _Tee(args.log)  # type: ignore[assignment]

    http = HttpClient(proxy=PROXY, timeout=20)
    print(f"拉取全量活跃市场（vol ≥ ${args.min_vol:,.0f}）...")
    mkts = fetch_all_active(http, min_vol=args.min_vol)
    print(f"活跃市场: {len(mkts)}")

    # 过滤事件盘
    event = [m for m in mkts
             if is_event_market(m.get("slug", ""), m.get("question", ""))]
    print(f"事件盘（非价格/体育）: {len(event)}")

    # ref ∈ [0.05, 0.95]
    tradable = []
    for m in event:
        prices = json.loads(m.get("outcomePrices") or "[]") or []
        if not prices:
            continue
        try:
            ref = float(prices[0])
        except (TypeError, ValueError):
            continue
        if 0.05 <= ref <= 0.95:
            tradable.append(m)
    print(f"ref∈[0.05,0.95] 可交易: {len(tradable)}")

    for m in sorted(tradable, key=lambda x: -float(x.get("volume24hr") or 0))[:40]:
        prices = json.loads(m.get("outcomePrices") or "[]") or []
        ref = float(prices[0]) if prices else float("nan")
        print(f"  vol=${float(m.get('volume24hr') or 0):>9,.0f} ref={ref:.2f} "
              f"| {m.get('slug','')[:58]}")

    if args.list_only or not tradable:
        return 0

    # LLM 评估
    cfg = load_config()
    scorer = LLMScorer(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
                       model=cfg.llm_model)
    if not scorer.enabled:
        print("!! LLM not configured (LLM_API_KEY missing)")
        return 1
    strat = EventMarketStrategy(scorer, min_edge=args.min_edge, min_rr=args.min_rr,
                                max_markets=args.max_markets)

    markets = [to_market(m) for m in tradable[:args.max_markets]]
    print(f"\nevaluating {len(markets)} markets (LLM)...")
    # 盘口快照（LLMBookStrategy 需要 books）
    clob = ClobClient(http=http)
    books = {}
    for m in markets:
        try:
            b = clob.get_book(m.outcomes[0].token_id)
            if b:
                books[m.outcomes[0].token_id] = b
        except Exception:
            pass
    print(f"books fetched: {len(books)}/{len(markets)}")

    signals = strat.scan(markets, books)
    print(f"\nsignals: {len(signals)} (edge ≥{args.min_edge}, RR ≥{args.min_rr}, EV>0)")
    for s in signals:
        side = s.extra.get("side", "?")
        print(f"  >>> BUY {side} {s.market.slug[:50]:50s} "
              f"p_buy={s.extra.get('buy_price')} edge={s.edge:+.3f} "
              f"rr={s.extra.get('rr')} ev={s.extra.get('ev'):+}")
        if s.extra.get("llm_reason"):
            print(f"      LLM: {s.extra['llm_reason']}")

    # 入库（模拟，settle_worker 结算）
    if not args.no_db and signals:
        from polytrader import db
        from scripts.simulate_equity_updown import build_db_rec
        inserted = 0
        for s in signals:
            side = s.extra.get("side")
            if side not in ("YES", "NO"):
                continue
            rec = build_db_rec({
                "trade_id": str(uuid.uuid4())[:8],
                "slug": s.market.slug,
                "coin": s.market.slug.split("-")[0],
                "window": "event",
                "side": side,
                "entry_price": round(float(s.extra.get("buy_price", s.market_price)), 4),
                "size_usd": args.size,
                "llm_p": round(float(s.extra.get("llm_p", 0)), 4),
                "ref": round(float(s.market_price), 4),
                "edge": round(float(s.edge), 4),
                "llm_reason": s.extra.get("llm_reason"),
                "model": s.extra.get("model"),
                "results_file": str(OUT_DIR / f"event_results_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"),
            }, mode="simulate")
            try:
                inserted += db.insert_pending([rec])
            except Exception as e:
                print(f"  !! db insert FAILED {s.market.slug}: {e}")
        print(f"db inserted: {inserted} (pending_trades, window='event')")
    return 0


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
