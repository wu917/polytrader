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
# 实测：7 天 OK、30 天 400 "interval too long" → 取 7 天分块
MAX_HISTORY_WINDOW = 7 * 24 * 3600


class DataApiClient:
    def __init__(self, api_base: str = "https://data-api.polymarket.com",
                 clob_api_base: str = "https://clob.polymarket.com",
                 http: HttpClient | None = None):
        self.api_base = api_base.rstrip("/")
        self.clob_api_base = clob_api_base.rstrip("/")
        self.http = http or HttpClient()

    # ---- 历史价格（CLOB /prices-history）----
    def price_history(self, token_id: str, interval: str = "1h",
                      start_ts: int | None = None, end_ts: int | None = None) -> list[dict]:
        """某 outcome token 的历史价格序列，返回 [{"t": 秒, "p": 价格}]。

        market 参数需要 token_id（clobTokenId）而非 condition_id——
        实测 condition_id 恒返回空。
        自动按 7 天窗口分块请求。interval 为 fidelity（秒）：60/300/900/3600/86400 等。
        """
        end = int(end_ts) if end_ts else int(time.time())
        start = int(start_ts) if start_ts else (end - 7 * 24 * 3600)
        fidelity = _interval_to_fidelity(interval)

        history: list[dict] = []
        cursor = start
        chunks = 0
        while cursor < end and chunks < 64:  # 上限防 API 异常导致空转
            chunk_end = min(cursor + MAX_HISTORY_WINDOW, end)
            params = {"market": token_id, "startTs": cursor, "endTs": chunk_end}
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

    def price_now(self, token_id: str) -> float | None:
        """取最近一笔历史价格作为当前价（容错实现）。"""
        rows = self.price_history(token_id, interval="1h")
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

    def trades_to_history(self, trades: list[dict], token_id: str,
                          bucket_s: int = 3600) -> list[dict]:
        """把成交记录聚合成价格序列 [{t: 秒, p: 价格}]。

        CLOB /prices-history 对已解决市场不返回历史，回测需用成交账本
        （/trades 保留完整历史）。按 bucket_s 时间桶取桶内最后成交价。
        """
        rows = []
        for t in trades:
            if str(t.get("asset", "")) != token_id:
                continue
            try:
                rows.append((float(t["timestamp"]), float(t["price"])))
            except (TypeError, ValueError, KeyError):
                continue
        rows.sort()
        out: list[dict] = []
        for t, p in rows:
            if out and t - out[-1]["t"] < bucket_s:
                out[-1] = {"t": t, "p": p}  # 桶内更新为最新价
            else:
                out.append({"t": t, "p": p})
        return out

    def market_trade_history(self, condition_id: str, token_id: str,
                             limit: int = 500) -> list[dict]:
        """市场的成交价格序列（回测数据源）。"""
        trades = self.get_trades(condition_id, limit=limit)
        return self.trades_to_history(trades, token_id)


def _interval_to_fidelity(interval: str) -> int | None:
    """把 '1m'/'1h'/'1d'/'max' 转成 CLOB fidelity 秒数。"""
    mapping = {"1m": 60, "5m": 300, "1h": 3600, "1d": 86400}
    if interval in mapping:
        return mapping[interval]
    if interval == "max":
        return None
    return None
