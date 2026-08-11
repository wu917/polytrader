"""CLOB API 客户端：实时订单簿（REST）+ 价格订阅（WebSocket）。"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable, Optional

import websocket  # websocket-client

from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import get_logger
from polytrader.models import OrderBook, OrderBookLevel

log = get_logger("data.clob")

# CLOB 订单簿价格格式：REST /book 返回 0-1 概率字符串；WS 返回相同格式
def _to_level(item) -> Optional[OrderBookLevel]:
    """把 CLOB level 解析为 OrderBookLevel。

    支持两种真实格式：
    - 对象: {"price": "0.52", "size": "100"}（REST /book）
    - 数组: ["0.52", "100"]（WS market channel / 旧版 REST）
    """
    try:
        if isinstance(item, dict):
            return OrderBookLevel(price=float(item.get("price")), size=float(item.get("size")))
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            return OrderBookLevel(price=float(item[0]), size=float(item[1]))
    except (TypeError, ValueError):
        return None
    return None


class ClobClient:
    """CLOB REST：订单簿查询。下单能力在 execution 层（需要签名）。"""

    def __init__(self, api_base: str = "https://clob.polymarket.com",
                 ws_url: str = "wss://ws-subscriptions-clob.polymarket.com",
                 http: HttpClient | None = None):
        self.api_base = api_base.rstrip("/")
        self.ws_url = ws_url
        self.http = http or HttpClient()

    # ---- REST ----
    def get_book(self, token_id: str) -> OrderBook | None:
        """获取单个 outcome token 的订单簿。"""
        data = self.http.get_json(f"{self.api_base}/book", params={"token_id": token_id})
        if not isinstance(data, dict):
            return None
        book = OrderBook(token_id=token_id)
        for level in data.get("bids", []) or []:
            parsed = _to_level(level)
            if parsed and parsed.price > 0:
                book.bids.append(parsed)
        for level in data.get("asks", []) or []:
            parsed = _to_level(level)
            if parsed and parsed.price > 0:
                book.asks.append(parsed)
        return book

    def get_midpoint(self, token_id: str) -> float | None:
        data = self.http.get_json(f"{self.api_base}/midpoint", params={"token_id": token_id})
        if isinstance(data, dict):
            mid = data.get("mid")
            try:
                return float(mid) if mid is not None else None
            except (TypeError, ValueError):
                return None
        return None

    # ---- WebSocket 订阅 ----
    def stream_books(self, token_ids: list[str],
                     on_book: Callable[[OrderBook], None],
                     on_error: Callable[[str], None] | None = None,
                     stop_event: threading.Event | None = None) -> None:
        """订阅多个 token 的订单簿更新（阻塞直到 stop_event 置位或断线）。

        WS 协议: 连接 /ws/market 后发送 {"assets_ids": [...], "type": "market"}，
        服务端推送 {"event_type": "book", "asset_id": ..., "bids": [...], "asks": [...]}。
        """
        url = f"{self.ws_url}/ws/market"
        stop = stop_event or threading.Event()

        def _on_message(ws, message: str):
            try:
                payload = json.loads(message)
            except json.JSONDecodeError:
                log.warning("WS invalid message: %.100s", message)
                return
            event = payload.get("event_type")
            if event == "book":
                asset_id = payload.get("asset_id", "")
                book = OrderBook(token_id=asset_id)
                for level in payload.get("bids", []) or []:
                    parsed = _to_level(level)
                    if parsed and parsed.price > 0:
                        book.bids.append(parsed)
                for level in payload.get("asks", []) or []:
                    parsed = _to_level(level)
                    if parsed and parsed.price > 0:
                        book.asks.append(parsed)
                if book.bids or book.asks:
                    on_book(book)
            elif event in ("error", "error_message"):
                log.warning("WS error: %s", payload)
                if on_error:
                    on_error(str(payload))

        def _on_open(ws):
            log.info("WS connected, subscribing %d tokens", len(token_ids))
            ws.send(json.dumps({"assets_ids": token_ids, "type": "market"}))

        def _on_error(ws, error):
            log.error("WS error: %s", error)
            if on_error:
                on_error(str(error))

        def _on_close(ws, code, msg):
            log.info("WS closed: %s %s", code, msg)

        headers = {"Origin": "https://polymarket.com"}
        ws = websocket.WebSocketApp(
            url,
            header=headers,
            on_message=_on_message,
            on_open=_on_open,
            on_error=_on_error,
            on_close=_on_close,
        )
        while not stop.is_set():
            ws.run_forever(ping_interval=20, ping_timeout=10)
            if stop.is_set():
                break
            log.info("WS disconnected, reconnecting in 3s...")
            time.sleep(3)
        ws.close()
