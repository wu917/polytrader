"""WS 实时订单簿订阅冒烟测试。

连接真实 wss://ws-subscriptions-clob.polymarket.com，订阅一个活跃 token，
收集 N 条 book 更新后退出。

用法: .venv/bin/python scripts/smoke_ws.py [--token <clobTokenId>] [--count 3]
"""
import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.data.clob_client import ClobClient
from polytrader.data.gamma_client import GammaClient
from polytrader.data.http_client import HttpClient

# 备用活跃 token（Xi Jinping out before 2027? 的 YES）
FALLBACK_TOKEN = "32338220190071351435772801779725302244575775216413325951443816017994629993401"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="http://127.0.0.1:7897")
    ap.add_argument("--token", default="")
    ap.add_argument("--count", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    token = args.token
    if not token:
        http = HttpClient(proxy=args.proxy)
        gamma = GammaClient(http=http)
        markets = gamma.get_markets(limit=10, active=True)
        binary = [m for m in markets if m.is_binary]
        token = binary[0].outcomes[0].token_id if binary else FALLBACK_TOKEN
        print(f"[ws] using token from first binary market: {token[:16]}...")

    received: list = []
    stop = threading.Event()

    def on_book(book):
        received.append(book)
        bb = book.best_bid()
        ba = book.best_ask()
        print(f"[ws] book {book.token_id[:12]}... bids={len(book.bids)} asks={len(book.asks)} "
              f"bid={bb.price if bb else '-'} ask={ba.price if ba else '-'}")
        if len(received) >= args.count:
            stop.set()

    client = ClobClient(http=HttpClient(proxy=args.proxy), proxy=args.proxy)
    thread = threading.Thread(
        target=client.stream_books, args=([token], on_book), kwargs={"stop_event": stop},
        daemon=True,
    )
    print(f"[ws] subscribing {token[:16]}... (timeout {args.timeout}s)")
    thread.start()
    thread.join(timeout=args.timeout)

    if len(received) >= args.count:
        print(f"[ws] SUCCESS: received {len(received)} book updates")
        return 0
    print(f"[ws] FAIL: only {len(received)} book updates in {args.timeout}s")
    return 1


if __name__ == "__main__":
    sys.exit(main())
