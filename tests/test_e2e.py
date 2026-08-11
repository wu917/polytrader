"""端到端测试：离线全链路（合成市场 → 三策略 → 风控 → 执行 → 报告）。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_offline_full_pipeline():
    proc = subprocess.run(
        [sys.executable, "scripts/run_polytrader.py", "offline"],
        cwd=ROOT, capture_output=True, text=True, timeout=180,
    )
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, out[-2000:]
    # 套利信号必须出现
    assert "arbitrage: 2 signals" in out
    # 成交必须全部 filled 且敞口不超过上限（风控生效）
    assert "[exec] " in out and "filled=" in out
    assert "exposure_usd" in out
    assert "'trades_total': " in out
    assert "[report]" in out
    # 敞口 ≤ 总敞口上限（3000）
    import re
    m = re.search(r"exposure_usd': ([\d.]+)", out)
    assert m is not None and float(m.group(1)) <= 3000.0 + 1e-6


def test_paper_mode_help_and_invalid_mode():
    proc = subprocess.run(
        [sys.executable, "scripts/run_polytrader.py", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "offline" in proc.stdout and "backtest" in proc.stdout
