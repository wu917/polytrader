"""Broker：三种执行模式。

- dry-run: 无网络，按信号自带价格模拟成交（测试/演示）
- paper: 真实取价（CLOB 订单簿）+ 模拟成交（策略验证）
- live: 真实下单。需要 Polymarket API 凭证；签名下单尚未实现，
  检测到 live 模式且无凭证/未实现签名时明确拒绝，绝不盲目下单。
"""
from __future__ import annotations

import logging
import time
import uuid
from abc import ABC, abstractmethod

from polytrader.data.clob_client import ClobClient
from polytrader.logging_setup import get_logger
from polytrader.models import Mode, Signal, Trade

log = get_logger("execution.broker")


class Broker(ABC):
    """把信号转为成交。"""

    mode: str = "dry-run"

    @abstractmethod
    def place(self, signal: Signal) -> Trade:
        """按信号执行一笔买入，返回成交记录。"""

    def place_all(self, signals: list[Signal]) -> list[Trade]:
        """批量执行（套利组同时执行由调用方保证顺序）。"""
        return [self.place(s) for s in signals]


class DryRunBroker(Broker):
    """纯模拟：按信号 market_price 全额成交。"""

    mode = Mode.DRY_RUN.value

    def place(self, signal: Signal) -> Trade:
        price = signal.market_price
        shares = round(signal.size_usd / price, 4) if price > 0 else 0.0
        trade = Trade(
            signal=signal.type, market_slug=signal.market.slug,
            condition_id=signal.market.condition_id,
            token_id=signal.outcome.token_id if signal.outcome else "",
            side=signal.side, price=price, shares=shares,
            usd_value=round(shares * price, 2),
            status="filled", mode=self.mode,
            order_id=f"dry-{uuid.uuid4().hex[:12]}",
            reason=signal.reason,
        )
        log.info("[dry-run] %s %s $%.2f @%.3f (%s)",
                 trade.side.value, trade.market_slug, trade.usd_value, price, signal.reason)
        return trade


class PaperBroker(Broker):
    """真实取价 + 模拟成交：从 CLOB 拉最新订单簿，按 ask 价成交。"""

    mode = Mode.PAPER.value

    def __init__(self, clob: ClobClient, slippage_tolerance: float = 0.02):
        self.clob = clob
        self.slippage_tolerance = slippage_tolerance

    def place(self, signal: Signal) -> Trade:
        token_id = signal.outcome.token_id if signal.outcome else ""
        book = self.clob.get_book(token_id) if token_id else None
        ask = book.best_ask() if book else None
        if ask is None:
            log.warning("[paper] no ask for %s, reject", signal.market.slug)
            return Trade(signal=signal.type, market_slug=signal.market.slug,
                         condition_id=signal.market.condition_id, token_id=token_id,
                         side=signal.side, price=0.0, shares=0.0, usd_value=0.0,
                         status="rejected", mode=self.mode, reason="no ask available")

        price = ask.price
        if price > signal.market_price * (1.0 + self.slippage_tolerance) and signal.market_price > 0:
            log.warning("[paper] slippage too high: ask %.3f vs signal %.3f, reject",
                        price, signal.market_price)
            return Trade(signal=signal.type, market_slug=signal.market.slug,
                         condition_id=signal.market.condition_id, token_id=token_id,
                         side=signal.side, price=price, shares=0.0, usd_value=0.0,
                         status="rejected", mode=self.mode,
                         reason=f"slippage {price / signal.market_price - 1:.1%}")

        shares = round(signal.size_usd / price, 4)
        trade = Trade(
            signal=signal.type, market_slug=signal.market.slug,
            condition_id=signal.market.condition_id, token_id=token_id,
            side=signal.side, price=price, shares=shares,
            usd_value=round(shares * price, 2),
            status="filled", mode=self.mode,
            order_id=f"paper-{uuid.uuid4().hex[:12]}",
            reason=signal.reason,
        )
        log.info("[paper] %s %s $%.2f @%.3f (ask from CLOB)",
                 trade.side.value, trade.market_slug, trade.usd_value, price)
        return trade


class LiveBroker(Broker):
    """真实下单（安全边界：当前拒绝执行并给出明确指引）。

    CLOB 签名下单需要 eth 私钥派生 API 凭证 + EIP-712 订单签名，
    属于资金操作，本项目在实现签名前不允许 live 执行。
    """

    mode = Mode.LIVE.value

    def __init__(self, credentials_present: bool = False):
        self.credentials_present = credentials_present

    def place(self, signal: Signal) -> Trade:
        log.error("[live] REFUSED: signed order execution not implemented. "
                  "Add POLYMARKET_PRIVATE_KEY/API_KEY/SECRET/PASSPHRASE to .env "
                  "and implement EIP-712 signing before enabling live mode. "
                  "Signal: %s %s $%.2f", signal.type.value, signal.market.slug, signal.size_usd)
        return Trade(signal=signal.type, market_slug=signal.market.slug,
                     condition_id=signal.market.condition_id,
                     token_id=signal.outcome.token_id if signal.outcome else "",
                     side=signal.side, price=signal.market_price, shares=0.0,
                     usd_value=0.0, status="rejected", mode=self.mode,
                     reason="live signing not implemented (safety guard)")


def make_broker(mode: str, clob: ClobClient | None = None,
                slippage_tolerance: float = 0.02,
                credentials_present: bool = False) -> Broker:
    if mode == Mode.DRY_RUN.value:
        return DryRunBroker()
    if mode == Mode.PAPER.value:
        if clob is None:
            clob = ClobClient()
        return PaperBroker(clob, slippage_tolerance)
    if mode == Mode.LIVE.value:
        return LiveBroker(credentials_present)
    raise ValueError(f"unknown mode: {mode}")
