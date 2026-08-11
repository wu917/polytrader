"""AI 模型真实数据训练流水线。

从 Gamma 拉取已解决（closed）市场作为标签样本，拉取历史价格构造特征，
训练 + 校准概率模型并保存 artifact，供 polytrader run 加载使用。

用法: .venv/bin/python scripts/train_ai.py [--proxy socks5h://127.0.0.1:7890]
                                        [--samples 400] [--out models/ai_artifact.pkl]
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.ai.features import feature_matrix
from polytrader.ai.train import build_dataset, extract_label, save_artifact, train_model
from polytrader.data.data_api import DataApiClient
from polytrader.data.gamma_client import GammaClient
from polytrader.data.http_client import HttpClient


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--proxy", default="socks5h://127.0.0.1:7890")
    ap.add_argument("--samples", type=int, default=400, help="最多拉取多少已解决市场")
    ap.add_argument("--out", default="models/ai_artifact.pkl")
    ap.add_argument("--no-calibrate", action="store_true")
    args = ap.parse_args()

    http = HttpClient(proxy=args.proxy)
    gamma = GammaClient(http=http)
    data = DataApiClient(http=http)

    print(f"[train] fetching up to {args.samples} closed markets...")
    closed = []
    offset = 0
    while len(closed) < args.samples:
        batch = gamma.get_markets(limit=100, offset=offset, active=False, closed=True)
        if not batch:
            break
        closed.extend(batch)
        offset += 100
        if len(batch) < 100:
            break
    closed = closed[:args.samples]
    print(f"[train] fetched {len(closed)} closed markets")

    labeled = [m for m in closed if extract_label(m) is not None]
    print(f"[train] labeled (binary, resolved): {len(labeled)}")

    if len(labeled) < 20:
        print("[train] too few labeled markets, abort")
        return 1

    # 拉历史价格（限制数量，避免过慢；已解决市场需按结算时间回看，
    # CLOB /prices-history 默认只返回最近 7 天）
    from polytrader.ai.backtest import _end_ts

    histories = {}
    for i, m in enumerate(labeled[:200]):
        try:
            end_ts = _end_ts(m)
            if end_ts == float("inf"):
                end_ts = None
            start_ts = (end_ts - 30 * 86400) if end_ts else None
            token_id = m.outcomes[0].token_id if m.outcomes else ""
            if not token_id:
                continue
            histories[m.condition_id] = data.price_history(
                token_id, interval="1h", start_ts=start_ts, end_ts=end_ts)
        except Exception as exc:  # noqa: BLE001
            print(f"  [warn] price-history failed for {m.slug}: {exc}")
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{min(len(labeled), 200)} histories")

    result = build_dataset(labeled, histories=histories)
    if result is None:
        print("[train] dataset build failed")
        return 1
    X, y, cols = result
    print(f"[train] dataset X={X.shape} y_yes={int(y.sum())}/{len(y)}")

    artifact = train_model(X, y, calibrate=not args.no_calibrate)
    artifact["columns"] = cols
    save_artifact(artifact, args.out)
    print(f"[train] done -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
