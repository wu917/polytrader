"""data-api / CLOB 历史数据客户端：历史价格与成交记录。

实测端点（2025）：
- CLOB /prices-history?market=<condition_id>&startTs=&endTs=&fidelity=  → {"history":[{t,p},...]}
- data-api /trades?market=<condition_id>&limit= → 每笔成交含 proxyWallet（跟单数据源）
"""
from __future__ import annotations

import logging
import time
from typing import Any

from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import get_logger

log = get_logger("data.dataapi")

# CLOB /prices-history 单次请求允许的最大时间窗口（秒）
MAX_HISTORY_WINDOW = 7 * 24 * 3600  # 7 天


class DataApiClient:
    def __init__(self, api_base: str = "https://data-api.polymarket.com",
                 clob_api_base: str = "https://clob.polymarket.com",
                 http: HttpClient | None = None):
        self.api_base = api_base.rstrip("/")
        self.clob_api_base = clob_api_base.rstrip("/")
        self.http = http or HttpClient()

    # ---- 历史价格（CLOB /prices-history）----
    def price_history(self, condition_id: str, interval: str = "1h",
                      start_ts: int | None = None, end_ts: int | None = None) -> list[dict]:
        """历史价格序列，返回 [{"t": 秒, "p": 价格}]。

        自动按 7 天窗口分块请求。interval 为 fidelity（秒）：60/300/900/3600/86400 等。
        """
        end = end_ts or int(time.time())
        start = start_ts or (end - 7 * 24 * 3600)
        fidelity = _interval_to_fidelity(interval)

        history: list[dict] = []
        cursor = start
        chunks = 0
        while cursor < end and chunks < 64:  # 上限防 API 异常导致空转
            chunk_end = min(cursor + MAX_HISTORY_WINDOW, end)
            params = {"market": condition_id, "startTs": cursor, "endTs": chunk_end}
            if fidelity:
                params["fidelity"] = fidelity
            data = self.http.get_json(f"{self.clob_api_base}/prices-history", params=params)
            rows = data.get("history", []) if isinstance(data, dict) else []
            history.extend(rows)
            cursor = chunk_end
            chunks += 1
            if len(rows) < 2:
                break  # 空窗口提前结束
        return history

    def price_now(self, condition_id: str) -> float | None:
        """取最近一笔历史价格作为当前价（容错实现）。"""
        rows = self.price_history(condition_id, interval="1h")
        if not rows:
            return None
        last = rows[-1]
        p = last.get("p")
        if isinstance(p, (list, tuple)):
            p = p[-1] if p else None  # OHLC 取 close
        try:
            return float(p)
        except (TypeError, ValueError):
            return None

    # ---- 成交记录（data-api /trades，跟单引擎数据源）----
    def get_trades(self, condition_id: str, limit: int = 100) -> list[dict]:
        """市场最近成交。字段：proxyWallet/side/asset/size/price/timestamp/title/slug。"""
        data = self.http.get_json(f"{self.api_base}/trades",
                                  params={"market": condition_id, "limit": min(limit, 500)})
        return data if isinstance(data, list) else []

    def get_user_trades(self, address: str, limit: int = 100) -> list[dict]:
        """某钱包的成交记录（跟单分析用）。"""
        data = self.http.get_json(f"{self.api_base}/trades",
                                  params={"user": address, "limit": min(limit, 500)})
        return data if isinstance(data, list) else []


def _interval_to_fidelity(interval: str) -> int | None:
    """把 '1m'/'1h'/'1d'/'max' 转成 CLOB fidelity 秒数。"""
    mapping = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}
    if interval in mapping:
        return mapping[interval]
    if interval == "max":
        return None
    return None
