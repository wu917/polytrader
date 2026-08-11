"""配置系统测试。"""
import os

from polytrader.config import load_config


def test_load_default_config():
    cfg = load_config()
    assert cfg.mode == "dry-run"
    assert cfg.arbitrage_enabled is True
    assert cfg.arbitrage_min_edge == 0.02
    assert cfg.max_daily_loss_usd == 100.0
    assert cfg.kelly_fraction == 0.25
    assert cfg.max_total_exposure_usd == 3000.0


def test_env_override_priority():
    os.environ["POLY_STRATEGIES__ARBITRAGE__MIN_EDGE"] = "0.08"
    try:
        cfg = load_config(load_env_file=False)
        assert cfg.mode == "dry-run"                 # mode 受保护，不可 env 覆盖
        assert cfg.arbitrage_min_edge == 0.08        # 非保护参数 env 覆盖生效
    finally:
        os.environ.pop("POLY_STRATEGIES__ARBITRAGE__MIN_EDGE", None)


def test_env_cannot_override_protected_risk_params():
    """安全：风控参数与 mode 禁止通过 POLY_ env 覆盖（只能改 config.yaml）。"""
    os.environ["POLY_RISK__MAX_DAILY_LOSS_USD"] = "999999"
    os.environ["POLY_RISK__COOLDOWN_SECONDS"] = "0"
    os.environ["POLY_MODE"] = "live"
    try:
        cfg = load_config(load_env_file=False)
        assert cfg.max_daily_loss_usd == 100.0   # 仍是 YAML/默认值
        assert cfg.cooldown_seconds == 300
        assert cfg.mode == "dry-run"             # live 不能被 env 打开
    finally:
        os.environ.pop("POLY_RISK__MAX_DAILY_LOSS_USD", None)
        os.environ.pop("POLY_RISK__COOLDOWN_SECONDS", None)
        os.environ.pop("POLY_MODE", None)


def test_config_repr_masks_credentials():
    """安全：Config.__repr__ 不得泄漏凭证。"""
    os.environ["POLYMARKET_PRIVATE_KEY"] = "0xsupersecret"
    os.environ["POLYMARKET_API_KEY"] = "apikey123"
    try:
        cfg = load_config(load_env_file=False)
        rep = repr(cfg)
        assert "0xsupersecret" not in rep
        assert "apikey123" not in rep
        assert "private_key='***'" in rep
    finally:
        os.environ.pop("POLYMARKET_PRIVATE_KEY", None)
        os.environ.pop("POLYMARKET_API_KEY", None)


def test_credentials_from_env():
    os.environ["POLYMARKET_PRIVATE_KEY"] = "0xabc"
    os.environ["POLYMARKET_API_KEY"] = "key"
    os.environ["POLYMARKET_API_SECRET"] = "secret"
    os.environ["POLYMARKET_API_PASSPHRASE"] = "phrase"
    try:
        cfg = load_config(load_env_file=False)
        assert cfg.private_key == "0xabc"
        assert cfg.credentials_present is True
    finally:
        for k in ("POLYMARKET_PRIVATE_KEY", "POLYMARKET_API_KEY",
                  "POLYMARKET_API_SECRET", "POLYMARKET_API_PASSPHRASE"):
            os.environ.pop(k, None)


def test_live_mode_requires_credentials():
    cfg = load_config(load_env_file=False)
    cfg.mode = "live"
    assert cfg.is_live
    assert not cfg.credentials_present  # 真实环境无凭证，live 应拒绝启动
