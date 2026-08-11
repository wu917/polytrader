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
    """AI 回测：已解决市场的模型质量评估（区分度/校准）。

    注意：使用历史价格做特征 + 结算结果做标签，评估的是模型区分度，
    不是真实可交易回测（真实回测需时间切片滚动，见 README 局限说明）。
    """
    from polytrader.ai.features import feature_matrix
    from polytrader.ai.train import extract_label
    from polytrader.data.data_api import DataApiClient
    from polytrader.data.gamma_client import GammaClient
    from polytrader.data.http_client import HttpClient
    from polytrader.ai.models import get_default_model

    cfg = load_config()
    http = HttpClient(proxy=args.proxy or cfg.effective_proxy())
    gamma = GammaClient(http=http)
    data = DataApiClient(http=http)

    print(f"AI backtest | samples={args.samples}")
    closed: list = []
    offset = 0
    while len(closed) < args.samples:
        batch = gamma.get_markets(limit=100, offset=offset, active=False, closed=True)
        if not batch:
            break
        closed.extend(batch)
        offset += 100
    labeled = [m for m in closed if extract_label(m) is not None]
    labeled = labeled[: args.samples]
    print(f"closed={len(closed)} labeled={len(labeled)}")

    if len(labeled) < 20:
        print("too few labeled markets")
        return 1

    histories = {}
    for i, m in enumerate(labeled[: args.history_limit]):
        try:
            histories[m.condition_id] = data.price_history(m.condition_id, interval="1h")
        except Exception:  # noqa: BLE001
            pass

    X, cols = feature_matrix(labeled, histories=histories)
    y = [extract_label(m) for m in labeled]
    valid = [(i, yy) for i, yy in enumerate(y) if yy is not None]
    idx = [i for i, _ in valid]
    y_arr = [yy for _, yy in valid]
    Xv = X[idx]
    print(f"features X={X.shape} labeled_y={len(y_arr)} yes_rate={sum(y_arr) / len(y_arr):.2f}")

    from sklearn.model_selection import cross_val_score
    import numpy as np
    model_cls = get_default_model()
    model_cls.fit(Xv, np.asarray(y_arr))
    probs = model_cls.predict_proba(Xv)
    preds = (probs > 0.5).astype(int)
    acc = float((preds == np.asarray(y_arr)).mean())
    print(f"in-sample accuracy: {acc:.3f}")

    # 按概率分桶看校准（粗略）
    import collections
    buckets = collections.defaultdict(list)
    for p, yy in zip(probs, y_arr):
        buckets[round(p * 10) / 10].append(yy)
    print("bucket  p_avg   n   y_rate")
    for k in sorted(buckets):
        vals = buckets[k]
        print(f"  {k:.1f}   {sum(vals) / len(vals):.3f}  {len(vals):3d}  {sum(vals) / len(vals):.3f}")
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

    b = sub.add_parser("backtest", help="AI 回测（模型质量评估）")
    b.add_argument("--proxy", default=None)
    b.add_argument("--samples", type=int, default=200)
    b.add_argument("--history-limit", type=int, default=100)

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
