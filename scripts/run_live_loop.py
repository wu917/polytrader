"""实盘 3 轮循环：LLM 信号 → FOK 真实下单（deposit wallet，CLOB V2）→ 入库结算。

每 5m 窗口一轮：扫描 5 币种 → LLM 评估 → 第一个 |edge|>=min_edge 的信号
FOK $size 真实下单（signatureType=3 / ERC-7739）→ 写入 pending_trades
（结算由 settle_worker 常驻进程自动处理）。

⚠️ 真实资金。默认每轮最多 1 笔、$1/笔；资金预检（deposit wallet pUSD 需覆盖
size + 手续费）。

用法: PYTHONPATH=. .venv/bin/python scripts/run_live_loop.py --rounds 3
"""
import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import requests
from dotenv import load_dotenv
from eth_account import Account

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

from polytrader import db  # noqa: E402
from polytrader.execution import order_v2, signer  # noqa: E402
from scripts.simulate_llm_updown import (  # noqa: E402
    COINS, LLMUpdownStrategy, fetch_windows)


class _Tee:
    """同时写 stdout 和日志文件（--log 时使用）。"""

    def __init__(self, path: str):
        self.fh = open(path, "a", encoding="utf-8")

    def write(self, s: str):
        sys.__stdout__.write(s)
        self.fh.write(s)
        self.fh.flush()

    def flush(self):
        sys.__stdout__.flush()
        self.fh.flush()


def _req(method: str, url: str, **kw):
    for i in range(4):
        try:
            r = requests.request(method, url, timeout=20,
                                 headers={"User-Agent": "Mozilla/5.0",
                                          **kw.pop("headers", {})}, **kw)
            return r
        except Exception as e:  # noqa: BLE001 代理抖动重试
            print(f"  重试{i + 1}: {str(e)[:50]}")
            time.sleep(3)
    raise RuntimeError("请求失败")


def derive_creds(eoa: str, pk: str) -> dict:
    ts = int(time.time())
    sig = signer.sign_clob_auth(eoa, pk, timestamp=ts, nonce=0)
    r = _req("GET", "https://clob.polymarket.com/auth/derive-api-key",
             headers=signer.derive_api_key_headers(eoa, sig, ts))
    r.raise_for_status()
    return r.json()


def place_fok(creds: dict, eoa: str, pk: str, deposit: str,
              token_id: str, side: int, size_usd: float, price: float) -> dict:
    """FOK 真实下单，返回响应（成功含 orderID/status/transactionsHashes）。"""
    maker_amt, taker_amt = order_v2.calc_amounts(side, size_usd, price)
    ts_ms = str(time.time_ns() // 1_000_000)
    td = order_v2.build_order_typed_data(
        maker=deposit, signer=deposit, token_id=token_id,
        maker_amount=maker_amt, taker_amount=taker_amt,
        side=side, signature_type=order_v2.POLY_1271,
        timestamp_ms=ts_ms, contract=order_v2.CTF_EXCHANGE_V2)
    sig1271 = order_v2.sign_order_poly1271(td, pk, order_v2.CTF_EXCHANGE_V2, 137)
    order = {**td["message"], "signature": sig1271,
             "salt": str(td["message"]["salt"]), "timestamp": ts_ms,
             "metadata": order_v2.ZERO_BYTES32, "builder": order_v2.ZERO_BYTES32}
    payload = order_v2.order_to_json_v2(order, owner=creds["apiKey"],
                                        order_type="FOK")
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    h2 = signer.l2_headers_new(eoa, creds["apiKey"], creds["passphrase"],
                               creds["secret"], "POST", "/order",
                               body=serialized)
    h2["Content-Type"] = "application/json"
    r = _req("POST", "https://clob.polymarket.com/order", data=serialized,
             headers=h2)
    return {"status_code": r.status_code, "body": r.text}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--windows", type=str, default="5m")
    ap.add_argument("--size", type=float, default=1.0, help="每笔 USD（默认 $1）")
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--coins", type=str, default=",".join(COINS))
    ap.add_argument("--per-round", type=int, default=1, help="每轮最多开几笔")
    ap.add_argument("--market", action="store_true",
                    help="市价化：BUY 直接 0.99 激进吃单（FOK 全成或撤）")
    ap.add_argument("--log", type=str, default="",
                    help="日志文件路径（输出同时写文件，默认只输出到 stdout）")
    args = ap.parse_args()
    if args.log:
        sys.stdout = _Tee(args.log)  # type: ignore[assignment]

    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "").strip()
    if not deposit:
        print("!! .env 缺 POLYMARKET_DEPOSIT_WALLET")
        return 1
    eoa = Account.from_key(pk).address
    creds = derive_creds(eoa, pk)
    print(f"EOA={eoa} deposit={deposit} 认证 OK")

    # 资金预检（deposit wallet pUSD ≥ size + fee）
    from polytrader.execution import chain
    bal = chain.call_balance(chain.PUSD, deposit) / 1e6
    need = args.size * args.per_round + 0.05 * args.per_round
    print(f"deposit wallet pUSD: ${bal:.4f} | 本轮预算: ${need:.2f}")
    if bal < need:
        print(f"!! 资金不足：需要 ${need:.2f}，只有 ${bal:.2f}（先充值）")
        return 1

    coins = [c for c in args.coins.split(",") if c]
    cm = {c: c for c in coins}
    from polytrader.config import load_config
    from polytrader.data.http_client import HttpClient
    from polytrader.strategies.llm_book import LLMScorer
    cfg = load_config()
    scorer = LLMScorer(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
                       model=cfg.llm_model)
    if not scorer.enabled:
        print("!! LLM not configured (LLM_API_KEY missing)")
        return 1
    strat = LLMUpdownStrategy(scorer, min_edge=args.min_edge, max_markets=20,
                              coin_map=cm)
    http = HttpClient(timeout=15)
    from polytrader.data.clob_client import ClobClient
    clob = ClobClient(http=http)
    win_filter = tuple(args.windows.split(","))
    results = []
    try:
        for rnd in range(1, args.rounds + 1):
            print(f"\n=== LOOP {rnd}/{args.rounds} ===")
            markets = fetch_windows(http, cm, windows=win_filter)
            # 去重：过滤已开单盘口
            already = {r["slug"] for r in db.fetch_pending()}
            markets = {s: m for s, m in markets.items() if s not in already}
            print(f"windows: {len(markets)} markets (after dedup, already: {len(already)})")
            if not markets:
                print("  无市场，等下一窗口")
                time.sleep(60)
                continue
            # 只对存活窗口评估
            signals = strat.scan(list(markets.values()))
            print(f"signals: {len(signals)}")
            placed = 0
            for s in signals:
                if placed >= args.per_round:
                    break
                side_str = s.extra.get("side")
                if side_str not in ("YES", "NO"):
                    continue
                m = s.market
                idx = 0 if side_str == "YES" else 1
                token_id = m.outcomes[idx].token_id
                # 预期成交价（吃单侧盘口价）：过滤坏单 [0.20, 0.85]
                expect_fill = None
                try:
                    b = clob.get_book(token_id)
                    if b:
                        if side_str == "YES":
                            expect_fill = b.best_ask().price if b.best_ask() else None
                        else:
                            expect_fill = (1.0 - b.best_bid().price
                                           if b.best_bid() else None)
                except Exception:
                    expect_fill = None
                if expect_fill is not None and not (0.20 <= expect_fill <= 0.85):
                    print(f"  {m.slug} {side_str} 预期成交价{expect_fill:.3f} "
                          f"超范围[0.20,0.85] 过滤（空壳盘口）")
                    continue
                if args.market:
                    # 市价化：直接 0.99 吃单（空壳盘口只有 0.99 有卖单）
                    price = 0.99
                else:
                    price = round(min(0.99, float(s.market_price) + 0.01), 3)
                print(f"  {m.slug} {side_str} llm_p={s.extra.get('llm_p',0):.3f} "
                      f"ref={s.market_price:.3f} edge={s.edge:+.3f} "
                      f"BUY@{price} ${args.size} {'[市价]' if args.market else ''}")
                resp = place_fok(creds, eoa, pk, deposit, token_id,
                                 order_v2.BUY, args.size, price)
                print(f"    POST /order: {resp['status_code']}")
                print(f"    {resp['body'][:220]}")
                try:
                    body = json.loads(resp["body"])
                except Exception:
                    body = {}
                if resp["status_code"] == 200 and body.get("success"):
                    # 实际成交价：BUY 时 making(USD) / taking(shares)
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
                    rec = {
                        "trade_id": str(uuid.uuid4())[:8],
                        "slug": m.slug,
                        "coin": m.slug.split("-")[0],
                        "window": "5m" if "-5m-" in m.slug else "15m",
                        "side": side_str,
                        "entry_price": round(float(s.market_price), 4),
                        "size_usd": args.size,
                        "round": rnd,
                        "mode": "live",
                        "order_id": body.get("orderID"),
                        "order_status": body.get("status"),
                        "llm_p": round(float(s.extra.get("llm_p", 0)), 4),
                        "ref_price": round(float(s.market_price), 4),
                        "edge": round(float(s.edge), 4),
                        "llm_reason": s.extra.get("llm_reason"),
                        "llm_model": s.extra.get("model"),
                        "results_file": str(ROOT / "backtest_results"
                                            / f"llm_results_live_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"),
                    }
                    db.insert_pending([rec])
                    if fill_price is not None:
                        db.mark_filled(rec["trade_id"], fill_price, tx_hash)
                    results.append({"round": rnd, "slug": m.slug, "side": side_str,
                                    "llm_p": s.extra.get("llm_p"),
                                    "ref": s.market_price, "edge": s.edge,
                                    "orderID": body.get("orderID"),
                                    "status": body.get("status"),
                                    "fill_price": fill_price,
                                    "tx": tx_hash})
                    placed += 1
                    print(f"    ✅ 成交 {body.get('orderID','')[:20]}... "
                          f"status={body.get('status')} "
                          f"fill_price=${fill_price} tx={tx_hash[:18] if tx_hash else '?'}...")
                else:
                    print(f"    ❌ 下单失败: {resp['body'][:200]}")
            if rnd < args.rounds:
                print("  等下一窗口...")
                time.sleep(120)
    except KeyboardInterrupt:
        print("\n中断")
    print("\n=== 汇总 ===")
    print(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"pending 总数: {db.count_pending()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
