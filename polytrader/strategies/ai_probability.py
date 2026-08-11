"""AI 概率策略：模型概率 vs 市场价格 → 期望边际信号。

流程：市场特征 → 概率模型（LightGBM/HistGB）→ 可选 LLM 评分融合
→ edge = P(model) - ask 价 ≥ min_edge 且流动性达标 → BUY 信号。
"""
from __future__ import annotations

import logging
from typing import Any

import numpy as np

from polytrader.ai.features import extract_features
from polytrader.ai.llm_scorer import LLMScorer, blend_probabilities
from polytrader.logging_setup import get_logger
from polytrader.models import Market, OrderBook, Side, Signal, SignalType
from polytrader.strategies.base import Strategy

log = get_logger("strategies.ai")


class AIProbabilityStrategy(Strategy):
    name = "ai_probability"

    def __init__(
        self,
        model: Any,                      # ProbabilityModel 实例
        columns: list[str] | None = None,
        min_edge: float = 0.05,
        min_liquidity_usd: float = 500.0,
        llm_scorer: LLMScorer | None = None,
        llm_weight: float = 0.3,
        min_price: float = 0.03,
        max_price: float = 0.97,
    ):
        self.model = model
        self.columns = columns
        self.min_edge = min_edge
        self.min_liquidity_usd = min_liquidity_usd
        self.llm = llm_scorer
        self.llm_weight = llm_weight
        self.min_price = min_price
        self.max_price = max_price

    def scan(self, markets: list[Market],
             books: dict[str, OrderBook] | None = None) -> list[Signal]:
        books = books or {}
        signals: list[Signal] = []
        for market in markets:
            if market.closed or not market.active or not market.is_binary:
                continue
            if market.liquidity < self.min_liquidity_usd:
                continue
            yes = market.outcomes[0]
            book = books.get(yes.token_id)
            ask = book.best_ask() if book else None
            if ask is None:
                continue
            if not (self.min_price <= ask.price <= self.max_price):
                continue

            prob = self._predict(market, book)
            if prob is None:
                continue
            edge = prob - ask.price
            if edge < self.min_edge:
                continue

            signals.append(Signal(
                type=SignalType.AI_PROBABILITY,
                market=market, outcome=yes,
                side=Side.BUY, probability=prob,
                fair_price=prob, edge=edge, market_price=ask.price,
                reason=f"ai: p={prob:.3f} ask={ask.price:.3f} edge={edge:.3f}",
                extra={"model_p": prob},
            ))
        return signals

    def _predict(self, market: Market, book: OrderBook) -> float | None:
        """单市场预测：特征向量 → 模型 → LLM 融合。"""
        try:
            feats = extract_features(market, book)
        except Exception as exc:  # noqa: BLE001
            log.warning("feature extraction failed for %s: %s", market.slug, exc)
            return None

        if self.columns:
            row = np.asarray([[feats.get(c, 0.0) for c in self.columns]], dtype=float)
        else:
            # 无显式列顺序时按特征字典的规范顺序
            row = np.asarray([[feats.get(c, 0.0) for c in sorted(feats)]], dtype=float)
        try:
            raw = self.model.predict_proba(row)
            # ProbabilityModel 接口返回 (n,)；原生 sklearn 返回 (n,2)
            p = np.asarray(raw, dtype=float)
            if p.ndim == 2:
                model_p = float(p[0, 1]) if p.shape[1] >= 2 else float(p[0, 0])
            else:
                model_p = float(p[0])
            model_p = float(np.clip(model_p, 0.0, 1.0))
        except Exception as exc:  # noqa: BLE001
            log.warning("model predict failed for %s: %s", market.slug, exc)
            return None

        llm_p = None
        if self.llm is not None and self.llm.enabled:
            llm_p = self.llm.score(market.question, market.description, market.category)
        return blend_probabilities(model_p, llm_p, self.llm_weight)
