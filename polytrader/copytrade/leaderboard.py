"""钱包发现：从市场成交记录聚合出活跃钱包画像（排行榜私有 API 的公开替代）。"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Iterable

from polytrader.copytrade.wallet_analysis import analyze_wallet_trades, score_wallet
from polytrader.data.data_api import DataApiClient
from polytrader.logging_setup import get_logger
from polytrader.models import WalletProfile

log = get_logger("copytrade.leaderboard")


class LeaderboardProvider(ABC):
    """钱包画像数据源（可插拔：聚合器 / 种子数据 / 未来排行榜 API）。"""

    @abstractmethod
    def fetch_profiles(self) -> list[WalletProfile]:
        ...


class TradesAggregatorProvider(LeaderboardProvider):
    """从热门市场的最近成交聚合钱包。

    对每个市场拉取最近 trades，按 proxyWallet 聚合，FIFO 估算盈亏画像。
    """

    def __init__(self, data_api: DataApiClient, market_condition_ids: Iterable[str],
                 trades_per_market: int = 200, top_n_wallets: int = 50):
        self.data_api = data_api
        self.market_condition_ids = list(market_condition_ids)
        self.trades_per_market = trades_per_market
        self.top_n_wallets = top_n_wallets

    def fetch_profiles(self) -> list[WalletProfile]:
        by_wallet: dict[str, list[dict]] = {}
        for cid in self.market_condition_ids:
            try:
                trades = self.data_api.get_trades(cid, limit=self.trades_per_market)
            except Exception as exc:  # noqa: BLE001
                log.warning("trades fetch failed for %s: %s", cid, exc)
                continue
            for t in trades:
                addr = str(t.get("proxyWallet", ""))
                if addr:
                    by_wallet.setdefault(addr, []).append(t)

        profiles = []
        for addr, trades in by_wallet.items():
            profile = analyze_wallet_trades(trades)
            profile.source = "trades_aggregator"
            profile.score = score_wallet(profile)
            profiles.append(profile)

        profiles.sort(key=lambda p: p.score, reverse=True)
        log.info("aggregated %d wallets from %d markets", len(profiles), len(self.market_condition_ids))
        return profiles[: self.top_n_wallets]


class SeedProvider(LeaderboardProvider):
    """种子数据源：dry-run 演示 / 离线测试用。"""

    def __init__(self, seed: list[WalletProfile]):
        self.seed = seed

    def fetch_profiles(self) -> list[WalletProfile]:
        return list(self.seed)
