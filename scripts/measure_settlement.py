"""D2 PTB 测算：已结算 5m 窗口的结算方向 vs Binance 窗口实际方向。

验证：Polymarket 结算（Chainlink 锚）与 Binance 价格方向的一致性——
若 85%+ 同侧（论文结论），则 Binance 可作为 Oracle Gap 策略的有效代理。
取最近 N 个已结算 btc 5m 窗口：T0/T1 = 窗口边界，Binance klines 1m 开盘价近似。
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.data.http_client import HttpClient

HTTP = HttpClient(proxy="http://127.0.0.1:7897", timeout=15)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--windows", type=int, default=12, help="最近 N 个 5m 窗口")
    args = ap.parse_args()

    now = int(time.time())
    w5 = (now // 300) * 300
    slugs = [f"btc-updown-5m-{w5 - 300 * i}" for i in range(1, args.windows + 1)]
    resp = HTTP.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                         "&".join(f"slug={s}" for s in slugs) + "&limit=100&locale=en")
    events = resp if isinstance(resp, list) else resp.get("events", [])
    markets = {}
    for ev in events:
        for m in ev.get("markets", []) or []:
            slug = m.get("slug", "")
            if "btc-updown-5m" in slug:
                markets[slug] = m

    out_dir = Path("backtest_results")
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"ptb_settlement_{time.strftime('%Y%m%d_%H%M%S')}.json"
    results = []
    for slug, m in sorted(markets.items()):
        ts = int(slug.rsplit("-", 1)[1])
        t0, t1 = ts * 1000, (ts + 300) * 1000
        try:
            k = HTTP.get_json("https://api.binance.com/api/v3/klines",
                              params={"symbol": "BTCUSDT", "interval": "1m",
                                      "startTime": t0 - 60_000, "endTime": t1 + 60_000})
        except Exception as e:
            print(f"  {slug}: binance klines failed: {e}")
            continue
        if not k:
            continue
        # 取 T0 前最近的 1m 收盘 与 T1 前最近的 1m 收盘
        p0 = p1 = None
        for bar in k:
            bt, close = int(bar[0]), float(bar[4])
            if bt <= t0 and (p0 is None or bt > 0):
                p0 = close
            if bt <= t1 and bt > t0:
                p1 = close
        if p0 is None or p1 is None:
            continue
        direction_bin = "UP" if p1 >= p0 else "DOWN"
        prices = m.get("outcomePrices") or ""
        try:
            prices = json.loads(prices) if isinstance(prices, str) else prices
        except Exception:
            continue
        if len(prices) < 1:
            continue
        yes_price = float(prices[0])
        settled = "RESOLVED" if yes_price in (0.0, 1.0) else f"ref={yes_price}"
        direction_poly = "UP" if yes_price == 1.0 else ("DOWN" if yes_price == 0.0 else "?")
        # TWAP 近似：T1 前 30s 的 1s kline 均价（2026-08-07 起 5m 结算用 30s TWAP）
        try:
            k1 = HTTP.get_json("https://api.binance.com/api/v3/klines",
                               params={"symbol": "BTCUSDT", "interval": "1s",
                                       "startTime": t1 - 30_000, "endTime": t1 + 5_000})
        except Exception:
            k1 = []
        twap = None
        if k1:
            twap = sum(float(b[4]) for b in k1) / len(k1)
        direction_twap = ("UP" if twap >= p0 else "DOWN") if twap else None
        match = (direction_twap == direction_poly) if direction_poly != "?" and twap else None
        results.append({"slug": slug, "t0": t0, "binance_dir": direction_bin,
                        "twap30": twap, "twap_dir": direction_twap,
                        "poly_dir": direction_poly, "settled": settled,
                        "match": match})
        print(f"  {slug}: binance={direction_bin} twap30={direction_twap} "
              f"poly={direction_poly} ({settled}) match={match}")

    path.write_text(json.dumps(results, indent=2))
    matched = [r for r in results if r["match"] is True]
    mism = [r for r in results if r["match"] is False]
    print(f"\nwindows={len(results)} matched={len(matched)} mismatch={len(mism)} "
          f"consistency={len(matched) / max(len(matched) + len(mism), 1):.1%}")
    print(f"  (match 对照 TWAP30 近似；binance 瞬时对照见各行)")
    print(f"saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
