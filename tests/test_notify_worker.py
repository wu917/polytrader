"""飞书通知 worker 测试。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

import scripts.notify_worker as nw  # noqa: E402


# ---------- 报告生成（mock 数据源） ----------

def test_build_report_content(monkeypatch, tmp_path):
    """报告包含资金/持仓/钱包画像/新结算四要素。"""
    monkeypatch.setattr(nw, "STATE_FILE", tmp_path / "state.json")
    # 首次运行（无上次状态）：ts=0 → 不显示新结算（无"上一次"基准）
    monkeypatch.setattr(nw, "get_balance", lambda: 12.34)
    monkeypatch.setattr(nw, "get_positions", lambda: [
        {"slug": "spy-up-or-down-on-x", "window": "daily", "side": "YES",
         "fill_price": 0.48, "created_at": "2026-08-19 09:30:00"}])
    monkeypatch.setattr(nw, "get_wallet_stats", lambda: [
        {"wallet": "0x204f72f35326", "n": 5, "wins": 3, "pnl": 1.2}])
    monkeypatch.setattr(nw, "get_recent_settled", lambda ts: [])
    rep = nw.build_report()
    assert "资金余额: $12.34" in rep["text"]
    assert "当前持仓 1 笔" in rep["text"]
    assert "spy-up-or-down-on-x" in rep["text"]
    assert "0x204f72f3" in rep["text"]
    assert "新增结算" not in rep["text"]  # 无基准不显示


def test_report_new_settled_shown(monkeypatch, tmp_path):
    """有上次状态（ts>0）时显示新增结算。"""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "ts": 100.0, "balance": 10.0, "positions": [], "wallet_stats": [],
    }), encoding="utf-8")
    monkeypatch.setattr(nw, "STATE_FILE", state)
    monkeypatch.setattr(nw, "get_balance", lambda: 10.0)
    monkeypatch.setattr(nw, "get_positions", lambda: [])
    monkeypatch.setattr(nw, "get_wallet_stats", lambda: [])
    monkeypatch.setattr(nw, "get_recent_settled", lambda ts: [
        {"slug": "old-market", "win": 1, "pnl": 0.5}])
    rep = nw.build_report()
    assert "新增结算 1 笔" in rep["text"]
    assert "old-market" in rep["text"]


def test_report_changes_vs_prev(monkeypatch, tmp_path):
    """对比上次：新开/平仓/资金变化/画像变化。"""
    state = tmp_path / "state.json"
    state.write_text(json.dumps({
        "ts": 1000.0, "balance": 10.0,
        "positions": [{"slug": "old-pos", "window": "copytrade"}],
        "wallet_stats": [{"wallet": "0x204f72f35326", "n": 4,
                          "wins": 2, "pnl": 0.8}],
    }), encoding="utf-8")
    monkeypatch.setattr(nw, "STATE_FILE", state)
    monkeypatch.setattr(nw, "get_balance", lambda: 12.0)
    monkeypatch.setattr(nw, "get_positions", lambda: [
        {"slug": "new-pos", "window": "copytrade", "side": "YES",
         "fill_price": 0.5, "created_at": "2026-08-19 10:00:00"}])
    monkeypatch.setattr(nw, "get_wallet_stats", lambda: [
        {"wallet": "0x204f72f35326", "n": 5, "wins": 3, "pnl": 1.2}])
    monkeypatch.setattr(nw, "get_recent_settled", lambda ts: [])
    rep = nw.build_report()
    text = rep["text"]
    assert "🆕 新开 1 笔" in text
    assert "✅ 平仓 1 笔" in text
    assert "↑+2.00" in text          # 资金 10 → 12
    assert "+0.40" in text           # 钱包 pnl 0.8 → 1.2
    assert rep["ts"] > 1000.0


def test_send_dry_prints(monkeypatch, capsys):
    """--dry 只打印不发送；无 webhook 时报错。"""
    monkeypatch.delenv("FEISHU_WEBHOOK_URL", raising=False)
    assert nw.send({"text": "test"}) is False  # 无 URL
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://example.invalid/hook")
    assert nw.send({"text": "test"}, dry=True) is True
    out = capsys.readouterr().out
    assert "test" in out and "DRY" in out


def test_send_success(monkeypatch):
    """发送成功（mock requests）：200 + code 0。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://x/hook")

    class FakeResp:
        status_code = 200
        text = '{"code":0}'
        def json(self):
            return {"code": 0}
    class FakeReq:
        def __init__(self):
            self.called = None
        def post(self, url, json, proxies, timeout):
            self.called = (url, json)
            return FakeResp()
    fake = FakeReq()
    monkeypatch.setattr("requests.post", fake.post)
    assert nw.send({"text": "hello"}) is True
    assert fake.called[0] == "https://x/hook"
    assert fake.called[1]["msg_type"] == "text"
    assert fake.called[1]["content"]["text"] == "hello"


def test_send_failure(monkeypatch):
    """发送失败返回 False。"""
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://x/hook")

    class FakeResp:
        status_code = 500
        text = "error"
        def json(self):
            return {}
    class FakeReq:
        def post(self, *a, **k):
            return FakeResp()
    monkeypatch.setattr("requests.post", FakeReq().post)
    assert nw.send({"text": "x"}) is False


def test_state_save_roundtrip(tmp_path):
    """状态保存/读取往返。"""
    state = tmp_path / "state.json"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(nw, "STATE_FILE", state)
    try:
        nw.save_state({"ts": 5.0, "balance": 3.0, "positions": [],
                       "wallet_stats": []})
        data = json.loads(state.read_text(encoding="utf-8"))
        assert data["ts"] == 5.0 and data["balance"] == 3.0
    finally:
        monkeypatch.undo()


def test_long_text_truncated(monkeypatch, tmp_path):
    """超长文本截断到 MAX_TEXT。"""
    monkeypatch.setattr(nw, "STATE_FILE", tmp_path / "s.json")
    monkeypatch.setattr(nw, "get_balance", lambda: 1.0)
    monkeypatch.setattr(nw, "get_positions", lambda: [])
    monkeypatch.setattr(nw, "get_wallet_stats", lambda: [])
    monkeypatch.setattr(nw, "get_recent_settled", lambda ts: [
        {"slug": "x" * 200, "win": 1, "pnl": 0.1} for _ in range(50)])
    rep = nw.build_report()
    assert len(rep["text"]) <= nw.MAX_TEXT
