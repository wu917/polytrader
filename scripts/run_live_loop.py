"""实盘循环（与 run_llm_loop 统一扫描/开单语义）：LLM 信号 → FOK 真实下单。

扫描/开单节奏与 run_llm_loop.py 完全一致：
- 一轮 = 一个 5m/15m 窗口（进入时不对齐整点，残余窗口照常扫）
- 窗口内每 --scan-interval 秒（默认 30s）扫描一次，窗口结束前
  --stop-before 秒（默认 40s）停止该窗口的扫描
- 窗口结束后睡到下一个窗口开始
- 去重统一用 seen_slugs.txt（load_seen/save_seen，与 simulate 一致），
  避免与 simulate 的 MySQL pending 单互相干扰

与 run_llm_loop 的差异（仅执行层）：
- 真实下单：FOK（signatureType=3 / ERC-7739），deposit wallet 流程
- 成交写 pending_trades（mode='live'）+ mark_filled(fill_price, fill_tx)
- 结算由 settle_worker 常驻进程处理（启动时自动拉起，退出不影响结算）

⚠️ 真实资金。默认每轮最多 1 笔、$1/笔；资金预检（deposit wallet pUSD 需
覆盖 size + 手续费）。

用法: PYTHONPATH=. .venv/bin/python scripts/run_live_loop.py \
          --rounds 3 [--scan-interval 60] [--stop-before 40]
"""
import argparse
import json
import os
import subprocess
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
    COINS, LLMUpdownStrategy, fetch_windows, load_seen, save_seen)

SETTLE_PID_FILE = ROOT / "settle_worker.pid"


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


def _settle_worker_alive() -> bool:
    pid = None
    if SETTLE_PID_FILE.exists():
        try:
            pid = int(SETTLE_PID_FILE.read_text().strip())
        except ValueError:
            return False
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def ensure_settle_worker():
    """结算常驻进程未运行时自动拉起（start 内部 fork，立即返回）。"""
    if _settle_worker_alive():
        return
    subprocess.run([sys.executable, "scripts/settle_worker.py", "start"],
                   cwd=ROOT, capture_output=True, text=True, timeout=30)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3,
                    help="窗口数（一轮 = 一个 5m/15m 窗口）")
    ap.add_argument("--windows", type=str, default="5m,15m",
                    help="参与的市场窗口（与 run_llm_loop 一致）")
    ap.add_argument("--scan-interval", type=int, default=30,
                    help="窗口内扫描间隔秒（默认 30；0=每窗口仅扫 1 次）")
    ap.add_argument("--stop-before", type=int, default=40,
                    help="窗口结束前 N 秒停止该窗口的扫描（默认 40）")
    ap.add_argument("--size", type=float, default=1.0, help="每笔 USD（默认 $1）")
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--coins", type=str, default=",".join(COINS))
    ap.add_argument("--per-round", type=int, default=1, help="每轮最多开几笔")
    ap.add_argument("--market", action="store_true",
                    help="市价化：BUY 直接 0.99 激进吃单（FOK 全成或撤）")
    ap.add_argument("--seen-file", type=str,
                    default="backtest_results/seen_slugs.txt",
                    help="已交易 slug 持久化文件（与 simulate 共用同一去重文件）")
    ap.add_argument("--log", type=str, default="",
                    help="日志文件路径（输出同时写文件，默认只输出到 stdout）")
    args = ap.parse_args()
    if args.log:
        sys.stdout = _Tee(args.log)  # type: ignore[assignment]

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
    print(f"deposit wallet pUSD: ${bal:.4f} | 每窗口预算: ${need:.2f}")
    if bal < need:
        print(f"!! 资金不足：需要 ${need:.2f}，只有 ${bal:.2f}（先充值）")
        return 1

    # ---- LLM 评估器（与 simulate/llm_updown 同一导入路径）----
    coins = [c for c in args.coins.split(",") if c]
    cm = {c: c for c in coins}
    from polytrader.ai.llm_scorer import LLMScorer
    from polytrader.config import load_config
    from polytrader.data.http_client import HttpClient
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

    # 结算常驻进程：启动时确保在跑（主任务退出后由它继续结算）
    ensure_settle_worker()
    print("settle worker ensured (pending storage: MySQL polytrader.pending_trades)")

    # ---- 主循环：每轮 = 一个窗口（当前窗口直接扫描，不对齐整点）----
    win_secs = 300 if "5m" in args.windows else 900
    results = []
    seen = load_seen(args.seen_file)
    try:
        for i in range(1, args.rounds + 1):
            print(f"\n=== LOOP {i}/{args.rounds} ===")
            now = int(time.time())
            w_start = (now // win_secs) * win_secs     # 当前窗口起点（不做对齐等待）
            w_end = w_start + win_secs
            print(f"  window {w_start} -> {w_end} (enter mid-window at {now})")

            scans = 0
            while True:
                now = int(time.time())
                if now > w_end - args.stop_before:     # 窗口结束前 stop_before 秒停止
                    print(f"  window ends in {w_end - now}s, stop scanning "
                          f"({scans} scans done)")
                    break
                scans += 1
                print(f"  scan {scans} @ {time.strftime('%H:%M:%S')}")

                markets = fetch_windows(http, cm, windows=win_filter)
                # 去重：seen 文件（与 simulate 共用，live 只去重自身成交过的 slug）
                already = {s for s in markets if s in seen}
                markets = {s: m for s, m in markets.items() if s not in seen}
                print(f"windows: {len(markets)} markets (after dedup, already: {len(already)})")
                if not markets:
                    if args.scan_interval <= 0:
                        break
                    time.sleep(args.scan_interval)
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
                    # 预期成交价（吃单侧盘口价）：过滤坏单 [0.25, 0.85]
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
                    if expect_fill is not None and not (0.25 <= expect_fill <= 0.85):
                        print(f"  {m.slug} {side_str} 预期成交价{expect_fill:.3f} "
                              f"超范围[0.25,0.85] 过滤（空壳盘口）")
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
                            "round": i,
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
                        results.append({"round": i, "slug": m.slug, "side": side_str,
                                        "llm_p": s.extra.get("llm_p"),
                                        "ref": s.market_price, "edge": s.edge,
                                        "orderID": body.get("orderID"),
                                        "status": body.get("status"),
                                        "fill_price": fill_price,
                                        "tx": tx_hash})
                        placed += 1
                        # 成交后记入 seen，避免同窗口后续扫描重复开单
                        seen.add(m.slug)
                        save_seen(args.seen_file, seen)
                        print(f"    ✅ 成交 {body.get('orderID','')[:20]}... "
                              f"status={body.get('status')} "
                              f"fill_price=${fill_price} tx={tx_hash[:18] if tx_hash else '?'}...")
                    else:
                        print(f"    ❌ 下单失败: {resp['body'][:200]}")
                if args.scan_interval <= 0:
                    break
                time.sleep(args.scan_interval)

            # 窗口结束：睡到下一个窗口开始（结算由常驻 settle_worker 处理）
            sleep_to = w_end + 2
            d = sleep_to - int(time.time())
            if d > 0 and i < args.rounds:
                print(f"  window over, waiting {d}s for next window")
                time.sleep(d)
    except KeyboardInterrupt:
        print("\n中断")

    print("\n=== 汇总 ===")
    print(json.dumps(results, ensure_ascii=False, indent=1))
    print(f"pending 总数: {db.count_pending()} "
          f"(settle_worker pid={SETTLE_PID_FILE} 将继续结算，退出不影响)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
