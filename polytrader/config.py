"""配置加载：YAML 主配置 + .env 环境变量覆盖。"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "config.yaml"

# YAML 嵌套路径 -> Config 字段名 的显式映射（避免歧义与命名漂移）
FIELD_MAP: dict[tuple[str, ...], str] = {
    ("mode",): "mode",
    ("network", "proxy"): "proxy",
    ("network", "request_timeout"): "request_timeout",
    ("network", "max_retries"): "max_retries",
    ("clob", "api_base"): "clob_api_base",
    ("clob", "ws_url"): "clob_ws_url",
    ("clob", "slippage_tolerance"): "slippage_tolerance",
    ("gamma", "api_base"): "gamma_api_base",
    ("data_api", "api_base"): "data_api_base",
    ("strategies", "arbitrage", "enabled"): "arbitrage_enabled",
    ("strategies", "arbitrage", "min_edge"): "arbitrage_min_edge",
    ("strategies", "arbitrage", "max_position_usd"): "arbitrage_max_position_usd",
    ("strategies", "ai_probability", "enabled"): "ai_enabled",
    ("strategies", "ai_probability", "min_edge"): "ai_min_edge",
    ("strategies", "ai_probability", "min_liquidity_usd"): "ai_min_liquidity_usd",
    ("strategies", "ai_probability", "kelly_fraction"): "kelly_fraction",
    ("strategies", "ai_probability", "llm_weight"): "llm_weight",
    ("strategies", "ai_probability", "llm_enabled"): "llm_enabled",
    ("strategies", "copytrade", "enabled"): "copytrade_enabled",
    ("strategies", "copytrade", "min_profit_usd"): "copytrade_min_profit_usd",
    ("strategies", "copytrade", "min_trades"): "copytrade_min_trades",
    ("strategies", "copytrade", "lookback_days"): "copytrade_lookback_days",
    ("strategies", "copytrade", "max_slippage"): "copytrade_max_slippage",
    ("strategies", "copytrade", "mirror_yes_only"): "copytrade_mirror_yes_only",
    ("risk", "max_position_usd"): "max_position_usd",
    ("risk", "max_total_exposure_usd"): "max_total_exposure_usd",
    ("risk", "max_daily_loss_usd"): "max_daily_loss_usd",
    ("risk", "max_drawdown_pct"): "max_drawdown_pct",
    ("risk", "max_open_positions"): "max_open_positions",
    ("risk", "min_price"): "min_price",
    ("risk", "max_price"): "max_price",
    ("risk", "cooldown_seconds"): "cooldown_seconds",
    ("execution", "order_timeout_seconds"): "order_timeout_seconds",
    ("execution", "cancel_on_timeout"): "cancel_on_timeout",
    ("execution", "fill_check_interval"): "fill_check_interval",
    ("logging", "level"): "log_level",
    ("logging", "file"): "log_file",
}


def load_env(env_path: Path | None = None) -> None:
    """加载 .env（已有环境变量优先，不覆盖）。"""
    env_file = env_path or PROJECT_ROOT / ".env"
    if env_file.exists():
        load_dotenv(env_file, override=False)


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _env_overrides(prefix: str = "POLY_") -> dict:
    """把 POLY_MODE / POLY_RISK__MAX_DAILY_LOSS_USD 之类的 env 转成嵌套 dict。

    语法: POLY_<PATH>，路径用 __ 分隔，如 POLY_RISK__MAX_DAILY_LOSS_USD=50。
    """
    out: dict = {}
    for key, val in os.environ.items():
        if not key.startswith(prefix):
            continue
        path = key[len(prefix):].lower().split("__")
        node = out
        for part in path[:-1]:
            node = node.setdefault(part, {})
        node[path[-1]] = _coerce(val)
    return out


def _coerce(val: str) -> Any:
    v = val.strip()
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    try:
        return int(v)
    except ValueError:
        pass
    try:
        return float(v)
    except ValueError:
        pass
    return v


def _walk_leaves(node: dict, prefix: tuple[str, ...] = ()) -> dict[tuple[str, ...], Any]:
    """递归收集所有叶子路径 -> 值。"""
    leaves: dict[tuple[str, ...], Any] = {}
    for k, v in node.items():
        path = prefix + (k,)
        if isinstance(v, dict) and v:
            leaves.update(_walk_leaves(v, path))
        else:
            leaves[path] = v
    return leaves


@dataclass
class Config:
    """扁平化的运行配置，所有子模块只依赖本对象。"""

    mode: str = "dry-run"  # dry-run | paper | live
    raw: dict[str, Any] = field(default_factory=dict)

    proxy: str = ""
    request_timeout: int = 15
    max_retries: int = 3

    clob_api_base: str = "https://clob.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com"
    slippage_tolerance: float = 0.02
    gamma_api_base: str = "https://gamma-api.polymarket.com"
    data_api_base: str = "https://data-api.polymarket.com"

    # 策略开关
    arbitrage_enabled: bool = True
    arbitrage_min_edge: float = 0.02
    arbitrage_max_position_usd: float = 1000.0

    ai_enabled: bool = True
    ai_min_edge: float = 0.05
    ai_min_liquidity_usd: float = 500.0
    kelly_fraction: float = 0.25
    llm_weight: float = 0.3
    llm_enabled: bool = False

    copytrade_enabled: bool = True
    copytrade_min_profit_usd: float = 5000.0
    copytrade_min_trades: int = 30
    copytrade_lookback_days: int = 90
    copytrade_max_slippage: float = 0.03
    copytrade_mirror_yes_only: bool = True

    # 风控
    max_position_usd: float = 500.0
    max_total_exposure_usd: float = 3000.0
    max_daily_loss_usd: float = 100.0
    max_drawdown_pct: float = 0.15
    max_open_positions: int = 10
    min_price: float = 0.03
    max_price: float = 0.97
    cooldown_seconds: int = 300

    # 执行
    order_timeout_seconds: int = 30
    cancel_on_timeout: bool = True
    fill_check_interval: int = 2

    log_level: str = "INFO"
    log_file: str = "logs/polytrader.log"

    # ---- 派生的 API 凭证（从 .env 读取）----
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    llm_api_key: str = ""
    llm_base_url: str = ""
    llm_model: str = "gpt-4o-mini"

    @property
    def is_live(self) -> bool:
        return self.mode == "live"

    @property
    def is_paper(self) -> bool:
        return self.mode == "paper"

    @property
    def credentials_present(self) -> bool:
        return bool(self.private_key and self.api_key and self.api_secret and self.api_passphrase)

    def effective_proxy(self) -> str | None:
        return self.proxy or os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None


def load_config(
    path: str | Path | None = None,
    env_path: str | Path | None = None,
    load_env_file: bool = True,
) -> Config:
    """加载配置。优先级：默认值 < YAML < POLY_ 环境变量。"""
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    raw: dict[str, Any] = {}
    if cfg_path.exists():
        raw = yaml.safe_load(cfg_path.read_text()) or {}

    if load_env_file:
        load_env(Path(env_path) if env_path else None)

    merged = _deep_merge(raw, _env_overrides())

    cfg = Config(raw=merged)
    for path, value in _walk_leaves(merged).items():
        field_name = FIELD_MAP.get(path)
        if field_name and hasattr(cfg, field_name):
            setattr(cfg, field_name, value)

    # 凭证
    cfg.private_key = os.environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    cfg.api_key = os.environ.get("POLYMARKET_API_KEY", "").strip()
    cfg.api_secret = os.environ.get("POLYMARKET_API_SECRET", "").strip()
    cfg.api_passphrase = os.environ.get("POLYMARKET_API_PASSPHRASE", "").strip()
    cfg.llm_api_key = os.environ.get("LLM_API_KEY", "").strip()
    cfg.llm_base_url = os.environ.get("LLM_BASE_URL", "https://api.openai.com/v1").strip()
    cfg.llm_model = os.environ.get("LLM_MODEL", cfg.llm_model).strip()

    return cfg
