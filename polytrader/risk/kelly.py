"""Kelly 仓位计算（预测市场适用）。

买 YES 于价格 price：赔率 b = (1 - price) / price。
Kelly 分数 f* = p - (1 - p) / b = p - (1 - p) * price / (1 - price)。
使用分数 Kelly（默认 0.25）控制波动。
"""
from __future__ import annotations

import math


def kelly_fraction(probability: float, price: float) -> float:
    """完整 Kelly 下注比例 f* ∈ [0, 1)。price 为成交价（0-1）。"""
    p = _clamp01(probability)
    price = _clamp01(price)
    if price <= 0.0 or price >= 1.0:
        return 0.0
    b = (1.0 - price) / price
    f = (b * p - (1.0 - p)) / b
    return max(0.0, min(f, 0.9))


def kelly_size_usd(probability: float, price: float, bankroll_usd: float,
                   fraction: float = 0.25, max_position_usd: float = 500.0) -> float:
    """分数 Kelly 下单金额（美元），受单笔上限约束。"""
    f = kelly_fraction(probability, price)
    size = bankroll_usd * f * max(0.0, min(fraction, 1.0))
    return round(min(size, max_position_usd), 2)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, float(x)))
