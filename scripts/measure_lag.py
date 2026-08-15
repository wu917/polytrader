"""实时测量：Binance BTC 价格变动 → Polymarket updown 定价修正的滞后（Oracle Gap 代理验证）。

方法：每 2s 同时采样 Binance ticker 价 + Gamma keyset 的 btc-updown-5m/15m ref_yes，
保存 CSV 供事后分析（互相关/事件匹配测滞后）。
用法: .venv/bin/python scripts/measure_lag.py --seconds 300
结果: backtest_results/lag_measure_<ts>.csv
"""
import argparse
import csv
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.data.http_client import HttpClient

HTTP = HttpClient(proxy="http://127.0.0.1:7897", timeout=8)


def binance_price(symbol: str) -> float:
    d = HTTP.get_json(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}")
    return float(d["price"])


def updown_ref() -> dict:
    """当前窗口 btc 5m/15m 的 ref_yes。"""
    now = int(time.time())
    w5 = (now // 300) * 300
    w15 = (now // 900) * 900
    slugs = [f"btc-updown-5m-{w5}", f"btc-updown-15m-{w15}"]
    try:
        resp = HTTP.get_json("https://gamma-api.polymarket.com/events/keyset?" +
                             "&".join(f"slug={s}" for s in slugs) + "&limit=100&locale=en")
    except Exception:
        return {}
    events = resp if isinstance(resp, list) else resp.get("events", [])
    out = {}
    for ev in events:
        for m in ev.get("markets", []) or []:
            slug = m.get("slug", "")
            if "btc-updown" not in slug:
                continue
            prices = m.get("outcomePrices") or ""
            try:
                prices = json.loads(prices) if isinstance(prices, str) else prices
            except Exception:
                continue
            if len(prices) >= 1:
                out[slug] = float(prices[0])
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=300)
    ap.add_argument("--interval", type=float, default=2.0)
    args = ap.parse_args()

    out_dir = Path("backtest_results")
    out_dir.mkdir(exist_ok=True)
    path = out_dir / f"lag_measure_{time.strftime('%Y%m%d_%H%M%S')}.csv"
    t0 = time.time()
    rows = 0
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "ts_unix", "btc_price", "btc_5m_yes", "btc_15m_yes"])
        while time.time() - t0 < args.seconds:
            ts = time.time()
            try:
                price = binance_price("BTCUSDT")
            except Exception:
                price = ""
            ref = updown_ref()
            w.writerow([time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                        round(ts, 3), price,
                        ref.get(f"btc-updown-5m-{(int(ts) // 300) * 300}", ""),
                        ref.get(f"btc-updown-15m-{(int(ts) // 900) * 900}", "")])
            rows += 1
            fh.flush()
            time.sleep(args.interval)
    print(f"saved: {path} rows={rows}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
