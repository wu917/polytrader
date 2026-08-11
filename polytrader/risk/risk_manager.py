"""风控管理器：敞口、日损熔断、回撤熔断、冷却、价格带。

敞口语义：按持仓市值（shares × 当前价）计算，而非买入成本。
外部通过 update_prices() 注入最新价格；价格未知的持仓回退到成本估算。
"""
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
    open_positions: dict[str, float] = field(default_factory=dict)      # token_id -> shares
    token_condition: dict[str, str] = field(default_factory=dict)       # token_id -> condition_id
    cost_basis: dict[str, float] = field(default_factory=dict)          # token_id -> 累计成本 USD
    last_trade_ts: dict[str, float] = field(default_factory=dict)       # condition_id -> ts


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
        self.prices: dict[str, float] = {}   # token_id -> 最新价（外部注入）

    # ---- 查询 ----
    def update_prices(self, prices: dict[str, float]) -> None:
        """注入最新市场价格，用于市值重估。"""
        self.prices.update({k: float(v) for k, v in prices.items() if v is not None})

    def exposure_of(self, condition_id: str) -> float:
        """单 condition 敞口（市值，价格未知时按成本估算）。"""
        total = 0.0
        for token_id, shares in self.state.open_positions.items():
            if self.state.token_condition.get(token_id) != condition_id:
                continue
            price = self.prices.get(token_id)
            if price is None:
                cost = self.state.cost_basis.get(token_id, 0.0)
                total += cost if shares > 0 else 0.0
            else:
                total += shares * price
        return total

    @property
    def total_exposure(self) -> float:
        return sum(self.exposure_of(cid) for cid in self._condition_ids())

    def _condition_ids(self) -> set[str]:
        return {cid for cid in self.state.token_condition.values() if cid}

    @property
    def open_position_count(self) -> int:
        return len(self._condition_ids())

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

        if self.open_position_count >= self.max_open_positions and cid not in self._condition_ids():
            return False, f"max open positions reached ({self.max_open_positions})"

        if self.total_exposure + size > self.max_total_exposure_usd:
            return False, (f"total exposure {self.total_exposure:.2f} + {size:.2f} "
                           f"> {self.max_total_exposure_usd:.2f}")

        cur = self.exposure_of(cid)
        if cur + size > self.max_position_usd:
            return False, (f"per-market exposure {cur:.2f} + {size:.2f} "
                           f"> {self.max_position_usd:.2f}")

        last = self.state.last_trade_ts.get(cid, 0.0)
        if now - last < self.cooldown_seconds:
            return False, f"cooldown: {now - last:.0f}s < {self.cooldown_seconds}s"

        return True, "ok"

    # ---- 状态更新 ----
    def record_trade(self, trade: Trade) -> None:
        """记录一笔成交（增加持仓与成本）。"""
        if trade.shares <= 0:
            return
        token_id = trade.token_id
        if not token_id:
            return
        self.state.open_positions[token_id] = self.state.open_positions.get(token_id, 0.0) + trade.shares
        self.state.token_condition[token_id] = trade.condition_id
        self.state.cost_basis[token_id] = self.state.cost_basis.get(token_id, 0.0) + trade.usd_value
        self.state.last_trade_ts[trade.condition_id] = time.time()

    def remove_trade(self, trade: Trade) -> None:
        """回滚一笔成交（套利组部分成交时撤销已成交 leg）。"""
        token_id = trade.token_id
        if not token_id or token_id not in self.state.open_positions:
            return
        shares = self.state.open_positions[token_id] - trade.shares
        if shares <= 1e-9:
            self.state.open_positions.pop(token_id, None)
            self.state.token_condition.pop(token_id, None)
            self.state.cost_basis.pop(token_id, None)
        else:
            self.state.open_positions[token_id] = shares
            self.state.cost_basis[token_id] = max(0.0, self.state.cost_basis.get(token_id, 0.0) - trade.usd_value)

    def record_pnl(self, realized_usd: float) -> None:
        """记录已实现盈亏（equity 由 mark_to_market 统一推导）。"""
        self.state.realized_pnl_today += realized_usd
        log.info("PnL update: today=%+.2f", self.state.realized_pnl_today)

    def mark_to_market(self, prices: dict[str, float] | None = None) -> float:
        """按当前价格重估持仓，更新权益与回撤。返回未实现盈亏（净盈亏，非市值）。

        equity = initial + realized_pnl + (持仓市值 - 持仓成本)
        """
        self.update_prices(prices or {})
        market_value = 0.0
        for token_id, shares in self.state.open_positions.items():
            price = self.prices.get(token_id)
            if price is None:
                continue
            market_value += shares * price
        open_cost = sum(self.state.cost_basis.values())
        unrealized = market_value - open_cost
        self.state.current_equity = self.initial_equity + self.state.realized_pnl_today + unrealized
        if self.state.current_equity > self.state.peak_equity:
            self.state.peak_equity = self.state.current_equity
        return unrealized
