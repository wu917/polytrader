"""收敛/确定性折价策略回测。

原理：预测市场临近结算时价格应收敛到 0 或 1。若市场最后成交价
已高度确定（p >= threshold 或 p <= 1-threshold），买入该确定侧，
收益 = (1 - p)（确定性折价）。回测验证"最后成交价高度确定的市场
是否真的正确结算"——即确定性折价是否真实存在（而非市场尾段错价）。

数据：已解决市场 + /trades 成交（每市场最后成交价作为入场价）。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from polytrader.logging_setup import get_logger
from polytrader.models import Market

log = get_logger("ai.convergence")


@dataclass
class ConvergenceTrade:
    market_slug: str
    condition_id: str
    side: str            # YES 或 NO（买入侧）
    last_trade_time: str
    entry_price: float   # 买入价（确定侧）
    settle_price: float  # 0.0 / 1.0
    size_usd: float
    pnl_usd: float
    pnl_pct: float


@dataclass
class ConvergenceResult:
    n_markets: int
    n_trades: int
    win_rate: float
    avg_entry_price: float
    total_return_pct: float
    total_pnl_usd: float
    trades: list[ConvergenceTrade] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "summary": {
                "n_markets": self.n_markets,
                "n_trades": self.n_trades,
                "win_rate": round(self.win_rate, 4),
                "avg_entry_price": round(self.avg_entry_price, 4),
                "total_return_pct": round(self.total_return_pct, 4),
                "total_pnl_usd": round(self.total_pnl_usd, 2),
            },
            "trades": [
                {"market_slug": t.market_slug, "condition_id": t.condition_id,
                 "side": t.side, "last_trade_time": t.last_trade_time,
                 "entry_price": t.entry_price, "settle_price": t.settle_price,
                 "size_usd": t.size_usd, "pnl_usd": round(t.pnl_usd, 2),
                 "pnl_pct": round(t.pnl_pct, 4)}
                for t in self.trades
            ],
        }


def run_convergence_backtest(
    markets: list[Market],
    trades_by_market: dict[str, list[dict]],
    threshold: float = 0.90,
    size_usd: float = 100.0,
) -> ConvergenceResult:
    """执行收敛策略回测。

    trades_by_market: condition_id -> /trades 行（含 asset/side/price/timestamp）。
    入场：市场最后成交价（YES token 最后一笔）>= threshold → 买 YES；
    <= 1-threshold → 买 NO。
    """
    trades_out: list[ConvergenceTrade] = []
    total_pnl = 0.0

    for m in markets:
        label = _label_of(m)
        if label is None:
            continue
        m_trades = trades_by_market.get(m.condition_id, [])
        if not m_trades:
            continue
        yes_token = m.outcomes[0].token_id if m.outcomes else ""
        # 最后成交价（YES token）
        last_yes: tuple[float, float] | None = None  # (ts, price)
        for t in m_trades:
            if str(t.get("asset", "")) != yes_token:
                continue
            try:
                ts = float(t["timestamp"])
                price = float(t["price"])
            except (TypeError, ValueError, KeyError):
                continue
            if last_yes is None or ts > last_yes[0]:
                last_yes = (ts, price)
        if last_yes is None:
            continue
        p_last = last_yes[1]
        if p_last >= threshold:
            side, entry = "YES", p_last
            settle = 1.0 if label == 1 else 0.0
        elif p_last <= 1.0 - threshold:
            side, entry = "NO", 1.0 - p_last   # 买 NO 的成本
            settle = 1.0 if label == 0 else 0.0
        else:
            continue  # 最后成交价不够确定，不交易

        pnl = size_usd * (settle - entry)
        total_pnl += pnl
        trades_out.append(ConvergenceTrade(
            market_slug=m.slug, condition_id=m.condition_id, side=side,
            last_trade_time=time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                          time.gmtime(last_yes[0])),
            entry_price=entry, settle_price=settle, size_usd=size_usd,
            pnl_usd=pnl, pnl_pct=(settle - entry) / entry,
        ))

    n = len(trades_out)
    return ConvergenceResult(
        n_markets=len(markets), n_trades=n,
        win_rate=(sum(1 for t in trades_out if t.pnl_usd > 0) / n) if n else 0.0,
        avg_entry_price=(sum(t.entry_price for t in trades_out) / n) if n else 0.0,
        total_return_pct=(total_pnl / (n * size_usd) * 100.0) if n else 0.0,
        total_pnl_usd=total_pnl,
        trades=trades_out,
        meta={"threshold": threshold, "size_usd": size_usd,
              "note": "entry at market's last YES-token trade price; "
                      "tests whether high-certainty tail prices resolve correctly"},
    )


def _label_of(m: Market) -> int | None:
    """YES 结算 1 / NO 结算 0；未解决返回 None。"""
    try:
        prices = [float(o.price) for o in m.outcomes]
    except (TypeError, ValueError):
        return None
    if len(prices) != 2:
        return None
    if prices[0] >= 0.999:
        return 1
    if prices[1] >= 0.999:
        return 0
    return None
