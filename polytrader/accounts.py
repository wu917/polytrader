"""PolyTrader 账户配置加载：钱包与 Polymarket 账号地址。

config/accounts.yaml 维护多账户（deposit_wallet/funder/私钥 env 引用），
任务启动时 `--account <name>` 选择，入 pending_trades.account 区分统计。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ACCOUNTS_FILE = ROOT / "config" / "accounts.yaml"


@dataclass
class Account:
    """单账户：EOA 私钥 + Polymarket deposit/funder 地址。"""

    name: str
    deposit_wallet: str = ""
    funder: str = ""
    private_key: str = ""
    # 原始配置（调试/兜底用）
    raw: dict = field(default_factory=dict, repr=False)

    @property
    def eoa(self) -> str:
        """从私钥推导 EOA 地址（无密钥返回空串）。"""
        if not self.private_key:
            return ""
        try:
            from eth_account import Account as EthAccount
            return EthAccount.from_key(self.private_key).address
        except Exception:  # noqa: BLE001
            return ""


def load_accounts(path: Path | None = None) -> dict[str, Account]:
    """加载所有账户。文件缺失/解析失败返回空 dict（调用方回退 env）。"""
    p = path or DEFAULT_ACCOUNTS_FILE
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 配置坏不阻塞启动
        return {}
    accounts_raw = data.get("accounts", data) or {}
    out: dict[str, Account] = {}
    for name, cfg in accounts_raw.items():
        if not isinstance(cfg, dict):
            continue
        out[str(name)] = _build_account(str(name), cfg)
    return out


def _build_account(name: str, cfg: dict) -> Account:
    """解析单账户：私钥 env 引用优先，明文兜底，再回退全局 env。"""
    pk = ""
    env_name = str(cfg.get("private_key_env", "") or "").strip()
    if env_name:
        pk = os.environ.get(env_name, "").strip()
    if not pk:
        pk = str(cfg.get("private_key", "") or "").strip()
    if not pk:
        pk = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    deposit = str(cfg.get("deposit_wallet", "") or "").strip()
    if not deposit:
        deposit = os.environ.get("POLYMARKET_DEPOSIT_WALLET", "").strip()
    return Account(
        name=name,
        deposit_wallet=deposit,
        funder=str(cfg.get("funder", "") or "").strip(),
        private_key=pk,
        raw=cfg,
    )


def get_account(name: str = "default", path: Path | None = None) -> Account:
    """按名取账户；不存在时回退 default / 纯 env 构造。"""
    accounts = load_accounts(path)
    if name in accounts:
        return accounts[name]
    if name != "default" and "default" in accounts:
        return accounts["default"]
    # 无配置文件：纯 env 兜底（兼容旧行为）
    return _build_account(name, {})


def resolve_account(name: str, path: Path | None = None) -> Account:
    """get_account 别名（语义：解析运行账户）。"""
    return get_account(name, path)
