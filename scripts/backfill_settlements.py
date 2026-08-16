"""补结算：对 run_llm_loop 结果文件中未结算（pnl=null）的交易补查结算并追加事件。

用法: .venv/bin/python scripts/backfill_settlements.py [--results backtest_results/llm_results_*.jsonl]
输出: backtest_results/backfill_<ts>.csv + 追加 trade_settled 事件到结果文件
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.data.http_client import HttpClient

HTTP = HttpClient(proxy="http://127.0.0.1:7897", timeout=15)


def _settle_from_market(m: dict) -> float | None:
    """从市场 dict 提取 YES 结算价（outcomePrices[0] 为 0/1 才算已结算）。"""
    prices = m.get("outcomePrices") or ""
    try:
        prices = json.loads(prices) if isinstance(prices, str) else prices
    except Exception:
        return None
    if not prices:
        return None
    try:
        yes = float(prices[0])
    except (TypeError, ValueError):
        return None
    if yes in (0.0, 1.0):
        return yes
    return None


def fetch_settle(slug: str) -> float | None:
    """按 slug 查结算价（YES 结算 1.0/0.0；未结算返回 None）。

    两级查询（2026-08-15 修复衍生盘结算）：
    1. events/keyset?slug=（主市场路径，现有逻辑）
    2. /markets?slug= 直查（衍生盘如 -1pt5/-away 后缀不在 events 列表，
       曾导致已成交单永久 pending 占持仓名额）
    """
    try:
        resp = HTTP.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                             f"slug={slug}&limit=10&locale=en")
    except Exception:
        resp = None
    events = resp if isinstance(resp, list) else (resp or {}).get("events", [])
    for ev in events:
        for m in ev.get("markets", []) or []:
            if m.get("slug") != slug:
                continue
            yes = _settle_from_market(m)
            if yes is not None:
                return yes
            return None
    # 回退：gamma /markets 直查（衍生盘子市场）
    # 注意：已结算市场不在默认查询结果——必须带 closed=true 才能查到
    # （2026-08-16 实测：结算前 closed=false 可查；结算后需 closed=true，
    #  否则 settle_worker 永远查不到 → 单子永久 pending 占持仓名额）
    try:
        resp2 = HTTP.get_json("https://gamma-api.polymarket.com/markets",
                              params={"slug": slug, "limit": 5,
                                      "closed": "true"})
    except Exception:
        return None
    items = resp2 if isinstance(resp2, list) else \
        (resp2 or {}).get("data", resp2.get("markets", []))
    for m in items:
        if m.get("slug") != slug:
            continue
        return _settle_from_market(m)
    return None


# ---- settle_v2：CLOB 持仓结算（权威数据源，gamma 降级为回退）----
# 2026-08-16 设计：live 单（有 order_id）优先用 CLOB 持仓判定——
#   positions（当前持仓 curPrice ∈ {0,1}）= 结算结果；
#   closed-positions + activity REDEEM = 已结算赎回；
#   gamma slug 查询仅作为回退（衍生盘/延迟问题由此根治）。

def fetch_clob_positions_map(user: str) -> dict[str, dict]:
    """当前持仓：asset -> {curPrice, size, avgPrice}（data-api 公开端点）。"""
    try:
        data = HTTP.get_json("https://data-api.polymarket.com/positions",
                             params={"user": user, "limit": 500})
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for p in (data if isinstance(data, list) else []):
        asset = str(p.get("asset", "") or "")
        if asset:
            out[asset] = {"curPrice": p.get("curPrice"),
                          "size": p.get("size"),
                          "avgPrice": p.get("avgPrice")}
    return out


def fetch_clob_closed_map(user: str) -> dict[str, dict]:
    """已平仓/已结算：asset -> {curPrice, realizedPnl}（公开端点）。"""
    try:
        data = HTTP.get_json("https://data-api.polymarket.com/closed-positions",
                             params={"user": user, "limit": 500})
    except Exception:
        return {}
    out: dict[str, dict] = {}
    for p in (data if isinstance(data, list) else []):
        asset = str(p.get("asset", "") or "")
        if asset:
            out[asset] = {"curPrice": p.get("curPrice"),
                          "realizedPnl": p.get("realizedPnl")}
    return out


def fetch_asset_actions(user: str, lookback_h: float = 48.0) -> dict[str, set[str]]:
    """钱包活动中的资产动作（REDEEM=结算赎回 / SELL=手动平仓）——区分依据。

    公开 /activity 端点；仅统计 lookback_h 内的动作。
    """
    try:
        data = HTTP.get_json("https://data-api.polymarket.com/activity",
                             params={"user": user, "limit": 500})
    except Exception:
        return {}
    import time
    cutoff = time.time() - lookback_h * 3600
    out: dict[str, set[str]] = {}
    for a in (data if isinstance(data, list) else []):
        try:
            ts = float(a.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and ts < cutoff:
            continue
        asset = str(a.get("asset", "") or "")
        if not asset:
            continue
        atype = str(a.get("type", "")).upper()
        side = str(a.get("side", "")).upper()
        if atype == "REDEEM":
            out.setdefault(asset, set()).add("REDEEM")
        elif atype == "TRADE" and side == "SELL":
            out.setdefault(asset, set()).add("SELL")
    return out


def settle_from_clob(asset: str, positions: dict, closed: dict,
                     actions: dict) -> float | None:
    """按 CLOB 持仓判定结算价（YES 结算 1.0/0.0；无法判定 None）。

    优先级：
    1. 当前持仓 curPrice ∈ {0,1} → 直接结算（权威，无平仓歧义）
    2. 持仓消失 + closed 有该资产 + activity 有 REDEEM → 结算赎回
       （closed curPrice=1 → 1.0；=0 → 0.0）
    3. 其余（含手动平仓 SELL）→ None（由调用方回退 gamma）
    """
    pos = positions.get(asset)
    if pos is not None:
        try:
            cur = float(pos.get("curPrice"))
        except (TypeError, ValueError):
            cur = None
        if cur in (0.0, 1.0):
            return cur
    cl = closed.get(asset)
    if cl is not None and "REDEEM" in (actions.get(asset) or set()):
        try:
            cur = float(cl.get("curPrice"))
        except (TypeError, ValueError):
            cur = None
        if cur in (0.0, 1.0):
            return cur
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=str, default="")
    args = ap.parse_args()

    if args.results:
        results_path = Path(args.results)
    else:
        results_path = sorted(Path("backtest_results").glob("llm_results_*.jsonl"))[-1]
    print(f"results: {results_path.name}")

    # 收集未结算交易（round 事件里的 trades + pnl=null），
    # 跳过已 backfill 过的（results 中已有 trade_settled 事件）
    pending = {}
    settled_ids = set()
    for line in results_path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("type") == "round":
            for t in rec.get("trades", []):
                if t.get("pnl") is None:
                    pending[t["slug"]] = t
        elif rec.get("type") == "trade_settled":
            if rec.get("trade_id"):
                settled_ids.add(rec["trade_id"])
    pending = {s: t for s, t in pending.items()
               if t.get("trade_id") not in settled_ids}
    print(f"pending unsettled trades: {len(pending)}")

    ts = time.strftime("%Y%m%d_%H%M%S")
    out_csv = Path("backtest_results") / f"backfill_{ts}.csv"
    fixed = []
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "trade_id", "slug", "coin", "window", "side",
                    "entry_price", "size_usd", "settle_yes", "win", "pnl"])
        for slug, t in sorted(pending.items()):
            settle = fetch_settle(slug)
            if settle is None:
                print(f"  still unsettled: {slug}")
                continue
            win = (t["side"] == "YES" and settle == 1.0) or \
                  (t["side"] == "NO" and settle == 0.0)
            pnl = round((t["size_usd"] / t["entry_price"]) * (1.0 if win else 0.0)
                        - t["size_usd"], 2)
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        t.get("trade_id"), slug, t.get("coin"),
                        t.get("window"), t["side"], t["entry_price"],
                        t["size_usd"], settle, 1 if win else 0, pnl])
            # 追加事件到结果文件
            with open(results_path, "a", encoding="utf-8") as rf:
                rf.write(json.dumps({
                    "type": "trade_settled", "round": t.get("round"),
                    "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "trade_id": t.get("trade_id"), "slug": slug,
                    "coin": t.get("coin"), "window": t.get("window"),
                    "side": t["side"], "entry_price": t["entry_price"],
                    "size_usd": t["size_usd"], "settle_yes": settle,
                    "win": 1 if win else 0, "pnl": pnl,
                    "backfilled": True}, ensure_ascii=False) + "\n")
            fixed.append((slug, settle, win, pnl))
            print(f"  fixed {slug}: settle_yes={settle} win={win} pnl=${pnl:+.2f}")

    wins = sum(1 for _, _, w, _ in fixed if w)
    total = sum(p for _, _, _, p in fixed)
    print(f"\nbackfilled={len(fixed)} wins={wins} total_pnl=${total:+.2f}")
    print(f"saved: {out_csv}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
