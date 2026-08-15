"""镜像引擎：从合格钱包的新成交生成镜像信号。

流程：拉取钱包画像 → 过滤合格目标（盈利/交易数门槛）→ 轮询目标钱包新交易
→ 对每笔满足条件的 BUY 生成镜像 Signal（去重 by trade id）。
"""
from __future__ import annotations

import logging
import time

from polytrader.copytrade.leaderboard import LeaderboardProvider
from polytrader.data.data_api import DataApiClient
from polytrader.logging_setup import get_logger
from polytrader.models import Market, OrderBook, Outcome, Side, Signal, SignalType, WalletProfile

log = get_logger("copytrade.mirror")


class MirrorEngine:
    def __init__(
        self,
        provider: LeaderboardProvider,
        data_api: DataApiClient,
        min_profit_usd: float = 5000.0,
        min_trades: int = 30,
        lookback_days: int = 90,
        max_slippage: float = 0.05,
        mirror_yes_only: bool = True,
        max_size_usd: float = 200.0,
        require_activity: bool = True,
        max_age_seconds: int = 600,
        slippage_per_min: float = 0.01,
        slippage_cap: float = 0.15,
        wash_filter: bool = True,
        wash_window_s: int = 1800,
    ):
        self.provider = provider
        self.data_api = data_api
        self.min_profit_usd = min_profit_usd
        self.min_trades = min_trades
        self.lookback_days = lookback_days
        self.max_slippage = max_slippage
        self.mirror_yes_only = mirror_yes_only
        self.max_size_usd = max_size_usd
        self.require_activity = require_activity  # 排行榜源无活跃时间，可关
        self.max_age_seconds = max_age_seconds  # 活动超龄不跟（信息已消化）
        self.slippage_per_min = slippage_per_min  # 年龄每多 1 分钟滑点容忍 +x
        self.slippage_cap = slippage_cap  # 动态滑点容忍封顶
        self.wash_filter = wash_filter  # 过滤套利/冲单订单（默认开）
        self.wash_window_s = wash_window_s  # 关联判断时间窗
        self._seen_trade_ids: set[str] = set()  # 已镜像交易去重
        self._target_wallets: list[str] = []
        # 钱包交易历史：wallet -> conditionId -> [(side, outcome_index, ts)]
        self._wallet_hist: dict[str, dict[str, list[tuple[str, int | None, float]]]] = {}

    # ---- 目标选择 ----
    def refresh_targets(self, profiles: list[WalletProfile] | None = None) -> list[str]:
        """更新合格目标钱包列表，返回地址列表。

        门槛说明：
        - realized_profit_usd >= min_profit_usd 恒校验
        - min_trades 仅在画像有交易数时校验（排行榜源 total_trades=0 跳过）
        - require_activity 为 True 时才校验 lookback_days 内活跃
          （排行榜源 recent_activity 为空，调用方应关掉该检查）
        """
        profiles = profiles if profiles is not None else self.provider.fetch_profiles()
        since = time.time() - self.lookback_days * 86400
        qualified: list[str] = []
        for p in profiles:
            if not p.address or p.realized_profit_usd < self.min_profit_usd:
                continue
            if p.total_trades and p.total_trades < self.min_trades:
                continue
            if self.require_activity:
                recent = p.recent_activity or []
                last_ts = 0.0
                for r in recent:
                    try:
                        last_ts = max(last_ts, float(r.get("timestamp", 0)))
                    except (TypeError, ValueError):
                        continue
                if last_ts < since:
                    continue
            qualified.append(p.address)
        self._target_wallets = qualified
        log.info("mirror targets: %d/%d wallets qualified",
                 len(self._target_wallets), len(profiles))
        return self._target_wallets

    # ---- 镜像扫描 ----
    def scan(self, markets: list[Market] | None = None,
             books: dict[str, OrderBook] | None = None) -> list[Signal]:
        """轮询目标钱包的新交易，产出镜像信号。

        markets/books 用于把 token_id 映射回市场元数据与当前 ask 价；
        缺省时仅输出最小信号（reason 注明无市场信息）。
        """
        if not self._target_wallets:
            self.refresh_targets()
        if not self._target_wallets:
            return []

        book_by_token = {tok: b for tok, b in (books or {}).items()}
        market_by_token: dict[str, Market] = {}
        for m in markets or []:
            for o in m.outcomes:
                market_by_token[o.token_id] = m

        signals: list[Signal] = []
        for wallet in self._target_wallets:
            try:
                trades = self.data_api.get_user_trades(wallet, limit=50)
            except Exception as exc:  # noqa: BLE001
                log.warning("user trades failed for %s: %s", wallet, exc)
                continue
            for t in trades:
                trade_id = _trade_id(t)
                if not trade_id or trade_id in self._seen_trade_ids:
                    continue
                self._seen_trade_ids.add(trade_id)
                side = str(t.get("side", "")).upper()
                if side != "BUY":
                    continue
                asset = str(t.get("asset", ""))
                if self.mirror_yes_only and not _is_yes_token(asset, markets or []):
                    continue
                signal = self._build_signal(t, market_by_token.get(asset), book_by_token.get(asset))
                if signal is not None:
                    signals.append(signal)
        return signals

    def _build_signal(self, trade: dict, market: Market | None,
                      book: OrderBook | None) -> Signal | None:
        asset = str(trade.get("asset", ""))
        try:
            exec_price = float(trade.get("price", 0))
        except (TypeError, ValueError):
            exec_price = 0.0

        # 当前可成交价：优先 ask（镜像跟随），无 book 用成交价
        ask_price = book.best_ask().price if book and book.best_ask() else None
        if ask_price is not None and exec_price > 0:
            slippage = ask_price / exec_price - 1.0
            if slippage > self.max_slippage:
                log.info("mirror skip %s: slippage %.1f%% > %.1f%%",
                         asset, slippage * 100, self.max_slippage * 100)
                return None
        fill_price = ask_price if ask_price is not None else exec_price

        outcome = None
        if market:
            outcome = next((o for o in market.outcomes if o.token_id == asset), None)
        return Signal(
            type=SignalType.COPYTRADE,
            market=market if market else Market(condition_id="", question=str(trade.get("title", ""))),
            outcome=outcome if outcome else None,  # type: ignore[arg-type]
            side=Side.BUY,
            probability=1.0, fair_price=fill_price,
            edge=max(fill_price - (exec_price or fill_price), 0.0),
            market_price=fill_price,
            size_usd=min(self.max_size_usd, (exec_price or fill_price) * float(trade.get("size", 0))),
            reason=f"mirror {str(trade.get('proxyWallet', ''))[:10]}... @${fill_price:.3f}",
            extra={"mirror_wallet": str(trade.get("proxyWallet", "")),
                   "mirror_trade_id": _trade_id(trade),
                   "exec_price": exec_price},
        )

    def _arb_markets_in(self, wallet: str, acts: list[dict]) -> set[str]:
        """从一批活动中检测套利/冲单市场（conditionId 集合）。

        同一钱包同一市场（批内或历史窗内）满足任一 → 该市场 BUY 全部过滤：
        1. 出现 SELL（买卖往返/冲单）
        2. 双 BUY 反向侧（outcomeIndex 不同，二元套利锁价）
        """
        if not self.wash_filter:
            return set()
        now = time.time()
        groups: dict[str, list[tuple[str, int | None]]] = {}
        # 历史累积（跨轮记忆，按钱包隔离）
        wallet_hist = self._wallet_hist.setdefault(wallet, {})
        for cid, items in wallet_hist.items():
            for side, idx, ts in items:
                if now - ts < self.wash_window_s:
                    groups.setdefault(cid, []).append((side, idx))
        # 当前批（同样按时间窗过滤：旧活动不参与套利判断）
        for a in acts:
            if str(a.get("type", "")).upper() != "TRADE":
                continue
            cid = str(a.get("conditionId", "") or "")
            if not cid:
                continue
            try:
                a_ts = float(a.get("timestamp", 0) or 0)
            except (TypeError, ValueError):
                a_ts = 0.0
            if a_ts and now - a_ts > self.wash_window_s:
                continue  # 超窗活动只记录不入组
            groups.setdefault(cid, []).append(
                (str(a.get("side", "")).upper(), a.get("outcomeIndex")))
            # 同步累积到历史（跨轮记忆，同时清理过期）
            hist = wallet_hist.setdefault(cid, [])
            hist[:] = [h for h in hist if now - h[2] < self.wash_window_s]
            hist.append((str(a.get("side", "")).upper(),
                         a.get("outcomeIndex"), now))
        arb: set[str] = set()
        for cid, items in groups.items():
            has_sell = any(side == "SELL" for side, _ in items)
            buy_idx = {str(idx) for side, idx in items
                       if side == "BUY" and idx is not None}
            if has_sell or len(buy_idx) > 1:
                arb.add(cid)
        return arb

    # ---- 活动流扫描（官方 /activity 端点，带 transactionHash 可靠去重）----
    def scan_activity(self, books: dict[str, OrderBook] | None = None) -> list[Signal]:
        """按钱包轮询官方活动流，产出镜像信号。

        与 scan() 的差异：
        - 数据源为 data-api /activity（含 transactionHash/title/slug/
          conditionId/outcome/outcomeIndex），无需 markets 参数即可构造信号
        - books 可选：提供 token_id -> OrderBook 时做滑点过滤并用 ask 成交价；
          缺省按目标钱包成交价直接跟随（跟单语义）
        """
        if not self._target_wallets:
            self.refresh_targets()
        if not self._target_wallets:
            return []

        signals: list[Signal] = []
        for wallet in self._target_wallets:
            try:
                acts = self.data_api.get_user_activity(wallet, limit=50)
            except Exception as exc:  # noqa: BLE001
                log.warning("user activity failed for %s: %s", wallet, exc)
                continue
            # 套利/冲单市场预检测：批内+历史（该钱包）存在 SELL 或双 BUY 反向
            # → 该市场全部 BUY 过滤（含先出现的，保证二元套利订单整体不跟）
            arb_markets = self._arb_markets_in(wallet, acts)
            for a in acts:
                if str(a.get("type", "")).upper() != "TRADE":
                    continue
                if str(a.get("side", "")).upper() != "BUY":
                    continue
                trade_id = _trade_id(a)
                if not trade_id or trade_id in self._seen_trade_ids:
                    continue
                # 非 YES 侧永久跳过（outcome 不会变化），入去重集
                if self.mirror_yes_only and not _is_yes_activity(a):
                    self._seen_trade_ids.add(trade_id)
                    continue
                # 超龄活动不跟（轮询+索引延迟下，久远买入的信息已消化，追高无 alpha）
                if not self._age_ok(a, trade_id):
                    continue
                # 二元套利/冲单过滤（同市场买卖往返或双侧买入）
                if str(a.get("conditionId", "") or "") in arb_markets:
                    self._seen_trade_ids.add(trade_id)  # 套利订单永久跳过
                    log.info("mirror skip %s: arb/wash market %s (wallet %s)",
                             trade_id[:24], str(a.get("conditionId", ""))[:16],
                             wallet[:10])
                    continue
                signal = self._build_activity_signal(a, wallet, books)
                if signal is None:
                    # 滑点超限等临时原因：不入去重集，下轮可重试
                    continue
                self._seen_trade_ids.add(trade_id)
                signals.append(signal)
        return signals

    def _age_ok(self, act: dict, trade_id: str) -> bool:
        """活动年龄检查：距今 > max_age_seconds 不跟（信息已消化）。

        无时间戳时视为新活动（不过滤，避免误杀索引延迟的条目）。
        """
        try:
            ts = float(act.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            return True
        if ts <= 0:
            return True
        age = time.time() - ts
        if age > self.max_age_seconds:
            log.info("mirror skip %s: activity age %.0fs > %ds (stale, no alpha)",
                     trade_id[:24], age, self.max_age_seconds)
            return False
        return True

    def _allowed_slippage(self, act: dict) -> float:
        """动态滑点容忍：基础 max_slippage + 活动年龄每多 1 分钟 +slippage_per_min。

        轮询+索引延迟下，发现越晚的市场位移越大——按年龄放宽容忍，
        避免"刚买的 3% 行情"被误判为追高；封顶 slippage_cap。
        """
        try:
            ts = float(act.get("timestamp", 0) or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts <= 0:
            return self.max_slippage
        age = max(0.0, time.time() - ts)
        extra = (age / 60.0) * self.slippage_per_min
        return min(self.max_slippage + extra, self.slippage_cap)

    def _build_activity_signal(self, act: dict, wallet: str,
                               books: dict[str, OrderBook] | None = None) -> Signal | None:
        asset = str(act.get("asset", "") or "")
        if not asset:
            log.warning("mirror: activity without asset (wallet=%s), skipping",
                        wallet[:10])
            return None
        try:
            exec_price = float(act.get("price", 0) or 0)
        except (TypeError, ValueError):
            exec_price = 0.0

        outcome_index = act.get("outcomeIndex")
        outcome_name = str(act.get("outcome", "") or "")
        if self.mirror_yes_only:
            if str(outcome_index) != "0" and outcome_name.upper() != "YES":
                log.info("mirror skip %s: not YES side (outcomeIndex=%s, outcome=%s)",
                         asset[:16], outcome_index, outcome_name)
                return None

        book = (books or {}).get(asset)
        ask_price = book.best_ask().price if book and book.best_ask() else None
        if ask_price is not None and exec_price > 0:
            allowed = self._allowed_slippage(act)
            slippage = ask_price / exec_price - 1.0
            if slippage > allowed:
                log.info("mirror skip %s: slippage %.1f%% > allowed %.1f%% "
                         "(age %.0fs)", asset[:16], slippage * 100,
                         allowed * 100, _activity_age(act))
                return None
        fill_price = ask_price if ask_price is not None else exec_price

        market = Market(condition_id=str(act.get("conditionId", "") or ""),
                        question=str(act.get("title", "") or ""),
                        slug=str(act.get("slug", "") or ""))
        outcome = Outcome(outcome_id=str(act.get("conditionId", "") or ""),
                          token_id=asset,
                          price=str(exec_price) if exec_price else "0",
                          name=outcome_name or ("YES" if str(outcome_index) == "0" else "NO"))
        try:
            shares = float(act.get("size", 0) or 0)
        except (TypeError, ValueError):
            shares = 0.0
        return Signal(
            type=SignalType.COPYTRADE,
            market=market, outcome=outcome,
            side=Side.BUY,
            probability=1.0, fair_price=fill_price,
            edge=max(fill_price - (exec_price or fill_price), 0.0),
            market_price=fill_price,
            size_usd=min(self.max_size_usd, (exec_price or fill_price) * shares),
            reason=f"mirror {wallet[:10]}... @${fill_price:.3f}",
            extra={"mirror_wallet": wallet,
                   "mirror_trade_id": _trade_id(act),
                   "exec_price": exec_price,
                   "outcome_index": outcome_index},
        )


def _trade_id(t: dict) -> str:
    """成交唯一 id：优先交易 hash/order id，否则按内容生成指纹。"""
    for key in ("id", "transactionHash", "hash", "orderHash"):
        if t.get(key):
            return f"{key}:{t[key]}"
    return f"{t.get('proxyWallet')}:{t.get('asset')}:{t.get('timestamp')}:{t.get('price')}:{t.get('size')}"


def _activity_age(act: dict) -> float:
    """活动距今秒数（无时间戳返回 0）。"""
    try:
        ts = float(act.get("timestamp", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, time.time() - ts) if ts > 0 else 0.0


def _is_yes_token(asset: str, markets: list[Market]) -> bool:
    """判断 token 是否为市场的 YES（第一个 outcome）。无法判定时保守拒绝。"""
    for m in markets:
        if m.outcomes and m.outcomes[0].token_id == asset:
            return True
    log.warning("mirror: token %s not found in known markets, skipping (unknown token)",
                asset[:16])
    return False


def _is_yes_activity(act: dict) -> bool:
    """activity 事件是否为 YES 侧（outcomeIndex=0 或 outcome=YES）。

    官方 /activity 事件带 outcomeIndex（0=YES/1=NO）与 outcome 名称。
    """
    try:
        idx = act.get("outcomeIndex")
        if idx is not None and str(idx) == "0":
            return True
    except (TypeError, ValueError):
        pass
    return str(act.get("outcome", "") or "").upper() == "YES"
