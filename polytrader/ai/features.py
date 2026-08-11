"""特征工程：市场元数据 + 订单簿 + 历史价格序列 → 模型特征向量。

特征列（训练与推理必须一致）：
- liq_log: log1p(liquidity)
- vol_log: log1p(volume24hr)
- days_to_end: 距到期天数（秒级, 归一化）
- desc_len: 描述长度
- spread: 相对买卖价差 (ask-bid)/mid
- depth_log: log1p(前 3 档深度 USD)
- ret_1h / ret_24h: 价格动量
- vol_1h: 近 1h 价格波动率
- cat_<category>: one-hot 类别（词干简化）
"""
from __future__ import annotations

import logging
import re
import time
from typing import Iterable

import numpy as np

from polytrader.logging_setup import get_logger
from polytrader.models import Market, OrderBook

log = get_logger("ai.features")

FEATURE_COLS = [
    "liq_log", "vol_log", "days_to_end", "desc_len",
    "spread", "depth_log", "ret_1h", "ret_24h", "vol_1h",
]

_CAT_PATTERN = re.compile(r"[^a-z0-9]+")


def _cat_key(category: str) -> str:
    key = _CAT_PATTERN.sub("_", category.lower()).strip("_")
    return key or "other"


def extract_features(
    market: Market,
    book: OrderBook | None = None,
    price_history: list[dict] | None = None,
    categories: list[str] | None = None,
) -> dict[str, float]:
    """单市场特征提取，返回 {特征名: 值}。categories 提供 one-hot 列全集。"""
    f: dict[str, float] = {}
    f["liq_log"] = float(np.log1p(max(market.liquidity, 0.0)))
    f["vol_log"] = float(np.log1p(max(market.volume, 0.0)))

    end_ts = _parse_ts(market.end_date)
    days_to_end = (end_ts - time.time()) / 86400.0 if end_ts else 0.0
    f["days_to_end"] = float(np.clip(days_to_end, -30.0, 730.0))

    f["desc_len"] = float(min(len(market.description or ""), 2000) / 100.0)

    # 订单簿特征
    if book and book.best_bid() and book.best_ask():
        mid = book.mid_price() or 0.5
        f["spread"] = float((book.best_ask().price - book.best_bid().price) / max(mid, 1e-6))
        f["depth_log"] = float(np.log1p(book.depth_usd(3)))
    else:
        f["spread"] = 0.05  # 缺订单簿时的保守默认
        f["depth_log"] = 0.0

    # 历史价格特征
    prices = _extract_prices(price_history or [])
    f["ret_1h"] = _momentum(prices, 3600)
    f["ret_24h"] = _momentum(prices, 86400)
    f["vol_1h"] = _volatility(prices, 3600)

    # 类别 one-hot
    for cat in categories or []:
        f[f"cat_{cat}"] = 1.0 if _cat_key(market.category) == cat else 0.0
    return f


def feature_matrix(
    markets: list[Market],
    books: dict[str, OrderBook] | None = None,
    histories: dict[str, list[dict]] | None = None,
) -> tuple[np.ndarray, list[str]]:
    """批量特征提取 → (X, columns)。books/histories 按 token_id / condition_id 索引。"""
    books = books or {}
    histories = histories or {}
    categories = sorted({_cat_key(m.category) for m in markets})
    cols = FEATURE_COLS + [f"cat_{c}" for c in categories]
    rows = []
    for m in markets:
        book = books.get(m.outcomes[0].token_id) if m.outcomes else None
        hist = histories.get(m.condition_id)
        f = extract_features(m, book, hist, categories)
        rows.append([f.get(c, 0.0) for c in cols])
    return np.asarray(rows, dtype=float), cols


def _extract_prices(history: list[dict]) -> np.ndarray:
    """[(t, p)] 序列 → 有序 (ts, price) 数组。"""
    pts = []
    for row in history:
        t = row.get("t")
        p = row.get("p")
        if isinstance(p, (list, tuple)):
            p = p[-1] if p else None
        try:
            pts.append((float(t), float(p)))
        except (TypeError, ValueError):
            continue
    pts.sort()
    if not pts:
        return np.zeros((0, 2))
    return np.asarray(pts)


def _momentum(pts: np.ndarray, window_s: float) -> float:
    """过去 window_s 的价格动量（最新价/窗口起点价 - 1），无数据为 0。"""
    if len(pts) < 2:
        return 0.0
    t0 = pts[-1, 0] - window_s
    idx = np.searchsorted(pts[:, 0], t0, side="left")
    if idx >= len(pts) - 1:
        return 0.0
    base = pts[idx, 1]
    if base <= 0:
        return 0.0
    return float(np.clip(pts[-1, 1] / base - 1.0, -1.0, 2.0))


def _volatility(pts: np.ndarray, window_s: float) -> float:
    """窗口内价格标准差，无数据为 0。"""
    if len(pts) < 3:
        return 0.0
    t0 = pts[-1, 0] - window_s
    idx = np.searchsorted(pts[:, 0], t0, side="left")
    window = pts[idx:, 1]
    if len(window) < 3:
        return 0.0
    return float(np.std(window))


def _parse_ts(iso_or_num: str) -> float | None:
    if not iso_or_num:
        return None
    s = str(iso_or_num)
    try:
        return float(s)
    except ValueError:
        pass
    # ISO8601 兼容解析（简化：取 13 位毫秒时间戳数字）
    digits = re.sub(r"\D", "", s)
    if len(digits) >= 13:
        try:
            return float(digits[:13]) / 1000.0
        except ValueError:
            return None
    return None
