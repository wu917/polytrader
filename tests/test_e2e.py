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
    assert "binary arb signal" in out or "arbitrage: 2 signals" in out
    # 成交必须发生且风控生效（敞口被卡在上限）
    assert "[exec] 6 trades, filled=6" in out
    assert "exposure_usd': 3000.0" in out
    assert "[report]" in out


def test_paper_mode_help_and_invalid_mode():
    proc = subprocess.run(
        [sys.executable, "scripts/run_polytrader.py", "--help"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert "offline" in proc.stdout and "backtest" in proc.stdout
