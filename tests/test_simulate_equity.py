"""simulate_equity_updown 模拟回测：成交价计算、结算 PnL、入库记录结构。"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location(
    "simulate_equity_updown", ROOT / "scripts" / "simulate_equity_updown.py")
sim = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sim)


def test_sim_market_price_yes_uses_ask():
    """YES 侧：用 ask 价。"""
    book = {"bid": 0.40, "ask": 0.52}
    assert sim.sim_market_price(book, "YES", 0.50) == pytest.approx(0.52, rel=1e-6)


def test_sim_market_price_no_uses_1_minus_bid():
    """NO 侧：用 1 - bid。"""
    book = {"bid": 0.40, "ask": 0.52}
    assert sim.sim_market_price(book, "NO", 0.50) == pytest.approx(0.60, rel=1e-6)


def test_sim_market_price_fallback_ref():
    """无盘口回退 ref 价。"""
    assert sim.sim_market_price(None, "YES", 0.47) == pytest.approx(0.47, rel=1e-6)


def test_sim_market_price_caps_at_0_97():
    """极端价 cap 到 0.97（与 5m 盘一致）。"""
    assert sim.sim_market_price(None, "YES", 0.99) == pytest.approx(0.97, rel=1e-6)


def test_fetch_settlement_resolved(monkeypatch):
    """已结算盘返回 YES 结算价。"""
    class FakeHttp:
        def get_json(self, url, params=None, **kw):
            return [{"markets": [{"slug": "nvda-up-or-down-on-august-14-2026",
                                  "outcomePrices": "[1.0, 0.0]"}]}]

    assert sim.fetch_settlement(FakeHttp(), "nvda-up-or-down-on-august-14-2026") == 1.0


def test_fetch_settlement_unresolved(monkeypatch):
    """未结算盘返回 None。"""
    class FakeHttp:
        def get_json(self, url, params=None, **kw):
            return [{"markets": [{"slug": "nvda-up-or-down-on-august-14-2026",
                                  "outcomePrices": "[0.55, 0.45]"}]}]

    assert sim.fetch_settlement(FakeHttp(), "nvda-up-or-down-on-august-14-2026") is None


def test_fetch_settlement_missing(monkeypatch):
    """查不到返回 None。"""
    class FakeHttp:
        def get_json(self, url, params=None, **kw):
            return []

    assert sim.fetch_settlement(FakeHttp(), "xxx") is None


def test_trade_record_structure():
    """模拟成交记录结构：window=daily 且含入库所需字段。"""
    t = {
        "trade_id": "abc123", "slug": "nvda-up-or-down-on-august-14-2026",
        "coin": "nvda", "window": "daily", "side": "YES",
        "llm_p": 0.55, "ref": 0.47, "edge": 0.08,
        "size_usd": 100.0, "entry_price": 0.52,
    }
    assert t["window"] == "daily"
    assert t["coin"] == "nvda"
    # 与 db.insert_pending 所需 key 对齐
    required = {"trade_id", "slug", "coin", "window", "side", "entry_price",
                "size_usd"}
    assert required <= set(t.keys())


def test_build_db_rec_simulate():
    """simulate 模式：无 order 字段，mode=simulate。"""
    t = {
        "trade_id": "abc123", "slug": "nvda-up-or-down-on-august-14-2026",
        "coin": "nvda", "window": "daily", "side": "YES",
        "llm_p": 0.55, "ref": 0.47, "edge": 0.08,
        "size_usd": 100.0, "entry_price": 0.52,
        "llm_reason": "r", "model": "m", "results_file": "/tmp/x.jsonl",
    }
    rec = sim.build_db_rec(t, mode="simulate")
    assert rec["mode"] == "simulate"
    assert rec["window"] == "daily"
    assert rec["ref_price"] == 0.47
    assert "order_id" not in rec and "fill_tx" not in rec


def test_build_db_rec_live():
    """live 模式：带 order 字段。"""
    t = {
        "trade_id": "abc123", "slug": "nvda-up-or-down-on-august-14-2026",
        "coin": "nvda", "window": "daily", "side": "YES",
        "llm_p": 0.55, "ref": 0.47, "edge": 0.08,
        "size_usd": 100.0, "entry_price": 0.52,
        "order_id": "ord-1", "order_status": "matched",
        "fill_price": 0.51, "fill_tx": "0xabc",
    }
    rec = sim.build_db_rec(t, mode="live")
    assert rec["mode"] == "live"
    assert rec["order_id"] == "ord-1"
    assert rec["fill_price"] == 0.51
    assert rec["fill_tx"] == "0xabc"


def test_db_rec_construction():
    """入库 rec 字段与 db.insert_pending 列对齐。"""
    t = {
        "trade_id": "abc123", "slug": "nvda-up-or-down-on-august-14-2026",
        "coin": "nvda", "window": "daily", "side": "YES",
        "llm_p": 0.55, "ref": 0.47, "edge": 0.08,
        "size_usd": 100.0, "entry_price": 0.52,
        "llm_reason": "r", "model": "m",
        "results_file": "/tmp/x.jsonl",
    }
    rec = sim.build_db_rec(t)
    # insert_pending 需要这些 key（缺失会 KeyError）
    for k in ("trade_id", "slug", "side", "entry_price", "size_usd"):
        assert k in rec
    assert rec["llm_p"] == 0.55 and rec["ref_price"] == 0.47
