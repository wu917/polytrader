"""钱包绩效分析：从成交记录估算已实现/未实现盈亏（FIFO 配对）。"""
from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Iterable

from polytrader.logging_setup import get_logger
from polytrader.models import WalletProfile

log = get_logger("copytrade.analysis")


def analyze_wallet_trades(trades: Iterable[dict], current_prices: dict[str, float] | None = None,
                          resolve_prices: dict[str, float] | None = None) -> WalletProfile:
    """按 FIFO 配对买卖，估算钱包绩效。

    trades: data-api /trades 行，含字段：
        asset (token id), side (BUY/SELL), size (股数), price, timestamp, title, slug
    current_prices: token_id -> 当前价（用于未平仓头寸 mark）
    resolve_prices: token_id -> 结算价（已解决市场按结算价 mark，默认按 current_prices）
    """
    profile = WalletProfile(address="")
    realized = 0.0
    wins = 0
    losses = 0
    open_positions: dict[str, deque[tuple[float, float]]] = defaultdict(deque)  # asset -> [(shares, cost)]
    remaining: dict[str, float] = defaultdict(float)  # asset -> 未平仓股数

    sorted_trades = sorted(trades, key=lambda t: float(t.get("timestamp", 0)))
    for t in sorted_trades:
        asset = str(t.get("asset", ""))
        side = str(t.get("side", "")).upper()
        try:
            size = float(t.get("size", 0))
            price = float(t.get("price", 0))
        except (TypeError, ValueError):
            continue
        if size <= 0 or price <= 0:
            continue

        if side == "BUY":
            open_positions[asset].append((size, price))
            remaining[asset] += size
        elif side == "SELL":
            queue = open_positions[asset]
            to_sell = size
            while to_sell > 1e-12 and queue:
                shares, cost = queue[0]
                matched = min(shares, to_sell)
                realized += matched * (price - cost)
                if price >= cost:
                    wins += 1
                else:
                    losses += 1
                to_sell -= matched
                remaining[asset] -= matched
                if shares - matched <= 1e-12:
                    queue.popleft()
                else:
                    queue[0] = (shares - matched, cost)

    # 未平仓头寸 mark 到结算价/当前价
    unrealized = 0.0
    for asset, shares in remaining.items():
        if shares <= 1e-12:
            continue
        mark = None
        if resolve_prices and asset in resolve_prices:
            mark = resolve_prices[asset]
        elif current_prices and asset in current_prices:
            mark = current_prices[asset]
        if mark is None:
            continue
        cost_total = sum(s * c for s, c in open_positions[asset])
        if shares > 0:
            avg_cost = cost_total / sum(s for s, _ in open_positions[asset])
            unrealized += shares * (mark - avg_cost)

    profile.address = str(sorted_trades[0].get("proxyWallet", "")) if sorted_trades else ""
    profile.realized_profit_usd = realized
    profile.unrealized_profit_usd = unrealized
    profile.total_trades = len(sorted_trades)
    closed = wins + losses
    profile.win_rate = wins / closed if closed else 0.0
    profile.avg_trade_profit_usd = realized / closed if closed else 0.0
    profile.recent_activity = sorted_trades[-20:]
    return profile


def score_wallet(profile: WalletProfile) -> float:
    """综合评分：盈利、胜率、交易量的加权，用于目标排序。

    权重：realized 60%，win_rate 25%，交易数 15%（对数归一）。
    """
    profit_score = max(profile.realized_profit_usd, 0.0) / 10000.0  # 1 万刀满分
    win_score = profile.win_rate
    activity_score = min(profile.total_trades / 200.0, 1.0)
    return 0.60 * min(profit_score, 1.0) + 0.25 * win_score + 0.15 * activity_score
