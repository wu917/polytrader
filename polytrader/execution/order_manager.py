"""订单管理器：信号 → 风控 → 执行 → 成交记录的编排。

关键职责：
- 套利组原子性：同一 group_id 的信号（如 YES+NO 配对）要么全部执行要么全部放弃，
  避免只成交一半造成风险敞口。
- 统一走 RiskManager 校验与 Kelly 仓位计算。
"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Iterable

from polytrader.execution.broker import Broker
from polytrader.logging_setup import get_logger
from polytrader.models import Signal, Trade
from polytrader.risk.kelly import kelly_size_usd
from polytrader.risk.risk_manager import RiskManager

log = get_logger("execution.orders")


class OrderManager:
    def __init__(self, broker: Broker, risk: RiskManager,
                 bankroll_usd: float = 5000.0, kelly_fraction: float = 0.25):
        self.broker = broker
        self.risk = risk
        self.bankroll_usd = bankroll_usd
        self.kelly_fraction = kelly_fraction
        self.trades: list[Trade] = []

    def execute(self, signals: list[Signal]) -> list[Trade]:
        """执行一组信号：按 group 分组，组内原子执行。返回全部成交记录。"""
        groups = _group_by_group_id(signals)
        executed: list[Trade] = []
        for group in groups:
            executed.extend(self._execute_group(group))
        return executed

    # ---- 内部 ----
    def _execute_group(self, group: list[Signal]) -> list[Trade]:
        # 1. 计算每笔仓位（Kelly）
        sized = []
        for sig in group:
            size = kelly_size_usd(
                sig.probability, sig.market_price,
                self.bankroll_usd, self.kelly_fraction,
                max_position_usd=self.risk.max_position_usd,
            )
            if size <= 0:
                log.info("skip %s: kelly size 0 (p=%.2f price=%.3f)",
                         sig.market.slug, sig.probability, sig.market_price)
                return []  # 组内任一不可行则整组放弃
            sized.append((sig, size))

        # 2. 组内所有信号必须全部通过风控（预检）
        for sig, size in sized:
            allowed, reason = self.risk.check(sig, size)
            if not allowed:
                log.info("group rejected: %s (%s)", sig.market.slug, reason)
                return []
            log.debug("risk ok: %s $%.2f", sig.market.slug, size)

        # 3. 全部通过后依次执行；若组内任一笔被 broker 拒绝
        #    （如 paper 模式滑点超限/缺 ask），回滚已成交的 leg，
        #    避免套利对分裂成裸方向敞口
        trades: list[Trade] = []
        for sig, size in sized:
            sig.size_usd = size
            trade = self.broker.place(sig)
            if trade.status == "filled":
                self.risk.record_trade(trade)
                self.trades.append(trade)
            trades.append(trade)

        if any(t.status != "filled" for t in trades):
            rolled = [t for t in trades if t.status == "filled"]
            log.warning("group partially filled (%d/%d) — rolling back %d filled legs",
                        len(rolled), len(trades), len(rolled))
            for t in rolled:
                self.risk.remove_trade(t)
                if t in self.trades:
                    self.trades.remove(t)
                t.status = "rolled_back"
        return trades

    def snapshot(self) -> dict:
        """运行摘要。"""
        filled = [t for t in self.trades if t.status == "filled"]
        return {
            "mode": self.broker.mode,
            "trades_total": len(filled),
            "exposure_usd": round(self.risk.total_exposure, 2),
            "realized_pnl_today": round(self.risk.state.realized_pnl_today, 2),
            "equity": round(self.risk.state.current_equity, 2),
            "drawdown_pct": round(self.risk.drawdown_pct * 100, 2),
        }


def _group_by_group_id(signals: Iterable[Signal]) -> list[list[Signal]]:
    """按 extra.group_id 分组；无 group_id 的信号各自成组。"""
    groups: dict[str, list[Signal]] = defaultdict(list)
    order: list[str] = []
    for sig in signals:
        gid = sig.extra.get("group_id") if sig.extra else None
        key = gid or f"single:{id(sig)}"
        if key not in groups:
            order.append(key)
        groups[key].append(sig)
    return [groups[k] for k in order]
