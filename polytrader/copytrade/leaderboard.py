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


class OfficialLeaderboardProvider(LeaderboardProvider):
    """官方交易员排行榜 → 聪明钱钱包（默认 MONTH 周期 + PNL 排序）。

    data-api /v1/leaderboard（2026-08 文档确认公开）：
    - time_period=MONTH 即"每月排行榜"维度，pnl 为官方口径的期间盈亏
    - 排行榜不提供交易数/活跃时间 → total_trades=0、recent_activity 空，
      mirror.refresh_targets 对这两种字段放宽（见 mirror 实现）
    """

    def __init__(self, data_api: DataApiClient, time_period: str = "MONTH",
                 order_by: str = "PNL", category: str = "OVERALL",
                 top_n: int = 50):
        self.data_api = data_api
        self.time_period = time_period
        self.order_by = order_by
        self.category = category
        self.top_n = top_n

    def fetch_profiles(self) -> list[WalletProfile]:
        rows = self.data_api.get_leaderboard(
            limit=self.top_n, time_period=self.time_period,
            order_by=self.order_by, category=self.category)
        profiles: list[WalletProfile] = []
        for r in rows:
            addr = str(r.get("proxyWallet", "") or "")
            if not addr:
                continue
            try:
                pnl = float(r.get("pnl", 0) or 0)
                vol = float(r.get("vol", 0) or 0)
            except (TypeError, ValueError):
                continue
            profile = WalletProfile(
                address=addr,
                realized_profit_usd=pnl,
                roi_pct=(pnl / vol * 100.0) if vol > 0 else 0.0,
                source="leaderboard",
                score=pnl,
            )
            profiles.append(profile)
        profiles.sort(key=lambda p: p.score, reverse=True)
        log.info("leaderboard[%s/%s/%s] profiles=%d top1=%s pnl=%.2f",
                 self.time_period, self.order_by, self.category,
                 len(profiles),
                 profiles[0].address[:10] if profiles else "-",
                 profiles[0].score if profiles else 0.0)
        return profiles[: self.top_n]

