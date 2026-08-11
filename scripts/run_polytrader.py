"""PolyTrader 主入口。

用法:
  # 离线全链路模拟（合成市场，dry-run，无网络）
  .venv/bin/python scripts/run_polytrader.py --offline

  # paper 模式：真实市场 + 真实订单簿 + 模拟成交
  .venv/bin/python scripts/run_polytrader.py --mode paper [--proxy socks5h://127.0.0.1:7890]

  # AI 回测：已解决市场的模型质量评估
  .venv/bin/python scripts/run_polytrader.py backtest --samples 200 [--proxy ...]
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.config import load_config
from polytrader.logging_setup import get_logger, setup_logging

log = get_logger("main")


def cmd_offline() -> int:
    """离线合成数据全链路：市场 → 套利/AI/跟单 → 风控 → 执行 → 报告。"""
    import numpy as np

    from polytrader.ai.models import HistGBProbabilityModel
    from polytrader.copytrade.leaderboard import SeedProvider
    from polytrader.copytrade.mirror import MirrorEngine
    from polytrader.data.data_api import DataApiClient
    from polytrader.execution.broker import DryRunBroker
    from polytrader.execution.order_manager import OrderManager
    from polytrader.models import Market, OrderBook, OrderBookLevel, Outcome, SignalType, WalletProfile
    from polytrader.risk.risk_manager import RiskManager
    from polytrader.strategies.ai_probability import AIProbabilityStrategy
    from polytrader.strategies.arbitrage import ArbitrageStrategy

    cfg = load_config()
    print("=" * 60)
    print(f"PolyTrader offline dry-run | mode={cfg.mode}")
    print("=" * 60)

    # 1. 合成市场：2 个套利机会 + 若干普通市场
    markets: list[Market] = []
    books: dict[str, OrderBook] = {}
    rng = np.random.default_rng(42)

    # 二元套利机会：YES ask 0.48 + NO ask 0.48 = 0.96
    arb = Market(condition_id="0xarb1", question="Will X happen?", slug="arb1",
                 liquidity=5000, volume=1000, active=True,
                 outcomes=[Outcome(outcome_id="a1", token_id="arb1-yes", price="0.48", name="Yes"),
                           Outcome(outcome_id="a2", token_id="arb1-no", price="0.48", name="No")])
    markets.append(arb)
    books["arb1-yes"] = OrderBook(token_id="arb1-yes",
                                  asks=[OrderBookLevel(0.48, 500)], bids=[OrderBookLevel(0.47, 100)])
    books["arb1-no"] = OrderBook(token_id="arb1-no",
                                 asks=[OrderBookLevel(0.48, 500)], bids=[OrderBookLevel(0.47, 100)])

    # 普通市场（AI 策略用）
    for i in range(8):
        ask = float(rng.uniform(0.05, 0.95))
        m = Market(condition_id=f"0xai{i}", question=f"AI market {i}?", slug=f"ai{i}",
                   category="crypto", liquidity=float(rng.uniform(1000, 10000)),
                   volume=500, active=True,
                   outcomes=[Outcome(outcome_id=f"y{i}", token_id=f"ai{i}-yes", price=str(ask), name="Yes"),
                             Outcome(outcome_id=f"n{i}", token_id=f"ai{i}-no", price=str(round(1 - ask, 3)), name="No")])
        markets.append(m)
        books[f"ai{i}-yes"] = OrderBook(token_id=f"ai{i}-yes", asks=[OrderBookLevel(ask, 200)])
        books[f"ai{i}-no"] = OrderBook(token_id=f"ai{i}-no", asks=[OrderBookLevel(round(1 - ask, 3), 200)])

    # 2. 策略
    strategies = []
    strategies.append(ArbitrageStrategy(min_edge=cfg.arbitrage_min_edge,
                                        max_position_usd=cfg.arbitrage_max_position_usd))

    # AI：训练一个合成模型（随机特征 → 模拟区分度）
    # 注意：特征维度必须与 extract_features 的 FEATURE_COLS（9 列）一致，
    # 且预测时必须传训练时的 columns 顺序
    from polytrader.ai.features import FEATURE_COLS

    X = rng.normal(size=(300, len(FEATURE_COLS)))
    y = ((X[:, 0] + X[:, 1]) > 0.3).astype(int)
    model = HistGBProbabilityModel(max_iter=40)
    model.fit(X, y)
    strategies.append(AIProbabilityStrategy(model=model, columns=FEATURE_COLS,
                                            min_edge=cfg.ai_min_edge,
                                            min_liquidity_usd=cfg.ai_min_liquidity_usd))

    # 跟单：种子合格钱包 → 镜像信号
    profile = WalletProfile(address="0xpro", realized_profit_usd=9000, total_trades=60,
                            win_rate=0.6, recent_activity=[{"timestamp": 2000000000}])
    data_api = DataApiClient()
    engine = MirrorEngine(SeedProvider([profile]), data_api,
                          min_profit_usd=5000, min_trades=30)
    engine.refresh_targets([profile])

    # 3. 扫描 → 汇总信号
    all_signals = []
    for strat in strategies:
        sigs = strat.scan(markets, books)
        all_signals.extend(sigs)
        print(f"[scan] {strat.name}: {len(sigs)} signals")
        for s in sigs:
            print(f"   - {s.type.value:14s} {s.market.slug:12s} "
                  f"p={s.probability:.3f} ask={s.market_price:.3f} edge={s.edge:+.3f}")

    # 4. 风控 + 执行
    risk = RiskManager(mode="dry-run", max_position_usd=cfg.max_position_usd,
                       max_total_exposure_usd=cfg.max_total_exposure_usd,
                       max_daily_loss_usd=cfg.max_daily_loss_usd,
                       cooldown_seconds=cfg.cooldown_seconds)
    om = OrderManager(DryRunBroker(), risk, bankroll_usd=5000.0,
                      kelly_fraction=cfg.kelly_fraction)
    trades = om.execute(all_signals)
    print(f"\n[exec] {len(trades)} trades, filled={sum(1 for t in trades if t.status == 'filled')}")
    for t in trades:
        print(f"   - {t.status:8s} {t.side.value} {t.market_slug:12s} "
              f"{t.usd_value:>8.2f} USD @ {t.price:.3f}")

    # 5. 报告
    print("\n[report]", om.snapshot())
    return 0


def cmd_paper(args) -> int:
    """paper 模式：真实市场扫描 + 模拟成交。"""
    from polytrader.data.clob_client import ClobClient
    from polytrader.data.gamma_client import GammaClient
    from polytrader.data.http_client import HttpClient
    from polytrader.execution.broker import PaperBroker
    from polytrader.execution.order_manager import OrderManager
    from polytrader.risk.risk_manager import RiskManager
    from polytrader.strategies.arbitrage import ArbitrageStrategy

    cfg = load_config()
    http = HttpClient(proxy=args.proxy or cfg.effective_proxy())
    gamma = GammaClient(http=http)
    clob = ClobClient(http=http)

    print(f"PolyTrader paper scan | proxy={args.proxy or 'direct'}")
    markets = gamma.get_markets(limit=args.markets, active=True)
    binary = [m for m in markets if m.is_binary]
    print(f"markets={len(markets)} binary={len(binary)}")

    # 拉订单簿
    books: dict[str, object] = {}
    for m in binary[: args.book_limit]:
        for o in m.outcomes:
            try:
                book = clob.get_book(o.token_id)
                if book:
                    books[o.token_id] = book
            except Exception as exc:  # noqa: BLE001
                log.debug("book fetch failed %s: %s", o.token_id, exc)

    strategy = ArbitrageStrategy(min_edge=cfg.arbitrage_min_edge,
                                 max_position_usd=cfg.arbitrage_max_position_usd)
    signals = strategy.scan(binary, books)
    print(f"arbitrage signals: {len(signals)}")
    for s in signals:
        print(f"   - {s.market.slug}: YES@${s.market_price:.3f} edge={s.edge:+.3f} {s.reason}")

    risk = RiskManager(mode="paper", max_position_usd=cfg.max_position_usd,
                       max_total_exposure_usd=cfg.max_total_exposure_usd,
                       cooldown_seconds=cfg.cooldown_seconds)
    om = OrderManager(PaperBroker(clob, cfg.slippage_tolerance), risk,
                      bankroll_usd=5000.0, kelly_fraction=cfg.kelly_fraction)
    trades = om.execute(signals)
    print(f"\n[exec] trades={len(trades)} filled={sum(1 for t in trades if t.status == 'filled')}")
    print("[report]", om.snapshot())
    return 0


def cmd_backtest(args) -> int:
    """可交易回测：时间切分训练/测试 + 模拟交易 + 收益率统计。

    结果保留到 backtest_results/（trades CSV + report JSON）：
      backtest_results/report_<ts>.json   汇总 + 完整交易单
      backtest_results/trades_<ts>.csv    逐笔交易单（Excel 友好）

    注意（诚实披露）：
    - 单次 walk-forward 切分（训练全部早于测试），非滚动回测
    - 未计滑点/手续费；订单簿深度不可用；入场价用结算前 24h 历史价
    - 收益率仅供参考，不构成盈利保证
    """
    import csv
    import json
    import time as _time

    from polytrader.ai.backtest import run_backtest
    from polytrader.ai.train import extract_label
    from polytrader.data.data_api import DataApiClient
    from polytrader.data.gamma_client import GammaClient
    from polytrader.data.http_client import HttpClient

    cfg = load_config()
    http = HttpClient(proxy=args.proxy or cfg.effective_proxy())
    gamma = GammaClient(http=http)
    data = DataApiClient(http=http)

    print(f"AI backtest | samples={args.samples} min_edge={args.min_edge} "
          f"entry_lookback={args.lookback_h}h train_frac={args.train_frac}")
    from polytrader.ai.backtest import _end_ts

    # 只回测最近 N 个月结算的市场：CLOB /prices-history 保留期有限，
    # 过旧市场无历史数据。默认 3 个月。
    import datetime

    since = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=args.since_months * 30)
    CLOB_EPOCH_DATE = since.strftime("%Y-%m-%d")
    labeled: list = []
    offset = 0
    while len(labeled) < args.samples and offset < args.samples * 4:
        batch = gamma.get_markets(limit=100, offset=offset, active=False, closed=True,
                                  end_date_min=CLOB_EPOCH_DATE)
        if not batch:
            break
        for m in batch:
            if extract_label(m) is not None:
                labeled.append(m)
        offset += 100
    labeled = labeled[: args.samples]
    print(f"closed_fetched={offset} labeled_recent={len(labeled)} (since {CLOB_EPOCH_DATE})")

    if len(labeled) < 40:
        print("too few labeled markets (need >= 40 for a meaningful split)")
        return 1

    # 拉成交历史：CLOB /prices-history 对已解决市场不返回数据，
    # 用 data-api /trades 成交账本构造价格序列（保留完整历史）
    histories = {}
    for i, m in enumerate(labeled):
        try:
            token_id = m.outcomes[0].token_id if m.outcomes else ""
            if not token_id:
                continue
            histories[m.condition_id] = data.market_trade_history(
                m.condition_id, token_id, limit=args.trade_limit)
        except Exception:  # noqa: BLE001
            pass
        if (i + 1) % 50 == 0:
            print(f"  ... {i + 1}/{len(labeled)} histories")
    with_hist = sum(1 for h in histories.values() if h)
    print(f"  markets with trade history: {with_hist}/{len(labeled)}")
    # 只保留有成交历史的市场（长尾无成交市场无法回测）
    labeled = [m for m in labeled if histories.get(m.condition_id)]
    print(f"  labeled with history: {len(labeled)}")

    result = run_backtest(
        labeled, histories,
        min_edge=args.min_edge, entry_lookback_h=args.lookback_h,
        size_usd=args.size_usd, train_frac=args.train_frac,
    )
    s = result.to_dict()["summary"]
    print("\n" + "=" * 60)
    print("BACKTEST RESULT")
    print("=" * 60)
    print(f"  markets        : train={s['n_trained']} test={s['n_test']}")
    print(f"  trades         : {s['n_trades']}")
    print(f"  win rate       : {s['win_rate']:.1%}")
    print(f"  avg edge       : {s['avg_edge']:.3f}")
    print(f"  total return   : {s['total_return_pct']:+.2f}%  "
          f"(${s['total_pnl_usd']:+.2f} on ${s['n_trades'] * args.size_usd} notional)")
    print(f"  max drawdown   : {s['max_drawdown_pct']:.1f}%")
    print("=" * 60)

    # ---- 保留输出 ----
    out_dir = Path("backtest_results")
    out_dir.mkdir(exist_ok=True)
    ts = _time.strftime("%Y%m%d_%H%M%S")
    report_path = out_dir / f"report_{ts}.json"
    trades_path = out_dir / f"trades_{ts}.csv"
    report_path.write_text(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    with open(trades_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["market_slug", "condition_id", "side", "entry_time", "entry_price",
                         "model_p", "edge", "settle_price", "size_usd", "pnl_usd", "pnl_pct"])
        for t in result.trades:
            writer.writerow([t.market_slug, t.condition_id, t.side, t.entry_time,
                             t.entry_price, round(t.model_p, 4), round(t.edge, 4),
                             t.settle_price, t.size_usd, round(t.pnl_usd, 2),
                             round(t.pnl_pct, 4)])
    print(f"\n  saved: {report_path}")
    print(f"  saved: {trades_path}")
    return 0


def main() -> int:
    setup_logging()
    ap = argparse.ArgumentParser(description="PolyTrader")
    sub = ap.add_subparsers(dest="cmd")

    sub.add_parser("offline", help="离线合成数据全链路 dry-run")
    p = sub.add_parser("paper", help="paper 模式真实扫描")
    p.add_argument("--proxy", default=None)
    p.add_argument("--markets", type=int, default=50)
    p.add_argument("--book-limit", type=int, default=10)

    b = sub.add_parser("backtest", help="可交易回测（收益率 + 交易单保留输出）")
    b.add_argument("--proxy", default=None)
    b.add_argument("--samples", type=int, default=300)
    b.add_argument("--history-limit", type=int, default=150)
    b.add_argument("--trade-limit", type=int, default=500, help="每市场拉取的成交条数（回测价格数据源）")
    b.add_argument("--since-months", type=int, default=3, help="只回测最近 N 个月结算的市场")
    b.add_argument("--min-edge", type=float, default=0.03, help="最小期望边际")
    b.add_argument("--lookback-h", type=float, default=24.0, help="入场取结算前 N 小时价格")
    b.add_argument("--size-usd", type=float, default=100.0, help="每笔名义金额")
    b.add_argument("--train-frac", type=float, default=0.7, help="时间切分训练集比例")

    args = ap.parse_args()
    if args.cmd == "offline" or args.cmd is None:
        return cmd_offline()
    if args.cmd == "paper":
        return cmd_paper(args)
    if args.cmd == "backtest":
        return cmd_backtest(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
