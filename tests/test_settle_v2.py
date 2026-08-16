"""settle_v2 测试：CLOB 持仓结算判定（positions/closed/REDEEM 区分）。"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.backfill_settlements import settle_from_clob


# ---------- settle_from_clob 核心判定 ----------

def test_cur_price_1_settles_yes():
    """当前持仓 curPrice=1.0 → 结算 1.0（赢）。"""
    positions = {"tokA": {"curPrice": 1.0, "size": 1.2}}
    assert settle_from_clob("tokA", positions, {}, {}) == 1.0


def test_cur_price_0_settles_no():
    """当前持仓 curPrice=0.0 → 结算 0.0（输）。"""
    positions = {"tokA": {"curPrice": 0.0, "size": 1.2}}
    assert settle_from_clob("tokA", positions, {}, {}) == 0.0


def test_cur_price_mid_not_settled():
    """curPrice 非 0/1（0.575）→ 未结算，返回 None。"""
    positions = {"tokA": {"curPrice": 0.575}}
    assert settle_from_clob("tokA", positions, {}, {}) is None


def test_closed_with_redeem_settles():
    """持仓消失 + closed curPrice=1 + activity REDEEM → 结算 1.0。"""
    closed = {"tokA": {"curPrice": 1.0, "realizedPnl": 2.09}}
    actions = {"tokA": {"REDEEM"}}
    assert settle_from_clob("tokA", {}, closed, actions) == 1.0


def test_closed_without_redeem_is_manual_close():
    """持仓消失 + closed 但无 REDEEM（手动平仓 SELL）→ 不自动结算 None。"""
    closed = {"tokA": {"curPrice": 0.5, "realizedPnl": 0.3}}
    actions = {"tokA": {"SELL"}}
    assert settle_from_clob("tokA", {}, closed, actions) is None


def test_closed_redeem_zero_settles():
    """REDEEM 且 closed curPrice=0 → 结算 0.0。"""
    closed = {"tokA": {"curPrice": 0.0, "realizedPnl": -0.5}}
    actions = {"tokA": {"REDEEM"}}
    assert settle_from_clob("tokA", {}, closed, actions) == 0.0


def test_unknown_asset_returns_none():
    """完全无记录 → None（调用方回退 gamma）。"""
    assert settle_from_clob("tokX", {}, {}, {}) is None
