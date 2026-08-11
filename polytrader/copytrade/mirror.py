"""镜像引擎：从合格钱包的新成交生成镜像信号。

流程：拉取钱包画像 → 过滤合格目标（盈利/交易数门槛）→ 轮询目标钱包新交易
→ 对每笔满足条件的 BUY 生成镜像 Signal（去重 by trade id）。
"""
from __future__ import annotations

import logging
import time

from polytrader.copytrade.leaderboard import LeaderboardProvider
from polytrader.data.data_api import DataApiClient
from polytrader.logging_setup import get_logger
from polytrader.models import Market, OrderBook, Side, Signal, SignalType, WalletProfile

log = get_logger("copytrade.mirror")


class MirrorEngine:
    def __init__(
        self,
        provider: LeaderboardProvider,
        data_api: DataApiClient,
        min_profit_usd: float = 5000.0,
        min_trades: int = 30,
        lookback_days: int = 90,
        max_slippage: float = 0.03,
        mirror_yes_only: bool = True,
        max_size_usd: float = 200.0,
    ):
        self.provider = provider
        self.data_api = data_api
        self.min_profit_usd = min_profit_usd
        self.min_trades = min_trades
        self.lookback_days = lookback_days
        self.max_slippage = max_slippage
        self.mirror_yes_only = mirror_yes_only
        self.max_size_usd = max_size_usd
        self._seen_trade_ids: set[str] = set()  # 已镜像交易去重
        self._target_wallets: list[str] = []

    # ---- 目标选择 ----
    def refresh_targets(self, profiles: list[WalletProfile] | None = None) -> list[str]:
        """更新合格目标钱包列表，返回地址列表。"""
        profiles = profiles if profiles is not None else self.provider.fetch_profiles()
        since = time.time() - self.lookback_days * 86400
        self._target_wallets = [
            p.address for p in profiles
            if p.address
            and p.realized_profit_usd >= self.min_profit_usd
            and p.total_trades >= self.min_trades
            and p.recent_activity
            and float(p.recent_activity[-1].get("timestamp", 0)) >= since
        ]
        log.info("mirror targets: %d/%d wallets qualified",
                 len(self._target_wallets), len(profiles))
        return self._target_wallets

    # ---- 镜像扫描 ----
    def scan(self, markets: list[Market] | None = None,
             books: dict[str, OrderBook] | None = None) -> list[Signal]:
        """轮询目标钱包的新交易，产出镜像信号。

        markets/books 用于把 token_id 映射回市场元数据与当前 ask 价；
        缺省时仅输出最小信号（reason 注明无市场信息）。
        """
        if not self._target_wallets:
            self.refresh_targets()
        if not self._target_wallets:
            return []

        book_by_token = {tok: b for tok, b in (books or {}).items()}
        market_by_token: dict[str, Market] = {}
        for m in markets or []:
            for o in m.outcomes:
                market_by_token[o.token_id] = m

        signals: list[Signal] = []
        for wallet in self._target_wallets:
            try:
                trades = self.data_api.get_user_trades(wallet, limit=50)
            except Exception as exc:  # noqa: BLE001
                log.warning("user trades failed for %s: %s", wallet, exc)
                continue
            for t in trades:
                trade_id = _trade_id(t)
                if not trade_id or trade_id in self._seen_trade_ids:
                    continue
                self._seen_trade_ids.add(trade_id)
                side = str(t.get("side", "")).upper()
                if side != "BUY":
                    continue
                asset = str(t.get("asset", ""))
                if self.mirror_yes_only and not _is_yes_token(asset, markets or []):
                    continue
                signal = self._build_signal(t, market_by_token.get(asset), book_by_token.get(asset))
                if signal is not None:
                    signals.append(signal)
        return signals

    def _build_signal(self, trade: dict, market: Market | None,
                      book: OrderBook | None) -> Signal | None:
        asset = str(trade.get("asset", ""))
        try:
            exec_price = float(trade.get("price", 0))
        except (TypeError, ValueError):
            exec_price = 0.0

        # 当前可成交价：优先 ask（镜像跟随），无 book 用成交价
        ask_price = book.best_ask().price if book and book.best_ask() else None
        if ask_price is not None and exec_price > 0:
            slippage = ask_price / exec_price - 1.0
            if slippage > self.max_slippage:
                log.info("mirror skip %s: slippage %.1f%% > %.1f%%",
                         asset, slippage * 100, self.max_slippage * 100)
                return None
        fill_price = ask_price if ask_price is not None else exec_price

        outcome = None
        if market:
            outcome = next((o for o in market.outcomes if o.token_id == asset), None)
        return Signal(
            type=SignalType.COPYTRADE,
            market=market if market else Market(condition_id="", question=str(trade.get("title", ""))),
            outcome=outcome if outcome else None,  # type: ignore[arg-type]
            side=Side.BUY,
            probability=1.0, fair_price=fill_price,
            edge=max(fill_price - (exec_price or fill_price), 0.0),
            market_price=fill_price,
            size_usd=min(self.max_size_usd, (exec_price or fill_price) * float(trade.get("size", 0))),
            reason=f"mirror {str(trade.get('proxyWallet', ''))[:10]}... @${fill_price:.3f}",
            extra={"mirror_wallet": str(trade.get("proxyWallet", "")),
                   "mirror_trade_id": _trade_id(trade),
                   "exec_price": exec_price},
        )


def _trade_id(t: dict) -> str:
    """成交唯一 id：优先交易 hash/order id，否则按内容生成指纹。"""
    for key in ("id", "transactionHash", "hash", "orderHash"):
        if t.get(key):
            return f"{key}:{t[key]}"
    return f"{t.get('proxyWallet')}:{t.get('asset')}:{t.get('timestamp')}:{t.get('price')}:{t.get('size')}"


def _is_yes_token(asset: str, markets: list[Market]) -> bool:
    """判断 token 是否为市场的 YES（第一个 outcome）。无法判定时保守拒绝。"""
    for m in markets:
        if m.outcomes and m.outcomes[0].token_id == asset:
            return True
    log.warning("mirror: token %s not found in known markets, skipping (unknown token)",
                asset[:16])
    return False
