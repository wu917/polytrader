"""扫描当日股票/商品 Up-or-Down 盘口并用 LLM 评估（paper 模式）。

流程：
1. public-search 发现当日活跃 up-or-down 盘（NVDA/TSLA/.../SPY/XAUUSD/WTI）
2. Gamma 拉盘口元数据（outcomePrices = 市场隐含 P(涨)）
3. EquityUpdownStrategy：日 K 特征 + 大盘局势 + LLM 判断 → edge
4. 输出信号与评估明细（JSONL + 表格）

用法:
  .venv/bin/python scripts/scan_equity_updown.py [--min-edge 0.05] [--out backtest_results]
  .venv/bin/python scripts/scan_equity_updown.py --list-only   # 只列当日盘口不调 LLM
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.config import load_config
from polytrader.data.http_client import HttpClient
from polytrader.models import Market, Outcome
from polytrader.strategies.equity_context import SYMBOL_MAP
from polytrader.strategies.equity_updown import EquityUpdownStrategy

SEARCH_BASE = "https://gamma-api.polymarket.com/public-search?q="
GAMMA_MARKET = "https://gamma-api.polymarket.com/markets?slug="
PROXY = "http://127.0.0.1:7897"


def discover_daily_updown(http: HttpClient,
                          symbols: list[str] | None = None) -> list[dict]:
    """public-search 各标的 slug 前缀 + 当日 slug 直查，找未结算 up-or-down 盘。

    返回 market 元数据列表（含 outcomePrices）。
    symbols: 白名单前缀（如 ["nvda","spy"]，大小写不敏感）；None/空 = 全部
    17 个 SYMBOL_MAP 标的。
    过滤：closed 或 endDate 已过（public-search 偶有历史盘 closed 未标）。
    public-search 有搜索质量问题（偶漏当日盘，如 SPY），
    因此对每个前缀再用美东日期构造 slug 直查 keyset 补漏。
    """
    allowed = {s.strip().lower() for s in (symbols or []) if s and s.strip()}
    now = int(time.time())
    seen: dict[str, dict] = {}

    def _is_live(m: dict) -> bool:
        slug = m.get("slug", "")
        if "up-or-down" not in slug:
            return False
        if m.get("closed"):
            return False
        end_date = m.get("endDate", "")
        if "T" in end_date:
            try:
                end_ts = int(datetime.strptime(
                    end_date[:19], "%Y-%m-%dT%H:%M:%S")
                    .replace(tzinfo=timezone.utc).timestamp())
            except ValueError:
                end_ts = 0
            if end_ts and end_ts < now:
                return False  # 结算日已过
        return True

    def _add(m: dict):
        slug = m.get("slug", "")
        if slug and slug not in seen:
            seen[slug] = m

    # 1) public-search 全量发现（symbols 白名单过滤）
    prefixes = list(SYMBOL_MAP) if not allowed else [
        p for p in SYMBOL_MAP if p in allowed]
    for prefix in prefixes:
        try:
            resp = http.get_json(SEARCH_BASE + prefix)
        except Exception:
            continue
        items = resp if isinstance(resp, list) else (resp.get("events") or [])
        for ev in items:
            for m in (ev.get("markets") or []):
                if _is_live(m):
                    _add(m)

    # 2) 当日/次日 slug 直查补漏（美东交易日，跳过周末）
    et = ZoneInfo("America/New_York")
    et_today = datetime.now(et).date()

    def _next_trading_day(d):
        d = d + timedelta(days=1)
        while d.weekday() >= 5:  # 周六=5 周日=6
            d += timedelta(days=1)
        return d

    # 今日盘（若今日是交易日且未过结算）与下一交易日盘
    candidates = []
    if et_today.weekday() < 5:
        candidates.append(et_today)
    candidates.append(_next_trading_day(et_today))
    for prefix in prefixes:
        for d in candidates:
            slug = f"{prefix}-up-or-down-on-{d.strftime('%B').lower()}-{d.day}-{d.year}"
            try:
                resp = http.get_json(GAMMA_MARKET + slug)
            except Exception:
                continue
            if not resp:
                continue
            m = resp[0] if isinstance(resp, list) else resp
            if m and _is_live(m):
                _add(m)

    # 按 slug 排序
    return sorted(seen.values(), key=lambda m: m.get("slug", ""))


def to_market(m: dict) -> Market:
    tokens = json.loads(m.get("clobTokenIds") or "[]") or []
    prices = json.loads(m.get("outcomePrices") or "[]") or []
    outcomes = []
    for i, t in enumerate(tokens):
        outcomes.append(Outcome(
            outcome_id=f"o{i}", token_id=t,
            price=str(prices[i]) if i < len(prices) else "",
            name=m.get("outcomes", [])[i] if i < len(m.get("outcomes", [])) else ""))
    return Market(
        condition_id=m.get("conditionId", ""),
        question=m.get("question", ""),
        slug=m.get("slug", ""),
        category="Equity/Commodity",
        description=m.get("description", ""),
        end_date=m.get("endDate", ""),
        liquidity=float(m.get("liquidity") or 0),
        volume=float(m.get("volume") or 0),
        closed=bool(m.get("closed")),
        active=bool(m.get("active")),
        outcomes=outcomes,
    )


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--min-liquidity", type=float, default=0.0)
    ap.add_argument("--out", default="backtest_results",
                    help="评估 JSONL 输出目录")
    ap.add_argument("--list-only", action="store_true",
                    help="只列出当日盘口，不调 LLM")
    args = ap.parse_args()

    http = HttpClient(proxy=PROXY, timeout=15)
    mkts = discover_daily_updown(http)
    print(f"discovered {len(mkts)} active daily up-or-down markets")
    for m in mkts:
        prices = json.loads(m.get("outcomePrices") or "[]") or []
        p_yes = float(prices[0]) if prices else float("nan")
        print(f"  {m.get('slug',''):58s} | P(涨)={p_yes:.3f} "
              f"| liq=${float(m.get('liquidity') or 0):,.0f}")

    if args.list_only or not mkts:
        return 0

    # 过滤流动性
    mkts = [m for m in mkts if float(m.get("liquidity") or 0) >= args.min_liquidity]
    if not mkts:
        print("no markets after liquidity filter")
        return 0

    cfg = load_config()
    scorer = LLMScorer(
        api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
        model=cfg.llm_model)
    if not scorer.enabled:
        print("LLM not configured (LLM_API_KEY missing)")
        return 1
    strat = EquityUpdownStrategy(scorer, min_edge=args.min_edge)

    markets = [to_market(m) for m in mkts]
    signals = strat.scan(markets)
    print(f"\nevaluated={len(strat.last_evaluations)} signals={len(signals)}")

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"equity_scan_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"
    with open(out_path, "w", encoding="utf-8") as fh:
        for ev in strat.last_evaluations:
            fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
    print(f"evaluations: {out_path}")

    for ev in strat.last_evaluations:
        flag = "SIGNAL" if ev["signal"] else "      "
        print(f"  [{flag}] {ev['slug'][:48]:48s} llm_p={ev['llm_p']:.3f} "
              f"ref={ev['ref_yes']:.3f} best_edge={ev['best_edge']:+.3f} "
              f"| {ev['reason'] or ''}")
    for s in signals:
        print(f"\n>>> BUY {s.outcome.name} {s.market.slug} @ {s.market_price:.3f} "
              f"edge={s.edge:+.3f}")
        print(f"    {s.reason}")
        if s.extra.get("llm_reason"):
            print(f"    LLM: {s.extra['llm_reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
