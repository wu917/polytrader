"""训练流水线：标签提取 + 训练 + 概率校准 + 模型持久化。

安全：artifact 为 pickle 格式，加载时强制路径白名单（models/ 目录内）
+ SHA-256 sidecar 完整性校验，防止从不可信来源加载模型导致 RCE。
"""
from __future__ import annotations

import hashlib
import logging
import pickle
from pathlib import Path
from typing import Any

import numpy as np

from polytrader.ai.features import feature_matrix
from polytrader.ai.models import ProbabilityModel, get_default_model
from polytrader.config import PROJECT_ROOT
from polytrader.logging_setup import get_logger
from polytrader.models import Market

log = get_logger("ai.train")

# artifact 允许的加载目录（默认安全边界）
ARTIFACT_DIRS = (PROJECT_ROOT / "models",)


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
    """保存 artifact 并生成 SHA-256 sidecar（load_artifact 校验用）。"""
    data = pickle.dumps(artifact)
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    Path(str(p) + ".sha256").write_text(digest + "\n")
    log.info("artifact saved to %s (sha256 %s)", p, digest[:12])


def load_artifact(path: str | Path, allow_untrusted: bool = False) -> dict[str, Any]:
    """加载 artifact，带安全校验：
    1. 默认只允许加载 ARTIFACT_DIRS（models/）内的文件——pickle 可执行任意代码，
       绝不能加载不可信来源的模型文件；
    2. 存在 .sha256 sidecar 时校验完整性（防损坏/篡改）。
    """
    p = Path(path).resolve()
    trusted = any(str(p).startswith(str(d.resolve())) for d in ARTIFACT_DIRS)
    if not allow_untrusted and not trusted:
        raise ValueError(
            f"refusing to load artifact from untrusted location: {p} "
            f"(allowed: {[str(d) for d in ARTIFACT_DIRS]}; "
            f"pass allow_untrusted=True only for files you fully control)"
        )
    data = p.read_bytes()
    sidecar = Path(str(p) + ".sha256")
    if sidecar.exists():
        expected = sidecar.read_text().strip()
        actual = hashlib.sha256(data).hexdigest()
        if expected != actual:
            raise ValueError(
                f"artifact checksum mismatch for {p}: file may be corrupted or tampered"
            )
    else:
        log.warning("artifact %s has no .sha256 sidecar; integrity not verified", p)
    return pickle.loads(data)


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
