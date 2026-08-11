"""套利引擎：二元 YES/NO 互补套利 + 分类市场概率和套利。

原理：
1. 二元套利：同一市场的 YES 与 NO 是互补事件。若 ask_yes + ask_no < 1 - min_edge，
   同时买入两边，无论结果如何都能盈利（期望无风险收益 = 1 - cost）。
2. 分类套利：同一事件下互斥候选市场的 YES 概率和理论上 = 1。若 sum(ask_i) < 1 - min_edge，
   同时买入所有候选，无论谁赢都盈利。互斥性由 Gamma event 的 market 分组保证。
"""
from __future__ import annotations

import logging
from typing import Optional

from polytrader.logging_setup import get_logger
from polytrader.models import Market, OrderBook, Side, Signal, SignalType
from polytrader.strategies.base import Strategy

log = get_logger("strategies.arbitrage")


class ArbitrageStrategy(Strategy):
    name = "arbitrage"

    def __init__(self, min_edge: float = 0.02,
                 max_position_usd: float = 1000.0,
                 group_size_cap: int = 12):
        self.min_edge = min_edge
        self.max_position_usd = max_position_usd
        self.group_size_cap = group_size_cap  # 分类套利最多同时买几个候选

    # ---- 二元套利 ----
    def scan(self, markets: list[Market],
             books: dict[str, OrderBook] | None = None) -> list[Signal]:
        books = books or {}
        signals: list[Signal] = []
        for market in markets:
            if not market.is_binary or market.closed or not market.active:
                continue
            book_yes = books.get(market.outcomes[0].token_id)
            book_no = books.get(market.outcomes[1].token_id)
            if book_yes is None or book_no is None:
                continue
            signals.extend(self._scan_binary_pair(market, book_yes, book_no))
        return signals

    def _scan_binary_pair(self, market: Market, book_yes: OrderBook,
                          book_no: OrderBook) -> list[Signal]:
        ask_yes = book_yes.best_ask()
        ask_no = book_no.best_ask()
        if ask_yes is None or ask_no is None:
            return []

        cost = ask_yes.price + ask_no.price
        edge = 1.0 - cost
        if edge < self.min_edge:
            return []

        group_id = f"arb:{market.condition_id}"
        # 单 leg 胜率 = 互补 outcome 的隐含概率（1 - ask_other），
        # 而非 1.0——保证 Kelly 语义正确（组整体胜率才接近 1）
        p_yes = 1.0 - ask_no.price
        p_no = 1.0 - ask_yes.price
        signals = [
            Signal(
                type=SignalType.ARBITRAGE,
                market=market, outcome=market.outcomes[0],
                side=Side.BUY, probability=p_yes, fair_price=p_yes,
                edge=edge, market_price=ask_yes.price,
                size_usd=self.max_position_usd,  # 风控层再收敛
                reason=f"binary arb: YES@${ask_yes.price:.3f}+NO@${ask_no.price:.3f}=${cost:.3f}, edge={edge:.3f}",
                extra={"group_id": group_id, "pair_price": cost, "leg_p": p_yes},
            ),
            Signal(
                type=SignalType.ARBITRAGE,
                market=market, outcome=market.outcomes[1],
                side=Side.BUY, probability=p_no, fair_price=p_no,
                edge=edge, market_price=ask_no.price,
                size_usd=self.max_position_usd,
                reason=f"binary arb: YES@${ask_yes.price:.3f}+NO@${ask_no.price:.3f}=${cost:.3f}, edge={edge:.3f}",
                extra={"group_id": group_id, "pair_price": cost, "leg_p": p_no},
            ),
        ]
        log.info("binary arb signal: %s edge=%.3f (YES %.3f + NO %.3f)",
                 market.slug, edge, ask_yes.price, ask_no.price)
        return signals

    # ---- 分类套利 ----
    def scan_categorical(self, markets: list[Market],
                         books: dict[str, OrderBook] | None = None) -> list[Signal]:
        """markets 为同一事件下的互斥候选市场，全部买入 YES。"""
        books = books or {}
        asks: list[tuple[Market, float]] = []
        for market in markets:
            if market.closed or not market.active or not market.is_binary:
                continue
            yes = market.outcomes[0]
            book = books.get(yes.token_id)
            ask = book.best_ask() if book else None
            if ask is not None:
                asks.append((market, ask.price))
        if len(asks) < 2:
            return []

        asks = asks[:self.group_size_cap]
        total = sum(p for _, p in asks)
        edge = 1.0 - total
        if edge < self.min_edge:
            return []

        group_id = f"cat:{markets[0].condition_id}"
        signals = []
        for market, ask in asks:
            yes = market.outcomes[0]
            # 单候选隐含概率 = ask/总和（归一化，总和为 1），Kelly 语义正确
            p = ask / total if total > 0 else 1.0 / len(asks)
            signals.append(Signal(
                type=SignalType.ARBITRAGE,
                market=market, outcome=yes,
                side=Side.BUY, probability=p, fair_price=p,
                edge=edge, market_price=ask,
                size_usd=self.max_position_usd,
                reason=f"categorical arb: sum(asks)={total:.3f}, edge={edge:.3f}, {len(asks)} candidates",
                extra={"group_id": group_id, "group_total": total, "group_size": len(asks)},
            ))
        log.info("categorical arb signal: %d candidates sum=%.3f edge=%.3f",
                 len(asks), total, edge)
        return signals
