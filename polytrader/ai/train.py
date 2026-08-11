"""训练流水线：标签提取 + 训练 + 概率校准 + 模型持久化。"""
from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Any

import numpy as np

from polytrader.ai.features import feature_matrix
from polytrader.ai.models import ProbabilityModel, get_default_model
from polytrader.logging_setup import get_logger
from polytrader.models import Market

log = get_logger("ai.train")


def extract_label(market: Market) -> int | None:
    """从已解决市场提取标签：winner 的 outcome price == 1（容错判断）。

    支持两种形态：outcomePrices=[...,"1.0",...]（字符串数组），
    或 outcomes 对象数组 price 字段。
    """
    prices = []
    for o in market.outcomes:
        raw = o.price
        try:
            prices.append(float(raw))
        except (TypeError, ValueError):
            prices.append(0.0)
    if not prices:
        return None
    winners = [i for i, p in enumerate(prices) if p >= 0.999]
    if len(winners) == 1:
        return 1 if winners[0] == 0 else 0  # YES=1, NO=0（仅支持二元市场）
    # 未解决或平局 → 无标签
    return None


def build_dataset(
    markets: list[Market],
    books: dict[str, Any] | None = None,
    histories: dict[str, list[dict]] | None = None,
) -> tuple[np.ndarray, np.ndarray, list[str]] | None:
    """构造 (X, y, columns)。仅保留有标签的二元市场；样本过少返回 None。"""
    books = books or {}
    histories = histories or {}
    labeled: list[Market] = []
    for m in markets:
        if not m.is_binary:
            continue
        if extract_label(m) is not None:
            labeled.append(m)
    if len(labeled) < 20:
        log.warning("labeled markets too few: %d (need >= 20)", len(labeled))
        return None

    X, cols = feature_matrix(labeled, books, histories)
    y = np.asarray([extract_label(m) for m in labeled], dtype=int)
    return X, y, cols


def train_model(
    X: np.ndarray, y: np.ndarray,
    calibrate: bool = True,
    force_model: str | None = None,
    model_kwargs: dict | None = None,
) -> dict[str, Any]:
    """训练 + 可选概率校准（sklearn CalibratedClassifierCV 保序回归）。

    返回 artifact dict：{"model": ProbabilityModel, "columns": [...], "calibrated": bool}
    """
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.model_selection import train_test_split

    X = np.asarray(X, dtype=float)
    y = np.asarray(y, dtype=int)

    base = get_default_model(force=force_model)
    if model_kwargs:
        # 用传入 kwargs 重建（工厂不支持 kwargs 时跳过）
        if hasattr(base, "model"):
            try:
                base.model.set_params(**model_kwargs)
            except Exception as exc:  # noqa: BLE001
                log.warning("model kwargs ignored: %s", exc)

    if calibrate and len(X) >= 100:
        X_tr, X_val, y_tr, y_val = train_test_split(X, y, test_size=0.25, random_state=42)
        base.fit(X_tr, y_tr)
        calibrated = CalibratedClassifierCV(estimator=base.model, method="isotonic", cv=3)
        calibrated.fit(X_val, y_val)
        artifact_model = _CalibratedWrapper(calibrated, name=getattr(base, "name", "calibrated"))
        log.info("trained+calibrated: n=%d acc=%.3f",
                 len(X), _accuracy(calibrated, X_val, y_val))
    else:
        base.fit(X, y)
        artifact_model = base
        log.info("trained (no calib): n=%d", len(X))

    return {"model": artifact_model, "calibrated": calibrate}


def save_artifact(artifact: dict[str, Any], path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as fh:
        pickle.dump(artifact, fh)
    log.info("artifact saved to %s", path)


def load_artifact(path: str | Path) -> dict[str, Any]:
    with open(path, "rb") as fh:
        return pickle.load(fh)


class _CalibratedWrapper(ProbabilityModel):
    """包一层 CalibratedClassifierCV 以符合 ProbabilityModel 接口。"""

    name = "calibrated"

    def __init__(self, model: Any, name: str = "calibrated"):
        self.model = model
        self.name = name

    def fit(self, X: np.ndarray, y: np.ndarray) -> "_CalibratedWrapper":
        self.model.fit(np.asarray(X, dtype=float), np.asarray(y, dtype=int))
        return self

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        return self.model.predict_proba(np.asarray(X, dtype=float))[:, 1]


def _accuracy(model: Any, X: np.ndarray, y: np.ndarray) -> float:
    preds = (model.predict_proba(X)[:, 1] > 0.5).astype(int)
    return float((preds == y).mean())
