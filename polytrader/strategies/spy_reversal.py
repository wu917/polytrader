"""SPY 大跌次日反转策略（交易策略 skill，可插拔）。

2026-08-19 基于 720 天 SPY 日线回测发现（493 交易日）：
- 昨日跌幅 -0.8%~-1.5% → 次日涨率 66.7%（基线 55.9%，两段 360 天窗口
  分别 65.5%/68.0%，效应稳定）
- 赔率正偏：涨时均幅 +1.17% vs 跌时 -0.76%
- 极端暴跌（>-2%）反转失效（42.9% 涨）→ 默认不出信号（不接飞刀）
- 连跌 ≥3 天强化（连跌 4 天 80% 涨）

输出与 EquityUpdownStrategy 同构的 Signal（extra 字段对齐），
下游 simulate/run_equity_live_loop 下单管道无需改动即可复用。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from polytrader.models import Market, Outcome, Side, Signal, SignalType
from polytrader.strategies.equity_context import EquityContextFetcher

log = logging.getLogger(__name__)


@dataclass
class ReversalParams:
    """双侧信号参数（可在配置中覆盖）。

    跌侧（反转）：昨跌 -0.8%~-1.5% → 次日涨率 66.7%（54 样本，
    两段 360 天窗口 65.5%/68.0%）；极端暴跌 >-2% 反转失效。
    涨侧（动量）：昨涨 >+1.0% → 次日继续涨 62.0%（50 样本，
    窗口稳定 63.0%/63.6%）；连涨 3 天 65.7%。
    """

    # ---- 跌侧（反转）----
    lo: float = -1.5          # 触发下界：昨日跌幅 %（含）
    hi: float = -0.8          # 触发上界：昨日跌幅 %（含）
    extreme: float = -2.0     # 极端暴跌线：低于此不出 YES（反转失效）
    extreme_no: bool = False  # 极端暴跌是否出 NO 信号（样本小，默认关）
    base_p: float = 0.667     # 跌侧触发时 YES 概率估计（回测值）
    streak_bonus: bool = True  # 连跌 ≥3 天强化信号
    streak_p: float = 0.72    # 连跌 ≥3 天时的概率估计

    # ---- 涨侧（动量）----
    up_lo: float = 1.0        # 涨侧触发阈值：昨日涨幅 ≥ %（动量延续）
    up_base_p: float = 0.62   # 涨侧触发时 YES 概率估计（回测值）
    up_streak_bonus: bool = True  # 连涨 ≥3 天强化信号
    up_streak_p: float = 0.657    # 连涨 ≥3 天时的概率估计


class SpyReversalStrategy:
    """条件策略：SPY 大跌次日反转 → 对 spy up-or-down 盘出 YES 信号。"""

    name = "spy_reversal"

    def __init__(self, fetcher: EquityContextFetcher | None = None,
                 params: ReversalParams | None = None):
        self.fetcher = fetcher or EquityContextFetcher()
        self.p = params or ReversalParams()
        self.last_signal_info: dict | None = None  # 最近一次扫描的条件详情
        self.last_evaluations: list = []  # 与 EquityUpdownStrategy 接口对齐

    def _daily_changes(self) -> list[tuple[str, float, float]]:
        """SPY 最近 N 根日 K → [(日期, 收盘, 较前日涨跌幅%)]（正序）。

        bar 字段与 parse_daily_bars 对齐：t=日期、c=收盘（已正序）。
        """
        bars = self.fetcher.fetch_bars("e", "SPY")
        out = []
        for i, b in enumerate(bars):
            if i == 0:
                out.append((b.get("t", ""), float(b["c"]), 0.0))
                continue
            prev = float(bars[i - 1]["c"])
            close = float(b["c"])
            out.append((b.get("t", ""), close,
                        (close - prev) / prev * 100 if prev else 0.0))
        return out

    def evaluate(self) -> dict | None:
        """评估当前条件（跌侧反转 + 涨侧动量）→ 信号详情（无信号 None）。"""
        changes = self._daily_changes()
        if len(changes) < 3:
            return None
        last_date, last_close, last_chg = changes[-1]  # 最近收盘日（"昨日"）
        # 连跌/连涨天数（含昨日）：从昨日向前数连续同向
        down_streak = up_streak = 0
        for _, _, ch in reversed(changes):
            if ch < 0:
                down_streak += 1
            else:
                break
        for _, _, ch in reversed(changes):
            if ch > 0:
                up_streak += 1
            else:
                break
        info = {"date": last_date, "prev_change_pct": round(last_chg, 3),
                "down_streak": down_streak, "up_streak": up_streak,
                "side": None, "p": None, "mode": None}
        _EPS = 1e-9  # 浮点容差：边界值（如恰 -1.5%）判定为含
        if self.p.lo - _EPS <= last_chg <= self.p.hi + _EPS:
            # 跌侧反转
            p = self.p.base_p
            if self.p.streak_bonus and down_streak >= 3:
                p = self.p.streak_p
                info["streak_bonus"] = True
            info.update({"side": "YES", "p": p, "mode": "reversal"})
        elif last_chg < self.p.extreme and self.p.extreme_no:
            info.update({"side": "NO", "p": 1.0 - 0.429,
                         "mode": "extreme_no"})  # 极端暴跌次日涨率 42.9%
        elif last_chg >= self.p.up_lo - _EPS:
            # 涨侧动量
            p = self.p.up_base_p
            if self.p.up_streak_bonus and up_streak >= 3:
                p = self.p.up_streak_p
                info["up_streak_bonus"] = True
            info.update({"side": "YES", "p": p, "mode": "momentum"})
        return info

    def scan(self, markets: list[Market], books: dict | None = None,
             max_workers: int = 1) -> list[Signal]:
        """对 spy up-or-down 盘产出反转信号（其余盘忽略）。

        只对**今日结算盘**出信号：discover 常同时返回今日盘 + 明日盘
        （如 08-19 结算与 08-20 结算），信号条件（昨日涨跌幅）预测的是
        今日涨跌，对明日盘语义错误——按 end_date 过滤（2026-08-19）。
        """
        info = self.evaluate()
        self.last_signal_info = info
        if not info or info["side"] is None:
            return []
        from datetime import datetime
        from zoneinfo import ZoneInfo
        et_today = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d")
        signals = []
        for m in markets:
            if not (m.slug or "").startswith("spy-up-or-down"):
                continue
            # 仅今日结算盘（end_date 的 UTC 日期 == 美东今日）
            end_day = (m.end_date or "")[:10]
            if end_day and end_day != et_today:
                log.debug("spy_reversal skip %s: end_date %s ≠ 今日 %s（明日盘）",
                          m.slug, end_day, et_today)
                continue
            side = info["side"]
            p = info["p"]
            idx = 0 if side == "YES" else 1
            outcome = m.outcomes[idx] if len(m.outcomes) > idx else Outcome(
                outcome_id=f"o{idx}", token_id="", name=side)
            # ref = 市场隐含价（YES 侧 outcome.price）
            try:
                ref = float(m.outcomes[0].price) if m.outcomes and m.outcomes[0].price else 0.5
            except (TypeError, ValueError):
                ref = 0.5
            edge = (p - ref) if side == "YES" else ((1.0 - p) - (1.0 - ref))
            mode = info.get("mode", "?")
            streak_txt = (f"{info['down_streak']}连跌" if mode == "reversal"
                          else f"{info['up_streak']}连涨")
            reason = (f"spy_{mode}: 昨日 SPY {info['prev_change_pct']:+.2f}% "
                      f"({streak_txt}) → {side} p={p:.3f} ref={ref:.3f}")
            signals.append(Signal(
                type=SignalType.AI_PROBABILITY, market=m, outcome=outcome,
                side=Side.BUY, probability=p if side == "YES" else 1.0 - p,
                fair_price=p if side == "YES" else 1.0 - p, edge=edge,
                market_price=ref, reason=reason,
                extra={"llm_p": p, "side": side, "model": "spy_reversal",
                       "llm_reason": reason,
                       "ctx": f"prev_change={info['prev_change_pct']}% "
                              f"mode={mode} {streak_txt}",
                       "regime": ""},
            ))
        return signals
