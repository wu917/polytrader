"""可插拔概率模型层。

设计：优先 LightGBM（梯度提升，预测市场概率建模的行业默认）；
若环境缺少 libomp/lightgbm（macOS 常见），自动 fallback 到 sklearn
HistGradientBoostingClassifier（无需 OpenMP 运行时），接口一致。
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

import numpy as np

from polytrader.logging_setup import get_logger

log = get_logger("ai.models")

try:
    import lightgbm as _lgb  # type: ignore

    LGBM_AVAILABLE = True
except (ImportError, OSError) as _exc:  # OSError: macOS 缺 libomp 时 dylib 加载失败
    _lgb = None  # type: ignore
    LGBM_AVAILABLE = False
    log.warning("lightgbm unavailable: %s — using sklearn fallback", _exc)

if LGBM_AVAILABLE:
    log.info("lightgbm %s available", _lgb.__version__)
else:
    log.warning("lightgbm unavailable (libomp missing?) — falling back to sklearn HistGradientBoosting")


class ProbabilityModel(ABC):
    """二分类概率模型接口。"""

    @abstractmethod
    def fit(self, X: np.ndarray, y: np.ndarray) -> "ProbabilityModel":
        ...

    @abstractmethod
    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        """返回 P(Yes) ∈ [0,1]，形状 (n,)。"""


class HistGBProbabilityModel(ProbabilityModel):
    """sklearn HistGradientBoosting，无 OpenMP 依赖，任何环境可跑。"""

    name = "histgb"

    def __init__(self, **kwargs: Any):
        from sklearn.ensemble import HistGradientBoostingClassifier

        params = dict(
            max_iter=300,
            learning_rate=0.05,
            max_leaf_nodes=31,
            min_samples_leaf=20,
            l2_regularization=1.0,
            random_state=42,
        )
        params.update(kwargs)
        self.model = HistGradientBoostingClassifier(**params)

    def fit(self, X: np.ndarray, y: np.ndarray) -> "HistGBProbabilityModel":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=int)
        # 小样本自适应：min_samples_leaf 过大（如默认 20）会让 <40 样本
        # 的数据集完全无法分裂（每叶子需 >=20），导致模型退化为均值预测。
        n = len(y)
        leaf = max(2, min(int(self.model.min_samples_leaf), n // 10))
        if leaf != self.model.min_samples_leaf:
            self.model.set_params(min_samples_leaf=leaf)
        self.model.fit(X, y)
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(np.asarray(X, dtype=float))[:, 1]


class LGBMProbabilityModel(ProbabilityModel):
    """LightGBM（需 libomp）。"""

    name = "lgbm"

    def __init__(self, **kwargs: Any):
        if _lgb is None:
            raise RuntimeError("lightgbm not installed")
        params = dict(
            objective="binary",
            metric="binary_logloss",
            num_leaves=31,
            learning_rate=0.05,
            n_estimators=300,
            min_child_samples=20,
            reg_lambda=1.0,
            random_state=42,
            verbosity=-1,
        )
        params.update(kwargs)
        self.model = _lgb.LGBMClassifier(**params)

    def fit(self, X: np.ndarray, y: np.ndarray) -> "LGBMProbabilityModel":
        X = np.asarray(X, dtype=float)
        y = np.asarray(y, dtype=int)
        # 小样本自适应（同 HistGB）：min_child_samples 默认 20
        n = len(y)
        leaf = max(2, min(int(self.model.min_child_samples), n // 10))
        if leaf != self.model.min_child_samples:
            self.model.set_params(min_child_samples=leaf)
        self.model.fit(X, y)
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(np.asarray(X, dtype=float))[:, 1]


def get_default_model(force: str | None = None) -> ProbabilityModel:
    """模型工厂：force='lgbm' 强制 LightGBM（不可用则报错），否则优先 lgbm、fallback histgb。"""
    if force == "lgbm":
        if not LGBM_AVAILABLE:
            raise RuntimeError("lightgbm requested but unavailable (install libomp + lightgbm)")
        return LGBMProbabilityModel()
    if force == "histgb":
        return HistGBProbabilityModel()
    if LGBM_AVAILABLE:
        return LGBMProbabilityModel()
    return HistGBProbabilityModel()
