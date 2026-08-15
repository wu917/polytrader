"""跟单过滤决策链模拟测试：多轮活动流回放（套利/冲单/正常/混合/过期恢复）。

用 ReplayApi 按轮次回放活动流，验证 MirrorEngine 跨轮决策：
- 套利市场（双 BUY 反向）后续 BUY 持续过滤（跨轮记忆）
- 冲单市场（BUY→SELL）后续 BUY 过滤
- 正常加仓（同侧 BUY）持续跟随
- 时间窗过期后恢复跟随
"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.copytrade.leaderboard import SeedProvider
from polytrader.copytrade.mirror import MirrorEngine
from polytrader.models import WalletProfile


def act(tx, cid, idx=0, side="BUY", ts=None, outcome=None):
    return {"type": "TRADE", "side": side, "size": "25", "price": "0.49",
            "asset": f"tok-{tx}", "transactionHash": tx,
            "timestamp": int(ts or time.time()), "conditionId": cid,
            "title": f"Q {cid}", "slug": f"m-{cid}",
            "outcome": outcome or ("Yes" if idx == 0 else "No"),
            "outcomeIndex": idx}


class ReplayApi:
    """按轮次回放活动流（模拟多轮 20s 扫描）。"""

    def __init__(self, rounds_data):
        self.rounds_data = rounds_data
        self.round = 0

    def get_user_activity(self, wallet, limit=50):
        return self.rounds_data[min(self.round, len(self.rounds_data) - 1)] \
            .get(wallet, [])

    def advance(self):
        self.round += 1

    def get_trades(self, cid, limit=100):
        return []

    def get_user_trades(self, wallet, limit=100):
        return []


def _engine(api, **kw):
    profile = WalletProfile(address="0xpro", realized_profit_usd=8000)
    params = {"min_profit_usd": 5000, "min_trades": 0, "require_activity": False}
    params.update(kw)
    return MirrorEngine(SeedProvider([profile]), api, **params)


def _run(engine, api, rounds):
    """跑 N 轮，返回每轮信号数。"""
    out = []
    for i in range(rounds):
        engine.refresh_targets()
        out.append(len(engine.scan_activity()))
        api.advance()
    return out


# ---------- 场景模拟 ----------

def test_arb_market_filtered_across_rounds():
    """套利场景：轮1 BUY YES → 轮2 BUY NO（发现套利）→ 轮3 BUY YES 仍被过滤。"""
    api = ReplayApi([
        {"0xpro": [act("0xa1", "condA", idx=0)]},
        {"0xpro": [act("0xa2", "condA", idx=1, outcome="No")]},
        {"0xpro": [act("0xa3", "condA", idx=0)]},
    ])
    engine = _engine(api)
    sigs = _run(engine, api, 3)
    # 轮1：未知套利，第一笔 BUY 正常出信号
    # 轮2：NO 侧被 yes_only 跳过（不入信号）；但条件已记录 → 后续过滤
    # 轮3：套利市场，BUY 被过滤
    assert sigs == [1, 0, 0], f"套利场景预期 [1,0,0] 实际 {sigs}"


def test_wash_roundtrip_filtered_across_rounds():
    """冲单场景：轮1 BUY YES → 轮2 SELL → 轮3 BUY YES 被过滤。"""
    api = ReplayApi([
        {"0xpro": [act("0xb1", "condB", idx=0)]},
        {"0xpro": [act("0xb2", "condB", idx=1, side="SELL")]},
        {"0xpro": [act("0xb3", "condB", idx=0)]},
    ])
    engine = _engine(api)
    sigs = _run(engine, api, 3)
    assert sigs == [1, 0, 0], f"冲单场景预期 [1,0,0] 实际 {sigs}"


def test_normal_add_position_kept_across_rounds():
    """正常加仓：轮1 BUY YES → 轮2 同侧 BUY YES → 均跟随。"""
    api = ReplayApi([
        {"0xpro": [act("0xc1", "condC", idx=0)]},
        {"0xpro": [act("0xc2", "condC", idx=0)]},
        {"0xpro": [act("0xc3", "condC", idx=0)]},
    ])
    engine = _engine(api)
    sigs = _run(engine, api, 3)
    assert sigs == [1, 1, 1], f"加仓场景预期 [1,1,1] 实际 {sigs}"


def test_mixed_wallet_only_arb_market_filtered():
    """混合钱包：condD 套利被过滤，condE/condF 正常保留。"""
    api = ReplayApi([
        {"0xpro": [act("0xd1", "condD", idx=0), act("0xe1", "condE", idx=0)]},
        {"0xpro": [act("0xd2", "condD", idx=1, outcome="No"),
                   act("0xf1", "condF", idx=0)]},
        {"0xpro": [act("0xd3", "condD", idx=0), act("0xe2", "condE", idx=0)]},
    ])
    engine = _engine(api)
    sigs = _run(engine, api, 3)
    # 轮1: D+E 各 1；轮2: F 1（D 的 NO 被 yes_only 跳过）；轮3: D 过滤 + E 1
    assert sigs == [2, 1, 1], f"混合场景预期 [2,1,1] 实际 {sigs}"


def test_wash_window_expiry_recovers():
    """时间窗过期：SELL 记录 2 小时后同市场 BUY 恢复跟随。"""
    api = ReplayApi([
        {"0xpro": [act("0g1", "condG", idx=1, side="SELL")]},
        {"0xpro": [act("0g2", "condG", idx=0)]},
    ])
    engine = _engine(api)
    # 第一轮记录 SELL
    assert _run(engine, api, 1) == [0]
    # 模拟时间流逝：把历史记录时间戳改为 2 小时前
    for cid, items in engine._wallet_hist["0xpro"].items():
        engine._wallet_hist["0xpro"][cid] = [
            (s, i, time.time() - 7200) for s, i, _ in items]
    assert _run(engine, api, 1) == [1], "窗口过期后应恢复跟随"


def test_same_market_different_wallets_isolated():
    """不同钱包在同一市场的行为互不影响（按钱包隔离）。"""
    api = ReplayApi([
        {"0xpro": [act("0h1", "condH", idx=0)],
         "0xother": [act("0h2", "condH", idx=1, outcome="No")]},
        {"0xpro": [act("0h3", "condH", idx=0)],
         "0xother": [act("0h4", "condH", idx=0)]},
    ])
    profile1 = WalletProfile(address="0xpro", realized_profit_usd=8000)
    profile2 = WalletProfile(address="0xother", realized_profit_usd=8000)
    engine = MirrorEngine(SeedProvider([profile1, profile2]), api,
                          min_profit_usd=5000, min_trades=0,
                          require_activity=False)
    engine.refresh_targets()
    r1 = engine.scan_activity()
    api.advance()
    r2 = engine.scan_activity()
    # 0xother 在 condH 上有 NO BUY（yes_only 跳过），0xpro 的 BUY 不受影响
    assert len(r1) == 1 and len(r2) == 1
