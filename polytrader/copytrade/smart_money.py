"""聪明钱回测：滚动评估钱包绩效 → 跟随盈利钱包的买入。

流程（无泄漏）：
1. 已解决市场按结算时间排序，前 train_frac 作"预热期"（评估钱包用），
   后 1-train_frac 作测试（跟随买入用）
2. 对每个测试市场 m：用全部 timestamp < T_m 的成交（FIFO 计算各钱包
   已实现盈亏 + 交易数）评估钱包 → 选 top_k 合格钱包（盈利 + 交易数门槛）
3. 跟随：m 结算前 follow_window_h 内 top 钱包的 BUY → 按该成交价买入
4. 结算：YES 结算 1.0 / 0.0，固定 size_usd/笔，汇总收益率与交易单

注意：入场价用目标钱包的成交价（镜像语义）；未计滑点/手续费。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from polytrader.ai.backtest import _end_ts
from polytrader.copytrade.wallet_analysis import analyze_wallet_trades
from polytrader.logging_setup import get_logger
from polytrader.models import Market

log = get_logger("copytrade.smartmoney")


@dataclass
class SmartMoneyTrade:
    market_slug: str
    condition_id: str
    side: str
    wallet: str
    entry_time: str
    entry_price: float
    settle_price: float
    size_usd: float
    pnl_usd: float
    pnl_pct: float


@dataclass
class SmartMoneyResult:
    n_preheat: int
    n_test: int
    n_trades: int
    win_rate: float
    total_return_pct: float
    max_drawdown_pct: float
    total_pnl_usd: float
    top_wallets_used: int
    trades: list[SmartMoneyTrade] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "summary": {
                "n_preheat": self.n_preheat,
                "n_test": self.n_test,
                "n_trades": self.n_trades,
                "win_rate": round(self.win_rate, 4),
                "total_return_pct": round(self.total_return_pct, 4),
                "max_drawdown_pct": round(self.max_drawdown_pct, 4),
                "total_pnl_usd": round(self.total_pnl_usd, 2),
                "top_wallets_used": self.top_wallets_used,
            },
            "trades": [
                {"market_slug": t.market_slug, "condition_id": t.condition_id,
                 "side": t.side, "wallet": t.wallet, "entry_time": t.entry_time,
                 "entry_price": t.entry_price, "settle_price": t.settle_price,
                 "size_usd": t.size_usd, "pnl_usd": round(t.pnl_usd, 2),
                 "pnl_pct": round(t.pnl_pct, 4)}
                for t in self.trades
            ],
        }


def run_smart_money_backtest(
    markets: list[Market],
    trades_by_market: dict[str, list[dict]],
    lookback_days: int = 90,
    top_k: int = 5,
    min_trades: int = 3,
    min_profit_usd: float = 50.0,
    train_frac: float = 0.7,
    size_usd: float = 100.0,
    follow_window_h: float = 24.0,
) -> SmartMoneyResult:
    """执行聪明钱回测。trades_by_market: condition_id -> /trades 行。"""
    labeled = [m for m in markets if _is_labeled(m)]
    labeled.sort(key=_end_ts)
    split = max(1, int(len(labeled) * train_frac))
    preheat, test_markets = labeled[:split], labeled[split:]

    # 全局钱包时间线：[(t, wallet, asset, price, side, size, cid)]
    timeline: list[tuple] = []
    for cid, trades in trades_by_market.items():
        for t in trades:
            try:
                ts = float(t["timestamp"])
                price = float(t["price"])
                size = float(t["size"])
            except (TypeError, ValueError, KeyError):
                continue
            timeline.append((ts, str(t.get("proxyWallet", "")), str(t.get("asset", "")),
                             price, str(t.get("side", "")).upper(), size, cid))
    timeline.sort()

    trades_out: list[SmartMoneyTrade] = []
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    wallets_used: set[str] = set()

    for m in test_markets:
        t_m = _end_ts(m)
        if t_m == 0.0:
            continue
        m_trades = trades_by_market.get(m.condition_id, [])
        # 跟随窗口基于市场最后成交时间 t_last（多数市场在结算前很早
        # 就停止交易——结果提前确定；基于 t_m 的窗口内无成交可跟）
        t_last = max((float(t["timestamp"]) for t in m_trades
                      if t.get("timestamp")), default=0.0)
        if t_last <= 0:
            continue
        follow_from = t_last - follow_window_h * 3600.0
        # 1) 用 T_m 之前全部成交评估钱包（严格无泄漏）
        past = [t for t in timeline if t[0] < follow_from]
        by_wallet: dict[str, list[dict]] = {}
        for ts, wallet, asset, price, side, size, cid in past:
            if not wallet:
                continue
            by_wallet.setdefault(wallet, []).append(
                {"asset": asset, "side": side, "size": size, "price": price,
                 "timestamp": ts})
        ranked = []
        for wallet, trades in by_wallet.items():
            profile = analyze_wallet_trades(trades)
            if profile.realized_profit_usd >= min_profit_usd and profile.total_trades >= min_trades:
                ranked.append((profile.realized_profit_usd, wallet))
        ranked.sort(reverse=True)
        top = [w for _, w in ranked[:top_k]]
        if not top:
            continue

        # 2) 跟随：m 在最后成交窗口内 top 钱包的 BUY
        followed = 0
        for t in m_trades:
            try:
                ts = float(t["timestamp"])
                price = float(t["price"])
            except (TypeError, ValueError, KeyError):
                continue
            if ts < follow_from or ts > t_last:
                continue
            wallet = str(t.get("proxyWallet", ""))
            if wallet not in top or str(t.get("side", "")).upper() != "BUY":
                continue
            # 每钱包每市场只跟一次
            if (m.condition_id, wallet) in wallets_used:
                continue
            wallets_used.add((m.condition_id, wallet))
            settle = 1.0 if _label_of(m) == 1 else 0.0
            pnl = size_usd * (settle - price)
            equity += pnl
            peak = max(peak, equity)
            dd = (peak - equity) / max(peak, size_usd)
            max_dd = max(max_dd, dd)
            followed += 1
            trades_out.append(SmartMoneyTrade(
                market_slug=m.slug, condition_id=m.condition_id, side="BUY",
                wallet=wallet,
                entry_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts)),
                entry_price=price, settle_price=settle, size_usd=size_usd,
                pnl_usd=pnl, pnl_pct=(settle - price) / price,
            ))

    n = len(trades_out)
    return SmartMoneyResult(
        n_preheat=len(preheat), n_test=len(test_markets), n_trades=n,
        win_rate=(sum(1 for t in trades_out if t.pnl_usd > 0) / n) if n else 0.0,
        total_return_pct=(equity / (n * size_usd) * 100.0) if n else 0.0,
        max_drawdown_pct=max_dd * 100.0,
        total_pnl_usd=equity,
        top_wallets_used=len({t.wallet for t in trades_out}),
        trades=trades_out,
        meta={"lookback_days": lookback_days, "top_k": top_k,
              "min_trades": min_trades, "min_profit_usd": min_profit_usd,
              "follow_window_h": follow_window_h, "size_usd": size_usd,
              "note": "wallet ranking uses trades strictly before the follow window (no look-ahead); "
                      "entry at target wallet's own fill price; no slippage/fees"},
    )


def _is_labeled(m: Market) -> bool:
    try:
        prices = [float(o.price) for o in m.outcomes]
    except (TypeError, ValueError):
        return False
    return len(prices) == 2 and any(p >= 0.999 for p in prices)


def _label_of(m: Market) -> int:
    """YES 结算 1 / NO 结算 0。"""
    try:
        return 1 if float(m.outcomes[0].price) >= 0.999 else 0
    except (TypeError, ValueError):
        return 0
