"""Gamma API 客户端：市场发现与元数据。"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable

from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import get_logger
from polytrader.models import Market, Outcome

log = get_logger("data.gamma")


class GammaClient:
    """封装 https://gamma-api.polymarket.com 的市场查询。

    文档要点：
    - GET /markets 返回市场列表（支持 limit/offset/active/closed 等过滤）
    - GET /markets/{condition_id} 返回单市场
    - 每个 market 的 outcomes 数组含 outcome id、token id 与价格字符串
    """

    def __init__(self, api_base: str = "https://gamma-api.polymarket.com",
                 http: HttpClient | None = None):
        self.api_base = api_base.rstrip("/")
        self.http = http or HttpClient()

    def _market_from_json(self, m: dict[str, Any]) -> Market:
        outcomes = []
        raw_outcomes = _as_list(m.get("outcomes"))
        prices = _as_list(m.get("outcomePrices"))
        token_ids = _as_list(m.get("clobTokenIds"))
        if raw_outcomes and isinstance(raw_outcomes[0], dict):
            # 旧格式：outcomes 为对象数组
            for o in raw_outcomes:
                outcomes.append(Outcome(
                    outcome_id=str(o.get("id", "")),
                    token_id=str(o.get("token_id", "")),
                    price=str(o.get("price", "")),
                    name=str(o.get("name", "")),
                ))
        else:
            # 现行格式：outcomes=名称数组，outcomePrices=价格数组，clobTokenIds=token 数组
            for i, name in enumerate(raw_outcomes):
                outcomes.append(Outcome(
                    outcome_id="",
                    token_id=str(token_ids[i]) if i < len(token_ids) else "",
                    price=str(prices[i]) if i < len(prices) else "",
                    name=str(name),
                ))
        liquidity = _safe_float(m.get("liquidity"))
        volume = _safe_float(m.get("volume24hr")) or _safe_float(m.get("volume"))
        # category 在 Gamma 里位于 event 级（events[0].category），
        # market 自身无 category 字段（此前解析恒为空导致特征失效）
        category = str(m.get("category") or "") or _event_category(m)
        return Market(
            condition_id=str(m.get("conditionId") or m.get("condition_id") or ""),
            question=str(m.get("question", "")),
            slug=str(m.get("slug", "")),
            category=category,
            description=str(m.get("description", ""))[:2000],
            end_date=str(m.get("endDate", "")),
            liquidity=liquidity,
            volume=volume,
            closed=bool(m.get("closed", False)),
            active=bool(m.get("active", True)),
            outcomes=outcomes,
        )

    def get_markets(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool | None = None,
        category: str | None = None,
        liquidity_min: float | None = None,
        end_date_min: str | None = None,
        end_date_max: str | None = None,
    ) -> list[Market]:
        """发现市场列表，返回解析后的 Market 对象。"""
        params: dict[str, Any] = {
            "limit": limit,
            "offset": offset,
            "active": str(active).lower(),
        }
        if closed is not None:
            params["closed"] = str(closed).lower()
        if category:
            params["category"] = category
        if liquidity_min:
            params["liquidity_num_min"] = liquidity_min
        if end_date_min:
            params["end_date_min"] = end_date_min
        if end_date_max:
            params["end_date_max"] = end_date_max
        data = self.http.get_json(f"{self.api_base}/markets", params=params)
        items = data if isinstance(data, list) else data.get("data", [])
        return [self._market_from_json(m) for m in items if isinstance(m, dict)]

    def get_market(self, condition_id: str) -> Market | None:
        # quote 防路径遍历/注入（condition_id 虽来自 Polymarket API，纵深防御）
        from urllib.parse import quote

        safe_id = quote(condition_id, safe="")
        data = self.http.get_json(f"{self.api_base}/markets/{safe_id}")
        if not data:
            return None
        return self._market_from_json(data)

    def get_events(
        self,
        limit: int = 100,
        offset: int = 0,
        active: bool = True,
        closed: bool | None = None,
    ) -> list[dict[str, Any]]:
        """事件级查询（一个事件可能含多个市场），返回原始 JSON。"""
        params: dict[str, Any] = {"limit": limit, "offset": offset,
                                  "active": str(active).lower()}
        if closed is not None:
            params["closed"] = str(closed).lower()
        data = self.http.get_json(f"{self.api_base}/events", params=params)
        return data if isinstance(data, list) else data.get("data", [])

    def iter_markets(self, batch: int = 100, max_markets: int = 1000, **filters: Any) -> Iterable[Market]:
        """分页遍历市场。"""
        offset = 0
        while offset < max_markets:
            batch_markets = self.get_markets(limit=batch, offset=offset, **filters)
            if not batch_markets:
                break
            yield from batch_markets
            offset += batch


def _event_category(m: dict) -> str:
    """从 market JSON 的 events[0].category 提取分类（Gamma 的 category 在 event 级）。"""
    events = m.get("events") or []
    if events and isinstance(events[0], dict):
        return str(events[0].get("category") or "")
    return ""


def _safe_float(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _as_list(v: Any) -> list:
    """Gamma 的数组字段可能为 JSON 编码字符串，统一转成 list。"""
    if v is None:
        return []
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []
