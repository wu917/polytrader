"""风控管理器：敞口、日损熔断、回撤熔断、冷却、价格带。"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field

from polytrader.logging_setup import get_logger
from polytrader.models import Signal, Trade

log = get_logger("risk.manager")


@dataclass
class RiskState:
    """运行时风控状态。"""

    realized_pnl_today: float = 0.0
    peak_equity: float = 0.0
    current_equity: float = 0.0
    exposure: dict[str, float] = field(default_factory=dict)   # condition_id -> 敞口 USD
    open_positions: dict[str, float] = field(default_factory=dict)  # token_id -> shares
    last_trade_ts: dict[str, float] = field(default_factory=dict)   # condition_id -> ts
    day: str = ""


class RiskManager:
    """所有风控规则的单一入口。check(signal) 返回 (allowed, reason)。"""

    def __init__(
        self,
        mode: str = "dry-run",
        max_position_usd: float = 500.0,
        max_total_exposure_usd: float = 3000.0,
        max_daily_loss_usd: float = 100.0,
        max_drawdown_pct: float = 0.15,
        max_open_positions: int = 10,
        min_price: float = 0.03,
        max_price: float = 0.97,
        cooldown_seconds: int = 300,
        initial_equity: float = 5000.0,
    ):
        self.mode = mode
        self.max_position_usd = max_position_usd
        self.max_total_exposure_usd = max_total_exposure_usd
        self.max_daily_loss_usd = max_daily_loss_usd
        self.max_drawdown_pct = max_drawdown_pct
        self.max_open_positions = max_open_positions
        self.min_price = min_price
        self.max_price = max_price
        self.cooldown_seconds = cooldown_seconds
        self.initial_equity = initial_equity
        self.state = RiskState(current_equity=initial_equity, peak_equity=initial_equity)

    # ---- 查询 ----
    @property
    def total_exposure(self) -> float:
        return sum(self.state.exposure.values())

    @property
    def drawdown_pct(self) -> float:
        if self.state.peak_equity <= 0:
            return 0.0
        return max(0.0, (self.state.peak_equity - self.state.current_equity) / self.state.peak_equity)

    # ---- 核心检查 ----
    def check(self, signal: Signal, size_usd: float | None = None) -> tuple[bool, str]:
        size = size_usd if size_usd is not None else signal.size_usd
        cid = signal.market.condition_id
        now = time.time()

        if self.mode == "live":
            return False, "live mode requires credentials & signed orders (not yet configured)"

        if size <= 0:
            return False, "size <= 0"

        if not (self.min_price <= signal.market_price <= self.max_price):
            return False, (f"price {signal.market_price:.3f} outside band "
                           f"[{self.min_price:.2f}, {self.max_price:.2f}]")

        if self.state.realized_pnl_today <= -self.max_daily_loss_usd:
            return False, (f"daily loss circuit breaker: "
                           f"{self.state.realized_pnl_today:.2f} <= -{self.max_daily_loss_usd:.2f}")

        if self.drawdown_pct >= self.max_drawdown_pct:
            return False, (f"drawdown circuit breaker: {self.drawdown_pct:.1%} "
                           f">= {self.max_drawdown_pct:.1%}")

        if len(self.state.exposure) >= self.max_open_positions and cid not in self.state.exposure:
            return False, f"max open positions reached ({self.max_open_positions})"

        if self.total_exposure + size > self.max_total_exposure_usd:
            return False, (f"total exposure {self.total_exposure:.2f} + {size:.2f} "
                           f"> {self.max_total_exposure_usd:.2f}")

        cur = self.state.exposure.get(cid, 0.0)
        if cur + size > self.max_position_usd:
            return False, (f"per-market exposure {cur:.2f} + {size:.2f} "
                           f"> {self.max_position_usd:.2f}")

        last = self.state.last_trade_ts.get(cid, 0.0)
        if now - last < self.cooldown_seconds:
            return False, f"cooldown: {now - last:.0f}s < {self.cooldown_seconds}s"

        return True, "ok"

    # ---- 状态更新 ----
    def record_trade(self, trade: Trade) -> None:
        cid = trade.condition_id
        self.state.exposure[cid] = self.state.exposure.get(cid, 0.0) + trade.usd_value
        self.state.open_positions[trade.token_id] = (
            self.state.open_positions.get(trade.token_id, 0.0) + trade.shares
        )
        self.state.last_trade_ts[cid] = time.time()

    def record_pnl(self, realized_usd: float) -> None:
        self.state.realized_pnl_today += realized_usd
        self.state.current_equity += realized_usd
        if self.state.current_equity > self.state.peak_equity:
            self.state.peak_equity = self.state.current_equity
        log.info("PnL update: today=%+.2f equity=%.2f drawdown=%.2f%%",
                 self.state.realized_pnl_today, self.state.current_equity,
                 self.drawdown_pct * 100)

    def mark_to_market(self, prices: dict[str, float]) -> float:
        """按当前价格重估持仓，更新权益与回撤。返回未实现盈亏。"""
        unrealized = 0.0
        for token_id, shares in self.state.open_positions.items():
            price = prices.get(token_id)
            if price is None:
                continue
            unrealized += shares * price
        self.state.current_equity = self.initial_equity + self.state.realized_pnl_today + unrealized
        if self.state.current_equity > self.state.peak_equity:
            self.state.peak_equity = self.state.current_equity
        return unrealized
