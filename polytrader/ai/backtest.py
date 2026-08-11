"""可交易回测：时间切分训练/测试 + 模拟交易 + 收益率统计 + 交易单。

设计（尽量诚实）：
- 按市场结算时间排序，前 train_frac 训练模型，后 1-train_frac 模拟交易
  （训练数据全部早于测试数据，无时间泄漏，单次 walk-forward 划分）
- 入场价：结算前 entry_lookback_h 小时的最近历史价格（模拟"提前感知"入场）
- 只做 YES 侧买入：edge = model_p - entry_price >= min_edge 才交易
- 每笔固定 size_usd（默认 $100），组合按等权累计
- 注意：in-sample 之外仍有偏差（未计滑点/手续费/订单簿深度不可用），
  收益率仅供参考，不构成盈利保证
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from polytrader.ai.features import feature_matrix
from polytrader.ai.models import ProbabilityModel
from polytrader.ai.train import extract_label
from polytrader.logging_setup import get_logger
from polytrader.models import Market

log = get_logger("ai.backtest")


@dataclass
class BacktestTrade:
    """一笔模拟交易单。"""

    market_slug: str
    condition_id: str
    side: str            # "YES"
    entry_time: str
    entry_price: float
    model_p: float
    edge: float
    settle_price: float  # 0.0 或 1.0
    size_usd: float
    pnl_usd: float
    pnl_pct: float


@dataclass
class BacktestResult:
    """回测汇总 + 交易单。"""

    n_trained: int
    n_test: int
    n_trades: int
    win_rate: float
    avg_edge: float
    total_return_pct: float
    max_drawdown_pct: float
    total_pnl_usd: float
    equity_curve: list[float] = field(default_factory=list)
    trades: list[BacktestTrade] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "meta": self.meta,
            "summary": {
                "n_trained": self.n_trained,
                "n_test": self.n_test,
                "n_trades": self.n_trades,
                "win_rate": round(self.win_rate, 4),
                "avg_edge": round(self.avg_edge, 4),
                "total_return_pct": round(self.total_return_pct, 4),
                "max_drawdown_pct": round(self.max_drawdown_pct, 4),
                "total_pnl_usd": round(self.total_pnl_usd, 2),
            },
            "equity_curve": [round(x, 2) for x in self.equity_curve],
            "trades": [
                {
                    "market_slug": t.market_slug,
                    "condition_id": t.condition_id,
                    "side": t.side,
                    "entry_time": t.entry_time,
                    "entry_price": t.entry_price,
                    "model_p": round(t.model_p, 4),
                    "edge": round(t.edge, 4),
                    "settle_price": t.settle_price,
                    "size_usd": t.size_usd,
                    "pnl_usd": round(t.pnl_usd, 2),
                    "pnl_pct": round(t.pnl_pct, 4),
                }
                for t in self.trades
            ],
        }


def run_backtest(
    markets: list[Market],
    histories: dict[str, list[dict]],
    model_factory: Any = None,
    min_edge: float = 0.05,
    entry_lookback_h: float = 24.0,
    size_usd: float = 100.0,
    train_frac: float = 0.7,
    min_price: float = 0.01,
    max_price: float = 0.99,
) -> BacktestResult:
    """执行回测。

    model_factory: 返回 ProbabilityModel 的可调用（默认 get_default_model）。
    histories: condition_id -> price-history 行 [{t, p}]（t 为秒）。
    """
    from polytrader.ai.models import get_default_model

    factory = model_factory or get_default_model
    labeled = [m for m in markets if extract_label(m) is not None]
    # 按结算时间排序，保证时间切分合法
    labeled.sort(key=lambda m: _end_ts(m))
    split = max(1, int(len(labeled) * train_frac))
    train_markets = labeled[:split]
    test_markets = labeled[split:]
    log.info("backtest split: train=%d test=%d", len(train_markets), len(test_markets))

    # ---- 训练 ----
    train_hist = {m.condition_id: histories.get(m.condition_id, []) for m in train_markets}
    X_tr, cols = feature_matrix(train_markets, histories=train_hist)
    y_tr = np.asarray([extract_label(m) for m in train_markets], dtype=int)
    model: ProbabilityModel = factory()
    model.fit(X_tr, y_tr)
    # 训练列全集（含类别 one-hot），预测时复用保证列一致
    train_cats = [c[4:] for c in cols if c.startswith("cat_")]

    # ---- 模拟交易 ----
    trades: list[BacktestTrade] = []
    equity = 0.0
    curve = []
    peak = 0.0
    max_dd = 0.0
    for m in test_markets:
        hist = histories.get(m.condition_id, [])
        entry = _entry_point(hist, entry_lookback_h)
        if entry is None:
            continue
        entry_price = entry["p"]
        if not (min_price <= entry_price <= max_price):
            continue
        feats, _ = feature_matrix([m], histories={m.condition_id: hist},
                                  categories=train_cats)
        try:
            p = float(np.clip(model.predict_proba(feats)[0], 0.0, 1.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("predict failed %s: %s", m.slug, exc)
            continue
        edge = p - entry_price
        if edge < min_edge:
            continue
        settle = 1.0 if extract_label(m) == 1 else 0.0
        pnl = size_usd * (settle - entry_price)
        equity += pnl
        peak = max(peak, equity)
        dd = (peak - equity) / max(peak, size_usd)
        max_dd = max(max_dd, dd)
        curve.append(equity)
        trades.append(BacktestTrade(
            market_slug=m.slug, condition_id=m.condition_id, side="YES",
            entry_time=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(entry["t"])),
            entry_price=entry_price, model_p=p, edge=edge,
            settle_price=settle, size_usd=size_usd,
            pnl_usd=pnl, pnl_pct=(settle - entry_price) / entry_price,
        ))

    n = len(trades)
    result = BacktestResult(
        n_trained=len(train_markets), n_test=len(test_markets), n_trades=n,
        win_rate=(sum(1 for t in trades if t.pnl_usd > 0) / n) if n else 0.0,
        avg_edge=(sum(t.edge for t in trades) / n) if n else 0.0,
        total_return_pct=(equity / (n * size_usd) * 100.0) if n else 0.0,
        max_drawdown_pct=max_dd * 100.0,
        total_pnl_usd=equity,
        equity_curve=curve,
        trades=trades,
        meta={
            "min_edge": min_edge,
            "entry_lookback_h": entry_lookback_h,
            "size_usd": size_usd,
            "train_frac": train_frac,
            "split_note": "time-sorted split: train data strictly earlier than test",
            "caveats": "no slippage/fees; order book depth unavailable; "
                       "single walk-forward split, not rolling",
        },
    )
    return result


def _end_ts(m: Market) -> float:
    """市场结算时间戳（秒）。解析失败用 0（排最前）。"""
    from polytrader.ai.features import _parse_ts

    ts = _parse_ts(str(m.end_date or ""))
    return ts if ts is not None else 0.0


def _entry_point(history: list[dict], lookback_h: float) -> dict | None:
    """取结算前 lookback_h 小时最近的 (t, p) 作为入场点；数据不足返回 None。"""
    pts = []
    for row in history:
        t = row.get("t")
        p = row.get("p")
        if isinstance(p, (list, tuple)):
            p = p[-1] if p else None
        try:
            pts.append((float(t), float(p)))
        except (TypeError, ValueError):
            continue
    if not pts:
        return None
    pts.sort()
    target = pts[-1][0] - lookback_h * 3600.0
    idx = np.searchsorted([t for t, _ in pts], target, side="right") - 1
    idx = max(0, idx)
    return {"t": pts[idx][0], "p": pts[idx][1]}
