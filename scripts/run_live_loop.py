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

# ⚠️ 硬性风控（用户强制，不可覆盖）：单笔最大 $1，绝不放大仓位。
MAX_ORDER_USD = 1.0

from polytrader import db  # noqa: E402
from polytrader.execution import order_v2, signer  # noqa: E402
from polytrader.execution.signer import l2_headers_new  # noqa: E402
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


# 代理：优先 HTTPS_PROXY 环境变量（缺省回退本机 socks5 7890，保持原行为）
PROXY = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
         or "socks5h://127.0.0.1:7890")


def _req(method: str, url: str, **kw):
    # 中国大陆环境必须走本地代理访问 clob.polymarket.com；
    # headers 从 kw 中取出，重试时保持同一份（避免重试丢失导致 401）
    proxies = {"http": PROXY, "https": PROXY}
    headers = kw.pop("headers", {})
    for i in range(4):
        try:
            r = requests.request(method, url, timeout=20, proxies=proxies,
                                 headers={"User-Agent": "Mozilla/5.0",
                                          **headers}, **kw)
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


def verify_token(token_id: str) -> bool:
    """下单前只读校验 token 在 CLOB 有效（GET /tick-size，公开端点）。

    规避偶发 invalid token id：5m 市场新创建时 gamma 已返回 clobTokenIds
    但 CLOB 侧可能尚未生效，直接下单会 400 invalid token id。
    """
    try:
        r = _req("GET", "https://clob.polymarket.com/tick-size",
                 params={"token_id": token_id})
        return r.status_code == 200
    except Exception:
        return False


def place_maker(creds: dict, eoa: str, pk: str, deposit: str,
                token_id: str, side: int, size_usd: float,
                price: float) -> dict:
    """maker GTC 限价单（post_only=True）：挂单不成交则留在订单簿。

    与 place_fok 的区别：orderType=GTC + postOnly=true，价格是限价
    （期望成交价），不会以更差价格吃单；空壳盘口下先挂单等对手盘。
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
             "metadata": order_v2.ZERO_BYTES32, "builder": order_v2.ZERO_BYTES32}
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


def get_order_auth(creds: dict, eoa: str, order_id: str) -> dict:
    """带 L2 认证查询订单（V2 端点需 POLY_* HMAC 头，不能裸调）。"""
    path = f"/data/order/{order_id}"
    h2 = l2_headers_new(eoa, creds["apiKey"], creds["passphrase"],
                        creds["secret"], "GET", path)
    r = _req("GET", f"https://clob.polymarket.com{path}", headers=h2)
    r.raise_for_status()
    return r.json()


def wait_order_fill(creds: dict, eoa: str, order_id: str,
                    timeout: int = 100, poll: int = 10) -> dict | None:
    """轮询订单成交状态，timeout 秒内 matched 则返回订单详情，否则 None。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(poll)
        try:
            od = get_order_auth(creds, eoa, order_id)
        except Exception as e:
            print(f"    (get_order 失败 {str(e)[:50]}，继续轮询)")
            continue
        if not isinstance(od, dict):
            continue
        st = od.get("status")
        if st == "matched":
            return od
        if st in ("cancelled", "canceled", "expired"):
            return None
    return None


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
    ap.add_argument("--fok-slip", type=float, default=0.01,
                    help="FOK 吃单滑点容忍（maxPrice=盘口价+slip，封顶 0.85，默认 0.01）")
    ap.add_argument("--seen-file", type=str,
                    default="backtest_results/seen_slugs.txt",
                    help="已交易 slug 持久化文件（与 simulate 共用同一去重文件）")
    ap.add_argument("--log", type=str, default="",
                    help="日志文件路径（输出同时写文件，默认只输出到 stdout）")
    args = ap.parse_args()
    # ⚠️ 强制单笔 $1 硬上限：传更大的 --size 直接拒绝启动（用户风控，不可覆盖）
    if args.size <= 0 or args.size > MAX_ORDER_USD:
        print(f"!! --size {args.size} 非法：须 (0, {MAX_ORDER_USD}] 区间 "
              f"（用户风控，强制 $1/单，不可多买）。已拒绝启动。")
        return 1
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
            # ⚠️ 每轮复查余额（成交会扣款，防止资金不足时无效下单）
            try:
                bal_now = chain.call_balance(chain.PUSD, deposit) / 1e6
            except Exception as e:
                print(f"  (余额查询失败 {str(e)[:40]}，按预算假设继续)")
                bal_now = bal
            if bal_now < args.size + 0.05:
                print(f"  !! 资金不足（${bal_now:.2f} < ${args.size + 0.05}），"
                      f"本轮跳过，等下一窗口")
                d = (w_end + 2) - int(time.time())
                if d > 0 and i < args.rounds:
                    time.sleep(d)
                continue

            scans = 0
            while True:
                now = int(time.time())
                if now > w_end - args.stop_before:     # 窗口结束前 stop_before 秒停止
                    print(f"  window ends in {w_end - now}s, stop scanning "
                          f"({scans} scans done)")
                    break
                scans += 1
                print(f"  scan {scans} @ {time.strftime('%H:%M:%S')}")
                try:  # ⚠️ 单次扫描异常不崩溃：跳过本次扫描继续
                    markets = fetch_windows(http, cm, windows=win_filter)
                except Exception as e:
                    print(f"  (fetch_windows 失败 {str(e)[:60]}，本次扫描跳过)")
                    if args.scan_interval <= 0:
                        break
                    time.sleep(args.scan_interval)
                    continue
                # 去重：seen 文件（与 simulate 共用，live 只去重自身成交过的 slug）
                already = {s for s in markets if s in seen}
                markets = {s: m for s, m in markets.items() if s not in seen}
                print(f"windows: {len(markets)} markets (after dedup, already: {len(already)})")
                if not markets:
                    if args.scan_interval <= 0:
                        break
                    time.sleep(args.scan_interval)
                    continue
                try:  # ⚠️ 评估/下单段异常不崩溃：记日志后继续下一扫描
                    signals = strat.scan(list(markets.values()))
                except Exception as e:
                    print(f"  (scan 失败 {str(e)[:60]}，本次扫描跳过)")
                    if args.scan_interval <= 0:
                        break
                    time.sleep(args.scan_interval)
                    continue
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
                    # FOK maxPrice：盘口吃单侧价（或 ref）+ 滑点容忍，封顶 0.85
                    # （0.85 与坏单过滤上限一致，避免空壳盘口 0.99 极端价成交）
                    base = expect_fill if expect_fill is not None \
                        else float(s.market_price)
                    price = round(min(0.85, max(0.01, base + args.fok_slip)), 2)
                    # ⚠️ 下单前校验 token 在 CLOB 有效（防偶发 invalid token id）
                    if not verify_token(token_id):
                        print(f"  {m.slug} {side_str} token 在 CLOB 无效/未生效，跳过")
                        continue
                    # 注：FOK/市价单 BUY 按 USD 金额（$1）驱动，豁免
                    # min_order_size 份额约束（实测 $1@0.54=1.85 份成交，
                    # 而 GTC 3.45 份被拒）——故不做份额预检
                    # ⚠️ 防重复：无论下单成败，本窗口该 slug 立即记入 seen，
                    # 避免 30s 后同窗口重复开单
                    seen.add(m.slug)
                    save_seen(args.seen_file, seen)
                    print(f"  {m.slug} {side_str} llm_p={s.extra.get('llm_p',0):.3f} "
                          f"ref={s.market_price:.3f} edge={s.edge:+.3f} "
                          f"FOK@{price} ${args.size} (slip {args.fok_slip:+.2f})")
                    resp = place_fok(creds, eoa, pk, deposit, token_id,
                                     order_v2.BUY, args.size, price)
                    print(f"    POST /order: {resp['status_code']}")
                    print(f"    {resp['body'][:220]}")
                    try:
                        body = json.loads(resp["body"])
                    except Exception:
                        body = {}
                    if resp["status_code"] == 200 and body.get("success"):
                        order_id = body.get("orderID")
                        # FOK 立即成交（status=matched）或拒绝；无需轮询/撤单
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
                            "entry_price": round(fill_price if fill_price
                                                 else price, 4),  # FOK 实际成交价
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
