"""跟单实盘模式深度测试：参数校验、delayed 成交回填、tick/negRisk 缓存。"""
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import scripts.run_live_loop as rll
import scripts.run_copytrade_loop as rcl


# ---------- 实盘参数校验 ----------

def test_live_size_over_hard_limit_rejected(monkeypatch, capsys):
    """--live 且 --size > $1 拒绝启动（单笔硬上限）。"""
    monkeypatch.setattr(sys, "argv", [
        "run_copytrade_loop.py", "--live", "--size", "2", "--rounds", "1"])
    rc = rcl.main()
    assert rc == 2
    assert "硬上限" in capsys.readouterr().out


def test_live_max_orders_zero_rejected(monkeypatch, capsys):
    """--live 且 --max-live-orders <= 0 拒绝启动。"""
    monkeypatch.setattr(sys, "argv", [
        "run_copytrade_loop.py", "--live", "--max-live-orders", "0",
        "--rounds", "1"])
    rc = rcl.main()
    assert rc == 2
    assert "必须 ≥ 1" in capsys.readouterr().out


def test_paper_size_over_100_rejected(monkeypatch):
    """模拟模式 size 上限 100。"""
    monkeypatch.setattr(sys, "argv", [
        "run_copytrade_loop.py", "--size", "101", "--rounds", "1"])
    assert rcl.main() == 2


# ---------- delayed 成交回填（_reconcile_pending_fills）----------

class FakeAuth:
    def __init__(self, status_map):
        self.status_map = status_map  # order_id -> status
        self.calls = []

    def __call__(self, creds, eoa, order_id):
        self.calls.append(order_id)
        if order_id in self.status_map:
            return self.status_map[order_id]
        raise RuntimeError("network error")


def _mk_ctx(auth):
    return {
        "creds": {}, "eoa": "0xeoa", "get_order_auth": auth,
        "pending_fills": [
            ("t1", "0xorder1", time.time()),
            ("t2", "0xorder2", time.time()),
        ],
    }


def _matched_order(fill=0.82, tx="0xabc"):
    return {"status": "MATCHED", "makingAmount": str(fill * 1000000),
            "takingAmount": "1000000", "transactionsHashes": [tx]}


def test_reconcile_backfills_matched_order(monkeypatch, capsys):
    """MATCHED 订单回填成交价 + 更新 order_status + 移出队列。"""
    auth = FakeAuth({"0xorder1": _matched_order(),
                     "0xorder2": {"status": "OPEN"}})
    ctx = _mk_ctx(auth)
    marked = []

    def fake_mark_filled(tid, fill, tx):
        marked.append((tid, fill, tx))

    class FakeCur:
        def __init__(self, conn): self.conn = conn
        def execute(self, sql, args): self.conn.updates.append((sql, args))
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeConn:
        def __init__(self): self.updates = []
        def cursor(self): return FakeCur(self)
        def close(self): pass

    monkeypatch.setattr(rcl.db, "mark_filled", fake_mark_filled)
    monkeypatch.setattr(rcl.db, "connect", lambda: FakeConn())
    rcl._reconcile_pending_fills(ctx, print, lambda rec: None, 1)
    assert marked == [("t1", 0.82, "0xabc")]  # 成交价已回填
    assert [o for _, o, _ in ctx["pending_fills"]] == ["0xorder2"]  # 已成交移出
    out = capsys.readouterr().out
    assert "MATCHED fill=$0.82" in out


def test_reconcile_keeps_unmatched_and_retries(monkeypatch):
    """未成交/网络失败的订单保留，下轮重试。"""
    auth = FakeAuth({"0xorder1": {"status": "OPEN"},
                     "0xorder2": {"status": "DELAYED"}})
    ctx = _mk_ctx(auth)
    monkeypatch.setattr(rcl.db, "mark_filled", lambda *a: None)
    monkeypatch.setattr(rcl.db, "connect", lambda: _FakeConn())
    rcl._reconcile_pending_fills(ctx, print, lambda rec: None, 1)
    assert len(ctx["pending_fills"]) == 2  # 都保留
    assert len(auth.calls) == 2

    # 网络错误也保留
    ctx2 = _mk_ctx(FakeAuth({}))
    monkeypatch.setattr(rcl.db, "connect", lambda: _FakeConn())
    rcl._reconcile_pending_fills(ctx2, print, lambda rec: None, 1)
    assert len(ctx2["pending_fills"]) == 2


class _FakeConn:
    class _C:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def execute(self, *a): pass
    def cursor(self): return self._C()
    def close(self): pass


def test_reconcile_drops_timeout_order(monkeypatch, capsys):
    """超过 600s 未确认：最终查询——未成交则释放 DB 占坑并移出队列。"""
    auth = FakeAuth({"0xorder1": {"status": "OPEN"}})
    ctx = _mk_ctx(auth)
    ctx["pending_fills"][0] = ("t1", "0xorder1", time.time() - 700)
    released = []

    class FakeCur2:
        def __init__(self, conn): self.conn = conn
        def execute(self, sql, args):
            if "status='cancelled'" in sql:
                released.append(args)
        def __enter__(self): return self
        def __exit__(self, *a): return False

    class FakeConn2:
        def cursor(self): return FakeCur2(self)
        def close(self): pass

    monkeypatch.setattr(rcl.db, "mark_filled", lambda *a: None)
    monkeypatch.setattr(rcl.db, "connect", lambda: FakeConn2())
    rcl._reconcile_pending_fills(ctx, print, lambda rec: None, 1)
    assert len(ctx["pending_fills"]) == 1  # 超时的已移除
    assert released, "未成交单应释放占坑（status='cancelled'）"
    assert "已释放占坑名额" in capsys.readouterr().out


def test_reconcile_timeout_matched_backfills(monkeypatch):
    """超时但最终 MATCHED → 回填成交（不释放）。"""
    auth = FakeAuth({"0xorder1": _matched_order(0.71, "0xtx1")})
    ctx = _mk_ctx(auth)
    ctx["pending_fills"][0] = ("t1", "0xorder1", time.time() - 700)
    marked = []
    monkeypatch.setattr(rcl.db, "mark_filled",
                        lambda tid, fill, tx: marked.append((tid, fill, tx)))
    monkeypatch.setattr(rcl.db, "connect", lambda: _FakeConn())
    rcl._reconcile_pending_fills(ctx, print, lambda rec: None, 1)
    assert marked == [("t1", 0.71, "0xtx1")]
    assert len(ctx["pending_fills"]) == 1  # 超时单已处理移除


def test_reconcile_no_pending_noop():
    """无待确认订单时直接返回。"""
    ctx = {"pending_fills": []}
    rcl._reconcile_pending_fills(ctx, print, lambda rec: None, 1)  # 不抛异常


# ---------- tick / negRisk 解析（run_live_loop）----------

class FakeReq:
    def __init__(self, status=200, payload=None, exc=None):
        self.status, self.payload, self.exc = status, payload, exc
        self.calls = 0

    def __call__(self, method, url, **kw):
        self.calls += 1
        if self.exc:
            raise self.exc
        class R:
            status_code = self.status
            def json(self_): return self.payload
        return R()


def test_get_tick_size_success_and_cache(monkeypatch):
    """tick 查询成功入缓存，二次命中不请求。"""
    req = FakeReq(payload={"minimum_tick_size": 0.001})
    monkeypatch.setattr(rll, "_req", req)
    cache = {}
    assert rll._get_tick_size("tok1", cache) == 0.001
    assert rll._get_tick_size("tok1", cache) == 0.001
    assert req.calls == 1  # 缓存命中


def test_get_tick_size_failure_not_cached(monkeypatch):
    """查询失败返回默认 0.01 且不写缓存（下轮重试）。"""
    req = FakeReq(exc=RuntimeError("net"))
    monkeypatch.setattr(rll, "_req", req)
    cache = {}
    assert rll._get_tick_size("tok1", cache) == 0.01
    assert cache == {}  # 失败不缓存
    req2 = FakeReq(payload={"minimum_tick_size": 0.001})
    monkeypatch.setattr(rll, "_req", req2)
    assert rll._get_tick_size("tok1", cache) == 0.001  # 下轮可恢复


def test_get_neg_risk_success_and_default(monkeypatch):
    """neg_risk 成功入缓存；失败默认 False 且不缓存。"""
    req = FakeReq(payload={"neg_risk": True})
    monkeypatch.setattr(rll, "_req", req)
    cache = {}
    assert rll._get_neg_risk("tok1", cache) is True
    assert cache["tok1"][0] is True

    req2 = FakeReq(exc=RuntimeError("net"))
    monkeypatch.setattr(rll, "_req", req2)
    assert rll._get_neg_risk("tok2", cache) is False
    assert "tok2" not in cache


# ---------- 开单数统计 ----------

def test_count_open_filters_copytrade(monkeypatch):
    """_count_open 只统计 copytrade 窗口的 pending。"""
    class FakeDB:
        @staticmethod
        def fetch_pending():
            return [{"window": "copytrade", "status": "pending"},
                    {"window": "copytrade", "status": "pending"},
                    {"window": "5m", "status": "pending"}]
    assert rcl._count_open(FakeDB) == 2
