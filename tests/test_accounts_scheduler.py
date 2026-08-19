"""多账户配置与 equity 调度器测试。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from polytrader.accounts import Account, get_account, load_accounts  # noqa: E402
from scripts.run_equity_scheduler import (build_cmd, load_equity_config,  # noqa: E402
                                          _et_now)
from scripts.simulate_equity_updown import _parse_symbols  # noqa: E402


# ---------- 账户配置 ----------

def test_load_accounts_from_yaml(tmp_path, monkeypatch):
    """accounts.yaml 多账户加载：私钥 env 引用优先、deposit 回退 env。"""
    f = tmp_path / "accounts.yaml"
    f.write_text("""
accounts:
  default:
    deposit_wallet: "0xDep111"
    private_key_env: TEST_PK_ENV
  equity_acct:
    deposit_wallet: "0xDep222"
    private_key: "0xplaintextkey"
""", encoding="utf-8")
    monkeypatch.setenv("TEST_PK_ENV", "0xenvkey123")
    monkeypatch.delenv("POLYMARKET_DEPOSIT_WALLET", raising=False)
    accs = load_accounts(f)
    assert set(accs) == {"default", "equity_acct"}
    assert accs["default"].private_key == "0xenvkey123"
    assert accs["default"].deposit_wallet == "0xDep111"
    assert accs["equity_acct"].private_key == "0xplaintextkey"
    assert accs["equity_acct"].deposit_wallet == "0xDep222"


def test_get_account_env_fallback(monkeypatch, tmp_path):
    """无配置文件/字段缺省时回退全局 env（兼容旧行为）。"""
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xglobalpk")
    monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", "0xGlobalDep")
    a = get_account("nonexistent", path=tmp_path / "missing.yaml")
    assert isinstance(a, Account)
    assert a.private_key == "0xglobalpk"
    assert a.deposit_wallet == "0xGlobalDep"


def test_get_account_unknown_returns_default(tmp_path, monkeypatch):
    """未知账户回退 default（任务策略误配不阻塞启动）。"""
    f = tmp_path / "accounts.yaml"
    f.write_text("""
accounts:
  default:
    deposit_wallet: "0xDep111"
    private_key: "0xpk111"
""", encoding="utf-8")
    monkeypatch.delenv("POLYMARKET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("POLYMARKET_DEPOSIT_WALLET", raising=False)
    a = get_account("typo_acct", path=f)
    assert a.name == "default"
    assert a.deposit_wallet == "0xDep111"


# ---------- equity 调度器 ----------

def test_load_equity_config_defaults(tmp_path):
    """配置文件缺失时回退内置默认（调度器可用）。"""
    cfg = load_equity_config(tmp_path / "missing.yaml")
    assert cfg["schedule"]["timezone"] == "America/New_York"
    assert cfg["schedule"]["runs"] == ["09:30", "12:00", "14:30"]
    assert cfg["live"] is False
    assert cfg["account"] == "default"


def test_build_cmd_simulate():
    """live=false → 复用 simulate_equity_updown.py（不重复实现交易）。"""
    cfg = load_equity_config()
    cfg["symbols"] = ["nvda", "spy"]
    cfg["account"] = "equity_acct"
    cfg["size"] = 2.0
    cmd = build_cmd(cfg)
    assert "simulate_equity_updown.py" in " ".join(cmd)
    assert "--account" in cmd and "equity_acct" in cmd
    assert "--symbols" in cmd and "nvda,spy" in cmd
    assert "--size" in cmd and "2.0" in cmd


def test_build_cmd_live():
    """live=true → 复用 run_equity_live_loop.py（真实下单链路）。"""
    cfg = load_equity_config()
    cfg["live"] = True
    cfg["per_run"] = 5
    cmd = build_cmd(cfg)
    assert "run_equity_live_loop.py" in " ".join(cmd)
    assert "--per-round" in cmd and "5" in cmd


def test_et_now_timezone():
    """美东时区换算（调度触发用美东本地时间）。"""
    dt = _et_now("America/New_York")
    assert dt.tzinfo is not None
    assert dt.strftime("%Y-%m-%d")  # 可格式化


def test_parse_symbols():
    """--symbols 参数解析：小写化/空=全部/None 语义。"""
    assert _parse_symbols("NVDA, Spy,tsla") == ["nvda", "spy", "tsla"]
    assert _parse_symbols("") is None
    assert _parse_symbols(None) is None
    assert _parse_symbols(" nvda ,, , ") == ["nvda"]
