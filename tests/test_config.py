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
    os.environ["POLY_MODE"] = "paper"
    os.environ["POLY_RISK__MAX_DAILY_LOSS_USD"] = "50"
    try:
        cfg = load_config(load_env_file=False)
        assert cfg.mode == "paper"
        assert cfg.max_daily_loss_usd == 50.0
    finally:
        os.environ.pop("POLY_MODE", None)
        os.environ.pop("POLY_RISK__MAX_DAILY_LOSS_USD", None)


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
