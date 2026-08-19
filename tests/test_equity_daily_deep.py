"""日级涨跌链路深度测试：spy_reversal 策略 + equity live/simulate + scheduler。

覆盖 2026-08-19~20 多轮对话中发现的问题点回归：
- evaluate 取错元素 / t-c 字段 / streak 含昨日 / 双侧信号 / 今日盘过滤
- run_equity_live_loop 函数内重复 import（UnboundLocalError）
- build_db_rec account 透传 / scheduler 触发匹配 / 配置隔离
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from polytrader.models import Market, Outcome  # noqa: E402
from polytrader.strategies.spy_reversal import (  # noqa: E402
    ReversalParams, SpyReversalStrategy)


ET = ZoneInfo("America/New_York")


def mk_fetcher(closes: list[float]):
    class F:
        def fetch_bars(self, kind, symbol):
            assert symbol == "SPY"
            return [{"t": f"d{i}", "c": c} for i, c in enumerate(closes)]
    return F()


def mk_market(end_date: str | None = None, yes_price: str = "0.5",
              slug: str = "spy-up-or-down-on-x"):
    outcomes = [Outcome(outcome_id="o0", token_id="t0", price=yes_price),
                Outcome(outcome_id="o1", token_id="t1", price="0.5")]
    return Market(condition_id="c1", question="SPY up or down?", slug=slug,
                  end_date=end_date or "", outcomes=outcomes)


# ---------- 阈值边界 ----------

@pytest.mark.parametrize("chg,expect_mode,expect_p", [
    (-1.5, "reversal", 0.667),   # 下界含
    (-0.8, "reversal", 0.667),   # 上界含
    (-1.0, "reversal", 0.667),
    (-0.79, None, None),         # 区间外（不足）
    (-1.51, None, None),         # 区间外（过深但未极端）
    (1.0, "momentum", 0.62),     # 涨侧含边界
    (0.99, None, None),
    (2.5, "momentum", 0.62),     # 大涨无上限：仍 momentum（样本稀少段）
])
def test_threshold_boundaries(chg, expect_mode, expect_p):
    """阈值边界精确判定（含边界值）。"""
    # 三根：前一日构造目标涨跌幅（最后一根为"昨日"）
    closes = [100.0, 101.0, 101.0 * (1 + chg / 100)]
    strat = SpyReversalStrategy(fetcher=mk_fetcher(closes))
    info = strat.evaluate()
    if expect_mode is None:
        assert info is None or info["side"] is None
    else:
        assert info["mode"] == expect_mode
        assert abs(info["p"] - expect_p) < 1e-6


def test_extreme_crash_default_no_signal():
    """极端暴跌 -2.5%：默认无信号（不接飞刀）；extreme_no 开启出 NO。"""
    closes = [100.0, 102.6, 100.03]  # 最后一日 -2.5%
    strat = SpyReversalStrategy(fetcher=mk_fetcher(closes))
    assert strat.evaluate()["side"] is None
    strat2 = SpyReversalStrategy(
        fetcher=mk_fetcher(closes),
        params=ReversalParams(extreme_no=True))
    info = strat2.evaluate()
    assert info["side"] == "NO"
    assert abs(info["p"] - (1.0 - 0.429)) < 1e-6


# ---------- 数据异常 ----------

def test_insufficient_bars():
    """少于 3 根 → None（无信号不崩）。"""
    assert SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.0])).evaluate() is None


def test_empty_bars():
    """空数据 → None。"""
    assert SpyReversalStrategy(fetcher=mk_fetcher([])).evaluate() is None


def test_flat_series():
    """连续平盘（chg=0）→ 无信号（不属于连跌/连涨）。"""
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 100.0, 100.0]))
    info = strat.evaluate()
    assert info is None or info["side"] is None


# ---------- streak 计算 ----------

def test_streak_alternating_not_counted():
    """涨跌交替：连跌数只数连续段（交替=1）。"""
    # d1=100 d2=101(+1%) d3=99.5(-1.49%) → 昨日-1.49% 在区间，连跌=1（仅昨日）
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.0, 99.5]))
    info = strat.evaluate()
    assert info["mode"] == "reversal"
    assert info["down_streak"] == 1
    assert info["p"] == 0.667  # 连跌不足 3 无 bonus


def test_streak_flat_day_breaks():
    """平盘打断连跌计数。"""
    # 100→99(-1%)→99(0%)→98.1(-1%)：昨日-1%，连跌=1（前日平盘打断）
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 99.0, 99.0, 98.1]))
    assert strat.evaluate()["down_streak"] == 1


# ---------- 今日盘过滤 ----------

def test_scan_filters_tomorrow_market():
    """明日盘被过滤（end_date ≠ 美东今日）。"""
    today = datetime.now(ET).strftime("%Y-%m-%d")
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    sigs = strat.scan([mk_market(f"{today}T20:00:00Z"),
                       mk_market("2099-12-31T20:00:00Z")])
    assert len(sigs) == 1
    assert sigs[0].market.end_date.startswith(today)


def test_scan_empty_end_date_passes():
    """end_date 缺失的盘放行（历史兼容，discover 保证有值）。"""
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    sigs = strat.scan([mk_market(end_date="")])
    assert len(sigs) == 1


def test_scan_non_spy_slug_ignored():
    """qqq/nvda 等其他标的盘不产信号。"""
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    assert strat.scan([mk_market(slug="qqq-up-or-down-on-x"),
                       mk_market(slug="nvda-up-or-down-on-x")]) == []


# ---------- ref 价解析与 edge ----------

def test_ref_price_invalid_falls_back():
    """outcome.price 非法 → ref 回退 0.5 不崩。"""
    m = mk_market(yes_price="abc")
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    sigs = strat.scan([m])
    assert len(sigs) == 1
    assert sigs[0].market_price == 0.5


def test_edge_yes_side():
    """YES 侧 edge = p - ref。"""
    m = mk_market(yes_price="0.40")
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    s = strat.scan([m])[0]
    assert abs(s.edge - (0.667 - 0.40)) < 1e-6
    assert s.extra["side"] == "YES"
    assert s.extra["model"] == "spy_reversal"


def test_no_market_outcomes_fallback():
    """盘无 outcomes（异常盘）→ 用空 Outcome 兜底，ref=0.5。"""
    m = Market(condition_id="c", question="x", slug="spy-up-or-down-on-x",
               end_date="")
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    sigs = strat.scan([m])
    assert len(sigs) == 1  # 不因缺 outcomes 崩溃


# ---------- run_equity_live_loop 静态回归 ----------

def test_live_loop_no_shadow_import():
    """回归：函数内不得重复 import HttpClient（曾致 UnboundLocalError）。"""
    src = Path("scripts/run_equity_live_loop.py").read_text(encoding="utf-8")
    # 顶部 import 之后不得再出现函数级 from ... import HttpClient
    lines = src.splitlines()
    top_imports = [i for i, l in enumerate(lines)
                   if "from polytrader.data.http_client import HttpClient" in l]
    assert len(top_imports) == 1, "HttpClient 应只在顶部 import 一次"


def test_live_loop_strategy_choices():
    """--strategy 参数支持 equity / spy_reversal。"""
    src = Path("scripts/run_equity_live_loop.py").read_text(encoding="utf-8")
    assert 'choices=["equity", "spy_reversal"]' in src
    assert "SpyReversalStrategy" in src


def test_live_loop_spy_reversal_skips_llm_gate():
    """spy_reversal 分支允许无 LLM（scorer.enabled 检查带豁免）。"""
    src = Path("scripts/run_equity_live_loop.py").read_text(encoding="utf-8")
    assert 'args.strategy != "spy_reversal"' in src


# ---------- 生产配置解析（当前实盘配置） ----------

def test_production_equity_yaml_parsed():
    """生产 config/equity.yaml 正确解析为实盘 spy_reversal 配置。"""
    from scripts.run_equity_scheduler import load_equity_config, DEFAULT_CONFIG
    cfg = load_equity_config(DEFAULT_CONFIG)
    assert cfg["live"] is True
    assert cfg["strategy"] == "spy_reversal"
    assert cfg["symbols"] == ["spy"]
    assert cfg["account"] == "default"
    assert cfg["size"] == 1.0
    # 触发时刻默认三档
    assert len(cfg["schedule"]["runs"]) == 3


def test_build_cmd_production_live():
    """生产配置构造的命令：live 脚本 + spy_reversal + spy 白名单。"""
    from scripts.run_equity_scheduler import load_equity_config, build_cmd, DEFAULT_CONFIG
    cmd = build_cmd(load_equity_config(DEFAULT_CONFIG))
    s = " ".join(cmd)
    assert "run_equity_live_loop.py" in s
    assert "--strategy spy_reversal" in s
    assert "--symbols spy" in s
    assert "--size 1.0" in s
    assert "simulate" not in s


# ---------- scheduler 跨日触发 ----------

def test_loop_next_day_fires_again(monkeypatch):
    """同刻去重只限当日：次日后同刻重新可触发。"""
    import scripts.run_equity_scheduler as sched
    fired: list[str] = []
    monkeypatch.setattr(sched, "trigger", lambda cfg, dry=False: fired.append("x"))
    monkeypatch.setattr(sched.time, "sleep", lambda _: None)
    cfg = sched.load_equity_config(Path("missing.yaml"))
    cfg["schedule"]["runs"] = ["08:00"]

    round_times = ["2026-01-01 08:00", "2026-01-01 08:00", "2026-01-02 08:00"]
    counter = {"i": 0}

    def fake_now(tz):
        t = round_times[min(counter["i"], len(round_times) - 1)]
        counter["i"] += 1
        return type("D", (), {
            "strftime": lambda self, fmt, _t=t: (
                _t.split(" ")[1] if fmt == "%H:%M" else _t)})()
    monkeypatch.setattr(sched, "_et_now", fake_now)
    sched.loop(cfg, rounds=3)
    assert len(fired) == 2  # 第1天1次 + 第2天1次（跨日重置）


# ---------- discover 白名单 ----------

def test_discover_symbols_case_insensitive(monkeypatch):
    """symbols 大小写不敏感（NVDA/Spy 均可命中）。"""
    from scripts import scan_equity_updown as seu
    calls: list[str] = []

    class FakeHttp:
        def get_json(self, url, **kw):
            calls.append(url)
            if "public-search" in url:
                return [{"markets": [
                    {"slug": "nvda-up-or-down-on-x", "closed": False,
                     "endDate": "2099-01-01T00:00:00Z"}]}]
            return None
    seu.discover_daily_updown(FakeHttp(), symbols=["NVDA", "Spy"])
    searches = [c for c in calls if "public-search" in c]
    assert len(searches) == 2  # nvda + spy 都被查询（大小写归一）


# ---------- 入库链路（spy_reversal → build_db_rec → insert） ----------

def test_spy_reversal_rec_to_db_roundtrip():
    """信号 → build_db_rec(mode=live) → insert_pending 全字段入库。"""
    from scripts.simulate_equity_updown import build_db_rec
    from polytrader import db as pdb
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    s = strat.scan([mk_market()])[0]
    rec = build_db_rec({
        "trade_id": "sprtest1", "slug": s.market.slug, "coin": "spy",
        "window": "daily", "side": s.extra["side"],
        "entry_price": float(s.market_price), "size_usd": 1.0,
        "llm_p": s.extra["llm_p"], "ref": float(s.market_price),
        "edge": float(s.edge), "llm_reason": s.reason,
        "model": s.extra["model"], "order_id": "0xord1",
        "order_status": "matched", "fill_price": 0.48, "fill_tx": "0xtx1",
        "account": "default",
    }, mode="live")
    assert rec["account"] == "default"
    assert rec["mode"] == "live"
    assert rec["order_id"] == "0xord1"
    # 真实入库往返
    pdb.ensure_schema()
    conn = pdb.connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_trades WHERE trade_id=%s", ("sprtest1",))
        conn.commit()
        pdb.insert_pending([rec])
        with conn.cursor() as cur:
            cur.execute("""SELECT account, `window`, mode, llm_p, llm_model
                           FROM pending_trades WHERE trade_id=%s""", ("sprtest1",))
            r = cur.fetchone()
            assert r["account"] == "default"
            assert r["window"] == "daily"
            assert r["mode"] == "live"
            assert abs(float(r["llm_p"]) - 0.667) < 1e-6
            assert r["llm_model"] == "spy_reversal"
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM pending_trades WHERE trade_id=%s", ("sprtest1",))
        conn.commit()
        conn.close()


# ---------- 定时语义（时区） ----------

def test_schedule_runs_match_et_only():
    """触发匹配用美东 HH:MM（回归：曾用完整日期导致永不触发）。"""
    import scripts.run_equity_scheduler as sched
    fired: list[str] = []
    orig = sched.trigger
    monkey = pytest.MonkeyPatch()
    monkey.setattr(sched, "trigger", lambda cfg, dry=False: fired.append("x"))
    monkey.setattr(sched.time, "sleep", lambda _: None)
    try:
        cfg = sched.load_equity_config(Path("missing.yaml"))
        cfg["schedule"]["runs"] = ["09:30"]
        # 美东时刻 09:30 触发（日期任意）
        monkey.setattr(sched, "_et_now", lambda tz: type("D", (), {
            "strftime": lambda self, fmt: ("09:30" if fmt == "%H:%M"
                                           else "2026-08-20 09:30")})())
        sched.loop(cfg, rounds=2)
        assert len(fired) == 1
    finally:
        monkey.undo()


# ---------- 参数自定义 ----------

def test_custom_params_overrides():
    """ReversalParams 自定义阈值生效（lo/hi/up_lo/概率）。"""
    closes = [100.0, 101.0, 100.0]  # 昨日 -0.99%——默认区间外
    p = ReversalParams(lo=-1.0, hi=-0.5, base_p=0.7)  # 自定义区间覆盖
    strat = SpyReversalStrategy(fetcher=mk_fetcher(closes), params=p)
    info = strat.evaluate()
    assert info["mode"] == "reversal"
    assert abs(info["p"] - 0.7) < 1e-9


def test_disable_streak_bonus():
    """streak_bonus=False：连跌 5 天也不提概率。"""
    closes = [100.0, 99.0, 98.0, 97.0, 96.5, 95.6]  # 连跌 5 天，昨日 -0.93%
    p = ReversalParams(streak_bonus=False)
    strat = SpyReversalStrategy(fetcher=mk_fetcher(closes), params=p)
    assert abs(strat.evaluate()["p"] - 0.667) < 1e-9


def test_up_streak_exactly_three():
    """连涨恰好 3 天 → bonus 触发（== 边界含）。"""
    closes = [100.0, 100.5, 101.0, 102.05]  # 连涨3天，昨日 +1.04%
    strat = SpyReversalStrategy(fetcher=mk_fetcher(closes))
    info = strat.evaluate()
    assert info["up_streak"] == 3
    assert abs(info["p"] - 0.657) < 1e-9


def test_extreme_exactly_negative_two():
    """昨跌恰 -2.0%：extreme 线语义（严格 < -2.0 才算极端）。
    -2.0 本身：既不在反转区间（<-0.8），也不算极端（不 < -2.0）
    → 默认无信号；extreme_no=True 同样无信号（-2.0 不是极端）。
    -2.01 才算极端：extreme_no=True 出 NO。
    """
    closes = [100.0, 100.0, 98.0]  # 昨日 (98-100)/100 = -2.0% 精确
    assert SpyReversalStrategy(fetcher=mk_fetcher(closes)).evaluate()["side"] is None
    s2 = SpyReversalStrategy(fetcher=mk_fetcher(closes),
                             params=ReversalParams(extreme_no=True))
    assert s2.evaluate()["side"] is None  # -2.0 不 < -2.0
    # -2.01% 才触发 NO
    s3 = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 100.0, 97.99]),
                             params=ReversalParams(extreme_no=True))
    assert s3.evaluate()["side"] == "NO"


# ---------- last_signal_info 结构 ----------

def test_last_signal_info_updated_on_no_signal():
    """无信号日 last_signal_info 仍更新（含条件详情，供诊断）。"""
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 100.3, 100.5]))
    strat.scan([mk_market()])
    info = strat.last_signal_info
    assert info is not None
    assert info["side"] is None
    assert "prev_change_pct" in info
    assert "down_streak" in info and "up_streak" in info


# ---------- Signal 字段对齐 ----------

def test_signal_fields_consistency():
    """Signal 字段一致性：probability/fair_price/edge/type。"""
    m = mk_market(yes_price="0.45")
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    s = strat.scan([m])[0]
    assert s.side == s.side.BUY
    assert s.type == s.type.AI_PROBABILITY
    assert abs(s.probability - 0.667) < 1e-9          # YES 侧 = p
    assert abs(s.fair_price - 0.667) < 1e-9
    assert abs(s.market_price - 0.45) < 1e-9
    assert "spy_reversal" in s.reason


# ---------- simulate --strategy 分支（mock 不下单） ----------

def test_simulate_spy_reversal_branch(monkeypatch):
    """simulate 源码包含 spy_reversal 分支（函数内 import，模块级不可 mock）。"""
    src = Path("scripts/simulate_equity_updown.py").read_text(encoding="utf-8")
    assert 'args.strategy == "spy_reversal"' in src
    assert "SpyReversalStrategy" in src
    assert 'args.strategy != "spy_reversal"' in src  # LLM gate 豁免


# ---------- discover 全链路（mock HTTP） ----------

def test_discover_liquidity_and_dedup(monkeypatch):
    """discover：过期盘过滤 + 同 slug 去重（mock 双源）。"""
    from scripts import scan_equity_updown as seu

    class FakeHttp:
        def get_json(self, url, **kw):
            if "public-search" in url:
                return [{"markets": [
                    {"slug": "spy-up-or-down-on-x", "closed": False,
                     "endDate": "2099-01-01T00:00:00Z"},
                    {"slug": "spy-up-or-down-on-y", "closed": True,
                     "endDate": "2099-01-01T00:00:00Z"},          # closed 滤
                    {"slug": "spy-up-or-down-on-z", "closed": False,
                     "endDate": "2020-01-01T00:00:00Z"},          # 过期滤
                    {"slug": "spy-up-or-down-on-x", "closed": False,
                     "endDate": "2099-01-01T00:00:00Z"},          # 重复去重
                ]}]
            return None  # slug 直查无
    mkts = seu.discover_daily_updown(FakeHttp(), symbols=["spy"])
    assert len(mkts) == 1 and mkts[0]["slug"] == "spy-up-or-down-on-x"


# ---------- 无 outcomes 全链路（scan 兜底已修） ----------

def test_scan_no_outcomes_after_fix():
    """回归：无 outcomes 的盘出信号不崩（Outcome 兜底补全）。"""
    m = Market(condition_id="c", question="q", slug="spy-up-or-down-on-x",
               end_date="")
    strat = SpyReversalStrategy(fetcher=mk_fetcher([100.0, 101.5, 100.49]))
    sigs = strat.scan([m])
    assert len(sigs) == 1
    assert sigs[0].outcome.token_id == ""  # 空 token 兜底
