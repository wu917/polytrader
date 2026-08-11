"""策略基类。"""
from __future__ import annotations

from abc import ABC, abstractmethod

from polytrader.models import Market, OrderBook, Signal


class Strategy(ABC):
    """所有策略的接口：给定市场与订单簿，产出交易信号。"""

    name: str = "base"

    @abstractmethod
    def scan(self, markets: list[Market],
             books: dict[str, OrderBook] | None = None) -> list[Signal]:
        """扫描一组市场，返回交易信号（可能多笔，如套利对）。"""
