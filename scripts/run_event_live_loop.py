"""通用事件盘实盘循环：LLM 评估 → maker GTC 限价单 → 轮询成交 → 入库结算。

复用：
- run_live_loop.derive_creds / _req（凭证派生与请求重试）
- order_v2（EIP-712 签名 + GTC/post_only maker 单）
- scan_event_markets（全量发现 + 事件盘过滤）
- simulate_equity_updown.build_db_rec（入库记录）

与 run_equity_live_loop 的区别：
- 市场范围：全量事件盘（选举/宏观/地缘/商业），非股票/商品 updown
- 执行方式：maker GTC 限价单（post_only，挂中间价等成交），非 FOK 吃单
  —— Polymarket 盘口空壳，taker 不可行，maker 挂单才有成交可能
- 挂单后轮询订单状态（最长 --wait 秒），未成交自动撤单

⚠️ 真实资金。默认每轮最多 1 笔、$1/笔；资金预检。

用法: PYTHONPATH=. .venv/bin/python scripts/run_event_live_loop.py \
          [--size 1] [--min-edge 0.05] [--min-rr 1.5] [--wait 600]
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
from polytrader.execution import order_v2  # noqa: E402
from polytrader.execution.signer import (  # noqa: E402
    ZERO_BYTES32, l2_headers_new, sign_clob_auth)
from scripts.run_live_loop import _Tee, _req, derive_creds  # noqa: E402
from scripts.simulate_equity_updown import build_db_rec  # noqa: E402
from scripts.scan_event_markets import (  # noqa: E402
    PROXY, fetch_all_active, is_event_market, to_market)
from polytrader.strategies.event_market import EventMarketStrategy  # noqa: E402


def place_maker(creds: dict, eoa: str, pk: str, deposit: str,
                token_id: str, side: int, size_usd: float,
                price: float) -> dict:
    """maker GTC 限价单（post_only=True）：挂单不成交则留在订单簿。

    与 run_live_loop.place_fok 的区别：orderType=GTC + postOnly=true，
    价格是限价（期望成交价），不会以更差价格吃单。
    """
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
             "metadata": ZERO_BYTES32, "builder": ZERO_BYTES32}
    payload = order_v2.order_to_json_v2(order, owner=creds["apiKey"],
                                        order_type="GTC", post_only=True)
    serialized = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    h2 = l2_headers_new(eoa, creds["apiKey"], creds["passphrase"],
                        creds["secret"], "POST", "/order", body=serialized)
    h2["Content-Type"] = "application/json"
    r = _req("POST", "https://clob.polymarket.com/order", data=serialized,
             headers=h2)
    return {"status_code": r.status_code, "body": r.text}


def cancel_order(creds: dict, eoa: str, order_id: str) -> dict:
    """撤销挂单（DELETE /order?orderID=...）。

    官方签名规则：query param 不在签名内，message = ts + DELETE + /order
    （无 body），因此 l2_headers_new 不传 body，order_id 走 query。
    """
    h2 = l2_headers_new(eoa, creds["apiKey"], creds["passphrase"],
                        creds["secret"], "DELETE", "/order")
    r = _req("DELETE", "https://clob.polymarket.com/order",
             params={"orderID": order_id}, headers=h2)
    return {"status_code": r.status_code, "body": r.text}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=float, default=1.0, help="每笔 USD（默认 $1）")
    ap.add_argument("--min-edge", type=float, default=0.05)
    ap.add_argument("--min-rr", type=float, default=1.5)
    ap.add_argument("--min-vol", type=float, default=5000.0)
    ap.add_argument("--max-markets", type=int, default=50)
    ap.add_argument("--per-round", type=int, default=1)
    ap.add_argument("--wait", type=int, default=600,
                    help="挂单后轮询秒数（默认 600s），超时未成交撤单")
    ap.add_argument("--poll", type=int, default=15, help="订单状态轮询间隔")
    ap.add_argument("--log", type=str, default="")
    args = ap.parse_args()
    if args.log:
        sys.stdout = _Tee(args.log)  # type: ignore[assignment]
        # logging 输出也进同一日志文件（统一集合到 log）
        from polytrader.logging_setup import setup_logging
        setup_logging(level="INFO", log_file=args.log)

    # ---- 凭证与资金预检 ----
    pk = os.environ["POLYMARKET_PRIVATE_KEY"]
    deposit = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "").strip()
    if not deposit:
        print("!! .env 缺 POLYMARKET_DEPOSIT_WALLET")
        return 1
    eoa = Account.from_key(pk).address
    creds = derive_creds(eoa, pk)
    print(f"EOA={eoa} deposit={deposit} 认证 OK")

    from polytrader.execution import chain
    bal = chain.call_balance(chain.PUSD, deposit) / 1e6
    need = args.size * args.per_round + 0.05 * args.per_round
    print(f"deposit wallet pUSD: ${bal:.4f} | 本轮预算: ${need:.2f}")
    if bal < need:
        print(f"!! 资金不足：需要 ${need:.2f}，只有 ${bal:.2f}（先充值）")
        return 1

    # ---- 扫描 + LLM 评估 ----
    from polytrader.data.http_client import HttpClient
    http = HttpClient(proxy=PROXY, timeout=20)
    cfg = load_config()
    scorer = LLMScorer(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
                       model=cfg.llm_model)
    if not scorer.enabled:
        print("!! LLM not configured (LLM_API_KEY missing)")
        return 1
    strat = EventMarketStrategy(scorer, min_edge=args.min_edge,
                                min_rr=args.min_rr,
                                max_markets=args.max_markets)

    print(f"拉取全量活跃市场（vol ≥ ${args.min_vol:,.0f}）...")
    mkts = fetch_all_active(http, min_vol=args.min_vol)
    event = [m for m in mkts
             if is_event_market(m.get("slug", ""), m.get("question", ""))]
    print(f"事件盘: {len(event)}")

    # ref 过滤 + 去重（已开单）
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
    already = {r["slug"] for r in db.fetch_pending()}
    tradable = [m for m in tradable if m.get("slug") not in already]
    print(f"可交易（ref∈[0.05,0.95]，未开单）: {len(tradable)}")
    if not tradable:
        print("无候选盘，退出")
        return 0

    # 盘口快照（LLMBookStrategy 需要 books）
    from polytrader.data.clob_client import ClobClient
    clob = ClobClient(http=http)
    markets = [to_market(m) for m in tradable[:args.max_markets]]
    books = {}
    for m in markets:
        try:
            b = clob.get_book(m.outcomes[0].token_id)
            if b:
                books[m.outcomes[0].token_id] = b
        except Exception:
            pass
    print(f"evaluating {len(markets)} markets (LLM)...")
    signals = strat.scan(markets, books)
    print(f"signals: {len(signals)}")

    # ---- maker 挂单 ----
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
        buy_price = float(s.extra.get("buy_price", s.market_price))
        # maker 限价：在 ref 价 ±1¢ 挂单（不追单，等成交）
        price = round(buy_price, 3)
        print(f"  {m.slug[:48]:48s} {side} llm_p={s.extra.get('llm_p', 0):.3f} "
              f"buy@{price} rr={s.extra.get('rr')} ev={s.extra.get('ev'):+} "
              f"${args.size} [maker GTC]")
        resp = place_maker(creds, eoa, pk, deposit, token_id,
                           order_v2.BUY, args.size, price)
        print(f"    POST /order: {resp['status_code']} {resp['body'][:160]}")
        try:
            body = json.loads(resp["body"])
        except Exception:
            body = {}
        if resp["status_code"] != 200 or not body.get("success"):
            print(f"    ❌ 挂单失败: {resp['body'][:160]}")
            continue
        order_id = body.get("orderID")
        print(f"    ✅ 已挂单 {order_id[:24]}... status={body.get('status')}")

        # 轮询成交状态
        filled = None
        deadline = time.time() + args.wait
        while time.time() < deadline:
            time.sleep(args.poll)
            try:
                od = clob.get_order(order_id)
            except Exception:
                continue
            st = od.get("status") if isinstance(od, dict) else None
            if st == "matched":
                filled = od
                break
            if st in ("cancelled", "canceled", "expired"):
                print(f"    订单状态 {st}，不再等待")
                break
        if filled is None:
            print(f"    ⏱️ {args.wait}s 未成交，撤单...")
            cx = cancel_order(creds, eoa, order_id)
            print(f"    cancel: {cx['status_code']} {cx['body'][:100]}")
            continue

        # 成交 → 入库
        fill_price = None
        try:
            making = float(filled.get("makingAmount") or 0)
            taking = float(filled.get("takingAmount") or 1)
            if taking > 0:
                fill_price = round(making / taking, 4)
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        tx_hash = None
        txs = filled.get("transactionsHashes") or []
        if txs:
            tx_hash = txs[0]
        rec = build_db_rec({
            "trade_id": str(uuid.uuid4())[:8],
            "slug": m.slug,
            "coin": m.slug.split("-")[0],
            "window": "event",
            "side": side,
            "entry_price": buy_price,
            "size_usd": args.size,
            "llm_p": round(float(s.extra.get("llm_p", 0)), 4),
            "ref": round(float(s.market_price), 4),
            "edge": round(float(s.edge), 4),
            "llm_reason": s.extra.get("llm_reason"),
            "model": s.extra.get("model"),
            "order_id": order_id,
            "order_status": "matched",
            "fill_price": fill_price,
            "fill_tx": tx_hash,
            "results_file": str(ROOT / "backtest_results"
                                / f"event_results_live_{time.strftime('%Y%m%d_%H%M%S')}.jsonl"),
        }, mode="live")
        try:
            db.insert_pending([rec])
            if fill_price is not None:
                db.mark_filled(rec["trade_id"], fill_price, tx_hash)
        except Exception as e:
            print(f"    ⚠️ db insert FAILED（已成交，写兜底）: {e}")
            fallback = ROOT / "backtest_results" / "event_live_fallback.jsonl"
            fallback.parent.mkdir(parents=True, exist_ok=True)
            with open(fallback, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        results.append({"slug": m.slug, "side": side, "orderID": order_id,
                        "fill_price": fill_price, "tx": tx_hash})
        placed += 1
        print(f"    ✅ 成交 fill=${fill_price} tx={tx_hash[:18] if tx_hash else '?'}...")

    print("\n=== 汇总 ===")
    print(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"pending 总数: {db.count_pending()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
