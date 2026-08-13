"""Broker：三种执行模式。

- dry-run: 无网络，按信号自带价格模拟成交（测试/演示）
- paper: 真实取价（CLOB 订单簿）+ 模拟成交（策略验证）
- live: 真实下单。需要 Polymarket API 凭证；签名下单尚未实现，
  检测到 live 模式且无凭证/未实现签名时明确拒绝，绝不盲目下单。
"""
from __future__ import annotations

import logging
import sys
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
    """真实下单：预检 → EIP-712 签名 → CLOB 下单 → 轮询成交 → 超时取消。

    安全护栏（不可绕过）：
    - 无私钥/凭证 → 拒绝
    - 单笔 > max_order_usd → 拒绝
    - 价格不在 [min_price, max_price] → 拒绝
    - 可用 USDC 不足 → 拒绝
    - confirm=True（默认）时需交互确认，非交互环境自动拒绝
    - 不自动重试下单（防重复成交）

    ⚠️ 未经 mainnet 实盘验证：上线前必须在 Polymarket 测试网全链路验证。
    """

    mode = Mode.LIVE.value

    def __init__(self, clob: ClobClient, private_key: str = "",
                 max_order_usd: float = 10.0, slippage_tolerance: float = 0.02,
                 order_timeout: int = 30, cancel_on_timeout: bool = True,
                 fill_check_interval: int = 2, confirm: bool = True,
                 min_price: float = 0.03, max_price: float = 0.97):
        self.clob = clob
        self.private_key = private_key
        self.max_order_usd = max_order_usd
        self.slippage_tolerance = slippage_tolerance
        self.order_timeout = order_timeout
        self.cancel_on_timeout = cancel_on_timeout
        self.fill_check_interval = fill_check_interval
        self.confirm = confirm
        self.min_price = min_price
        self.max_price = max_price
        from eth_account import Account
        self.maker = Account.from_key(private_key).address if private_key else ""

    def place(self, signal: Signal) -> Trade:
        def _reject(reason: str) -> Trade:
            log.error("[live] REFUSED %s %s $%.2f: %s",
                      signal.type.value, signal.market.slug, signal.size_usd, reason)
            return Trade(signal=signal.type, market_slug=signal.market.slug,
                         condition_id=signal.market.condition_id,
                         token_id=signal.outcome.token_id if signal.outcome else "",
                         side=signal.side, price=signal.market_price, shares=0.0,
                         usd_value=0.0, status="rejected", mode=self.mode, reason=reason)

        # 1. 前置护栏
        if not (self.private_key and self.clob.auth_ready):
            return _reject("live credentials not configured")
        size = signal.size_usd
        if size <= 0 or size > self.max_order_usd:
            return _reject(f"size ${size:.2f} outside (0, ${self.max_order_usd}]")
        price = signal.market_price
        if price <= 0 or not (self.min_price <= price <= self.max_price):
            return _reject(f"price {price} outside [{self.min_price}, {self.max_price}]")
        token_id = signal.outcome.token_id if signal.outcome else ""
        if not token_id:
            return _reject("no token_id")

        # 2. 资金预检（可用 USDC）
        try:
            usdc = self.clob.get_usdc_balance()
        except Exception as e:
            return _reject(f"balance check failed: {e}")
        if usdc < size:
            return _reject(f"USDC balance ${usdc:.2f} < order ${size:.2f}")

        # 3. 构造订单 + EIP-712 签名
        from polytrader.execution import signer
        asset = signer.asset_id(token_id)
        maker_amount = signer.usd_to_maker_amount(size)
        taker_amount = signer.shares_to_taker_amount(size / price)
        order = {
            "maker": self.maker,
            "taker": signer.ZERO_ADDRESS,
            "tokenId": int(asset, 16),
            "makerAmount": maker_amount,
            "takerAmount": taker_amount,
            "id": int(time.time() * 1000) % (2 ** 63),   # salt：毫秒时间戳
            "feeRateBps": 0,
            "nonce": 0,                                   # CLOB 侧管理防重放
            "expiration": int(time.time()) + self.order_timeout + 60,
        }
        signed = signer.build_order(order, self.private_key)

        # 4. 交互确认（安全）
        summary = (f"[live] PLACE {signal.market.slug} {signal.side.value} "
                   f"${size:.2f} @ ~{price:.3f} (shares≈{size / price:.2f}) "
                   f"token={asset[:14]}...")
        log.warning(summary)
        if self.confirm:
            if not sys.stdin or not sys.stdin.isatty():
                return _reject("confirm required but no interactive terminal")
            try:
                ans = input("Type 'yes' to place real order: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return _reject("confirm aborted")
            if ans != "yes":
                return _reject("confirm rejected")

        # 5. 下单
        try:
            resp = self.clob.place_order(signed)
        except Exception as e:
            return _reject(f"place order failed: {e}")
        order_id = str(resp.get("orderID") or resp.get("order_id") or "")
        if not order_id:
            return _reject(f"no order_id in response: {resp}")
        log.info("[live] order placed id=%s", order_id)

        # 6. 轮询成交
        deadline = time.time() + self.order_timeout
        while time.time() < deadline:
            time.sleep(self.fill_check_interval)
            try:
                st = self.clob.get_order(order_id)
            except Exception as e:
                log.warning("[live] order query error: %s", e)
                continue
            status = str(st.get("status") or "").lower()
            if status in ("matched", "filled", "done"):
                fill = st.get("original_size") or st.get("size") or size / price
                try:
                    shares = float(fill)
                except (TypeError, ValueError):
                    shares = round(size / price, 4)
                return Trade(signal=signal.type, market_slug=signal.market.slug,
                             condition_id=signal.market.condition_id,
                             token_id=token_id, side=signal.side,
                             price=price, shares=round(shares, 4),
                             usd_value=round(shares * price, 2),
                             status="filled", mode=self.mode, order_id=order_id,
                             reason=signal.reason)
            if status in ("canceled", "cancelled", "expired"):
                return _reject(f"order {status} before fill")

        # 7. 超时：取消（可选）
        if self.cancel_on_timeout:
            try:
                self.clob.cancel_order(order_id)
                log.warning("[live] order %s timed out, cancel sent", order_id)
            except Exception as e:
                log.error("[live] cancel failed for %s: %s", order_id, e)
        return _reject(f"order timed out (order_id={order_id})")


def make_broker(mode: str, clob: ClobClient | None = None,
                slippage_tolerance: float = 0.02,
                credentials_present: bool = False,
                private_key: str = "",
                max_order_usd: float = 10.0,
                order_timeout: int = 30, cancel_on_timeout: bool = True,
                fill_check_interval: int = 2, confirm: bool = True,
                min_price: float = 0.03, max_price: float = 0.97) -> Broker:
    if mode == Mode.DRY_RUN.value:
        return DryRunBroker()
    if mode == Mode.PAPER.value:
        if clob is None:
            clob = ClobClient()
        return PaperBroker(clob, slippage_tolerance)
    if mode == Mode.LIVE.value:
        if clob is None:
            clob = ClobClient()
        if not clob.auth_ready:
            log.warning("[live] ClobClient lacks auth credentials; "
                        "LiveBroker will reject all orders")
        return LiveBroker(
            clob, private_key=private_key,
            max_order_usd=max_order_usd, slippage_tolerance=slippage_tolerance,
            order_timeout=order_timeout, cancel_on_timeout=cancel_on_timeout,
            fill_check_interval=fill_check_interval, confirm=confirm,
            min_price=min_price, max_price=max_price)
    raise ValueError(f"unknown mode: {mode}")
