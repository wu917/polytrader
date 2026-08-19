"""多账户配置化 + equity 调度器深度测试（边界/故障/集成链路）。"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from polytrader import accounts as acc_mod  # noqa: E402
from scripts import run_equity_scheduler as sched  # noqa: E402
from scripts.simulate_equity_updown import build_db_rec  # noqa: E402


# ---------- accounts：文件/解析故障 ----------

def test_load_accounts_broken_yaml(tmp_path):
    """YAML 损坏 → 返回空 dict，不抛异常（不阻塞启动）。"""
    f = tmp_path / "accounts.yaml"
    f.write_text("accounts:\n  default: [unclosed", encoding="utf-8")
    assert acc_mod.load_accounts(f) == {}


def test_load_accounts_missing_file(tmp_path):
    assert acc_mod.load_accounts(tmp_path / "nope.yaml") == {}


def test_load_accounts_skips_non_dict_entries(tmp_path):
    """非 dict 账户条目跳过（列表/字符串不炸）。"""
    f = tmp_path / "accounts.yaml"
    f.write_text("""
accounts:
  default:
    deposit_wallet: "0xAA"
  broken: [1, 2, 3]
  also_broken: "str"
""", encoding="utf-8")
    accs = acc_mod.load_accounts(f)
    assert set(accs) == {"default"}


def test_private_key_env_priority_chain(tmp_path, monkeypatch):
    """私钥优先级：env 引用 > 明文 > 全局 env。"""
    monkeypatch.setenv("TEST_PK", "0xENVKEY")
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "0xGLOBALKEY")
    f = tmp_path / "accounts.yaml"
    f.write_text("""
accounts:
  a:
    private_key_env: TEST_PK
    private_key: "0xPLAIN"
    deposit_wallet: "0xD1"
  b:
    private_key: "0xPLAINB"
    deposit_wallet: "0xD2"
  c:
    deposit_wallet: "0xD3"
""", encoding="utf-8")
    accs = acc_mod.load_accounts(f)
    assert accs["a"].private_key == "0xENVKEY"      # env 引用优先
    assert accs["b"].private_key == "0xPLAINB"      # 明文兜底
    assert accs["c"].private_key == "0xGLOBALKEY"   # 全局 env 兜底


def test_deposit_wallet_env_fallback(tmp_path, monkeypatch):
    """deposit 缺省回退 env POLYMARKET_DEPOSIT_WALLET。"""
    monkeypatch.setenv("POLYMARKET_DEPOSIT_WALLET", "0xEnvDep")
    f = tmp_path / "accounts.yaml"
    f.write_text("""
accounts:
  default:
    private_key: "0xPK"
""", encoding="utf-8")
    a = acc_mod.get_account("default", path=f)
    assert a.deposit_wallet == "0xEnvDep"
    assert a.funder == ""


def test_account_eoa_derived_from_pk():
    """EOA 从私钥正确推导；无/坏私钥返回空串不抛。"""
    from eth_account import Account as EthAccount
    pk = EthAccount.create().key.hex()
    a = acc_mod.Account(name="t", private_key=pk)
    assert a.eoa == EthAccount.from_key(pk).address
    assert acc_mod.Account(name="t2", private_key="").eoa == ""
    assert acc_mod.Account(name="t3", private_key="not-a-key").eoa == ""


# ---------- scheduler：配置与命令构造 ----------

def test_load_equity_config_partial_override(tmp_path):
    """部分配置覆盖：只给 schedule，其余回退默认。"""
    f = tmp_path / "equity.yaml"
    f.write_text("""
schedule:
  runs: ["10:00"]
symbols: ["nvda"]
""", encoding="utf-8")
    cfg = sched.load_equity_config(f)
    assert cfg["schedule"]["runs"] == ["10:00"]
    assert cfg["schedule"]["timezone"] == "America/New_York"  # 默认
    assert cfg["symbols"] == ["nvda"]
    assert cfg["live"] is False
    assert cfg["per_run"] == 3


def test_load_equity_config_broken_yaml(tmp_path):
    """坏 YAML → 全默认不抛。"""
    f = tmp_path / "equity.yaml"
    f.write_text("schedule: [unclosed", encoding="utf-8")
    cfg = sched.load_equity_config(f)
    assert cfg["schedule"]["runs"] == ["09:30", "12:00", "14:30"]


def test_build_cmd_no_symbols_omits_flag():
    """symbols 空 → 不带 --symbols（discover 全量）。"""
    cfg = sched.load_equity_config()
    cfg["symbols"] = []
    cmd = sched.build_cmd(cfg)
    assert "--symbols" not in cmd


def test_build_cmd_simulate_account_and_log():
    """simulate 命令：account/日志/金额全参数正确。"""
    cfg = sched.load_equity_config()
    cfg.update({"account": "my_acct", "size": 3.5, "min_edge": 0.08,
                "min_liquidity": 500, "max_markets": 5})
    cmd = sched.build_cmd(cfg)
    s = " ".join(cmd)
    assert "simulate_equity_updown.py" in s
    assert "run_equity_live_loop.py" not in s
    assert "--account my_acct" in s
    assert "--size 3.5" in s
    assert "--min-edge 0.08" in s
    assert "--min-liquidity 500.0" in s
    assert "--max-markets 5" in s
    assert "--log" in s and "equity_scheduler_run.log" in s


# ---------- scheduler：调度循环去重（mock trigger） ----------

def _fake_now_str(hhmm: str):
    """构造 strftime 按格式返回的假时刻对象。"""
    return type("D", (), {
        "strftime": lambda self, fmt: (hhmm if fmt == "%H:%M"
                                       else f"2099-01-01 {hhmm}")})()


def test_loop_fires_once_per_slot(monkeypatch):
    """同一时刻只触发一次（fired 去重）；非触发时刻不触发。"""
    fired: list[str] = []

    def fake_trigger(cfg, dry=False):
        fired.append("x")

    monkeypatch.setattr(sched, "trigger", fake_trigger)
    cfg = sched.load_equity_config()
    cfg["schedule"]["runs"] = ["99:99"]  # 永不命中的时刻
    monkeypatch.setattr(sched.time, "sleep", lambda _: None)
    monkeypatch.setattr(sched, "_et_now", lambda tz: _fake_now_str("08:00"))
    sched.loop(cfg, rounds=5)
    assert fired == []  # 非触发时刻不触发


def test_loop_dedup_same_minute(monkeypatch):
    """同一分钟内多轮只触发一次。"""
    fired: list[str] = []

    def fake_trigger(cfg, dry=False):
        fired.append("x")

    monkeypatch.setattr(sched, "trigger", fake_trigger)
    monkeypatch.setattr(sched.time, "sleep", lambda _: None)
    cfg = sched.load_equity_config()
    cfg["schedule"]["runs"] = ["08:00"]
    monkeypatch.setattr(sched, "_et_now", lambda tz: _fake_now_str("08:00"))
    sched.loop(cfg, rounds=5)
    assert len(fired) == 1  # 5 轮同刻只触发 1 次


# ---------- PID 管理 ----------

def test_pid_lifecycle(tmp_path, monkeypatch):
    """写/读 PID 往返；alive 检测。"""
    import os
    pid_file = tmp_path / "pid.txt"
    monkeypatch.setattr(sched, "PID_FILE", pid_file)
    sched._write_pid(12345)
    assert sched._read_pid() == 12345
    assert sched._alive(os.getpid()) is True     # 当前进程存活
    # 不存在的 pid（负数不会分配）→ False
    assert sched._alive(-99999) is False


# ---------- build_db_rec account 透传（集成链路） ----------

def test_build_db_rec_account_passthrough():
    """build_db_rec 透传 account（simulate/equity live 入库链路）。"""
    t = {"trade_id": "t1", "slug": "nvda-up-or-down-on-x", "coin": "nvda",
         "window": "daily", "side": "YES", "entry_price": 0.5,
         "size_usd": 1.0, "account": "equity_acct"}
    rec = build_db_rec(t, mode="simulate")
    assert rec["account"] == "equity_acct"
    # 不带 account 不写该键（旧行为兼容）
    rec2 = build_db_rec({k: v for k, v in t.items() if k != "account"},
                        mode="simulate")
    assert "account" not in rec2


def test_insert_pending_with_account_roundtrip():
    """account 列真实入库往返（插入→查询→清理）。"""
    from polytrader import db as pdb
    pdb.ensure_schema()
    conn = pdb.connect()
    trade_id = "accttest1"
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_trades WHERE trade_id=%s", (trade_id,))
        conn.commit()
        pdb.insert_pending([{
            "trade_id": trade_id, "slug": "test-acct-slug", "coin": "t",
            "window": "daily", "side": "YES", "entry_price": 0.5,
            "size_usd": 1.0, "account": "equity_acct", "mode": "simulate",
        }])
        with conn.cursor() as cur:
            cur.execute("SELECT account FROM pending_trades WHERE trade_id=%s",
                        (trade_id,))
            assert cur.fetchone()["account"] == "equity_acct"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_trades WHERE trade_id=%s", (trade_id,))
        conn.commit()
        conn.close()


# ---------- discover symbols 过滤（mock 网络） ----------

def test_discover_symbols_whitelist_filters(monkeypatch):
    """discover 白名单只查询/返回白名单前缀盘口（mock HTTP）。"""
    from scripts import scan_equity_updown as seu
    calls: list[str] = []

    class FakeHttp:
        def get_json(self, url, **kw):
            calls.append(url)
            # public-search 返回两个市场
            if "public-search" in url:
                return [{"markets": [{"slug": "nvda-up-or-down-on-x",
                                      "closed": False, "endDate": "2099-01-01T00:00:00Z"}]}]
            # slug 直查返回对应市场
            if "nvda-up-or-down" in url:
                return {"slug": "nvda-up-or-down-on-x", "closed": False,
                        "endDate": "2099-01-01T00:00:00Z"}
            if "spy-up-or-down" in url:
                return {"slug": "spy-up-or-down-on-x", "closed": False,
                        "endDate": "2099-01-01T00:00:00Z"}
            return None

    mkts = seu.discover_daily_updown(FakeHttp(), symbols=["nvda"])
    slugs = [m["slug"] for m in mkts]
    assert all(s.split("-")[0] in ("nvda",) for s in slugs)
    # 白名单外前缀（spy）不被查询——public-search 调用只含 nvda
    search_calls = [c for c in calls if "public-search" in c]
    assert len(search_calls) == 1 and "nvda" in search_calls[0]
    assert not any("spy" in c for c in search_calls)
