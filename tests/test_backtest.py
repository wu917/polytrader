"""回测模块测试：时间切分、入场点、交易单、收益率统计。"""
import numpy as np

from polytrader.ai.backtest import BacktestTrade, _entry_point, run_backtest
from polytrader.ai.models import HistGBProbabilityModel
from polytrader.models import Market, Outcome


def make_market(slug: str, end_ts: float, label_yes: bool) -> Market:
    prices = ["1.0", "0.0"] if label_yes else ["0.0", "1.0"]
    return Market(
        condition_id=f"0x{slug}", question=f"Q {slug}", slug=slug,
        end_date=f"{int(end_ts * 1000)}",  # 13 位毫秒时间戳
        liquidity=1000.0, volume=500.0, closed=True, active=False,
        outcomes=[Outcome(outcome_id="o1", token_id=f"{slug}-yes", price=prices[0], name="Yes"),
                  Outcome(outcome_id="o2", token_id=f"{slug}-no", price=prices[1], name="No")],
    )


def make_history(end_ts: float, entry_price: float, final_price: float, hours: int = 48) -> list[dict]:
    """价格序列：前 (hours-24) 小时 ≈ entry_price，最后 24 小时线性走向 final_price。

    模拟"模型能识别入场机会"的形态：YES 市场结算前 24h 价格仍低（0.55），
    随后上涨到 0.95；NO 市场从 0.55 跌到 0.05。
    """
    rows = []
    for i in range(hours):
        t = end_ts - (hours - i) * 3600.0
        if i < hours - 24:
            p = entry_price
        else:
            frac = (i - (hours - 24)) / 24.0
            p = entry_price + (final_price - entry_price) * frac
        rows.append({"t": t, "p": round(max(0.01, p), 4)})
    rows.append({"t": end_ts - 60, "p": final_price})
    return rows


def test_entry_point_picks_price_before_lookback():
    end = 1_700_000_000.0
    hist = make_history(end, entry_price=0.55, final_price=0.95, hours=48)
    # lookback 24h → 取 end-24h 附近的价格（约 0.55，尚未上涨）
    entry = _entry_point(hist, lookback_h=24.0)
    assert entry is not None
    assert end - 24 * 3600 - 3600 <= entry["t"] <= end - 24 * 3600 + 60
    assert 0.5 < entry["p"] < 0.7  # 仍在低位


def test_entry_point_empty_history():
    assert _entry_point([], 24.0) is None


def _synthetic_backtest() -> object:
    """构造可区分的合成数据集：特征与标签强相关，时间切分合法。"""
    rng = np.random.default_rng(11)
    n = 60
    markets = []
    histories = {}
    for i in range(n):
        end_ts = 1_700_000_000.0 + i * 86400.0 * 7  # 每周结算一个，时间递增
        label_yes = (i % 2 == 0)
        m = make_market(f"m{i}", end_ts, label_yes)
        # 特征：label_yes 的市场流动性更高（模型可学习）
        m.liquidity = 8000.0 if label_yes else 500.0
        markets.append(m)
        # 价格：入场点（结算前 24h）都为 0.55 中性位，
        # YES 市场随后涨到 0.95、NO 市场跌到 0.05 → 模型信号有真实价值
        histories[m.condition_id] = make_history(
            end_ts, entry_price=0.55, final_price=0.95 if label_yes else 0.05)
    return markets, histories


def test_run_backtest_produces_trades_and_returns():
    markets, histories = _synthetic_backtest()
    result = run_backtest(
        markets, histories,
        model_factory=lambda: HistGBProbabilityModel(max_iter=50),
        min_edge=0.02, entry_lookback_h=24.0, size_usd=100.0, train_frac=0.6,
    )
    # 时间切分：训练集在前
    assert result.n_trained == 36
    assert result.n_test == 24
    assert len(result.trades) > 0
    assert result.n_trades == len(result.trades)
    assert 0 <= result.win_rate <= 1
    # 合成数据里 YES 市场入场价 < 结算 1.0 → 有盈利
    assert result.total_pnl_usd > 0
    assert result.total_return_pct > 0
    # 交易单字段完整
    t0 = result.trades[0]
    assert isinstance(t0, BacktestTrade)
    assert t0.settle_price in (0.0, 1.0)
    assert t0.entry_price > 0
    assert t0.size_usd == 100.0
    # equity 曲线与 max drawdown
    assert len(result.equity_curve) == result.n_trades
    assert result.max_drawdown_pct >= 0


def test_run_backtest_to_dict_serializable():
    markets, histories = _synthetic_backtest()
    result = run_backtest(
        markets, histories,
        model_factory=lambda: HistGBProbabilityModel(max_iter=50),
        min_edge=0.02, train_frac=0.6,
    )
    d = result.to_dict()
    assert "summary" in d and "trades" in d and "equity_curve" in d
    assert d["summary"]["n_trades"] == len(d["trades"])
    assert all(isinstance(t["pnl_usd"], float) for t in d["trades"])


def test_run_backtest_high_min_edge_reduces_trades():
    markets, histories = _synthetic_backtest()
    r_low = run_backtest(markets, histories,
                         model_factory=lambda: HistGBProbabilityModel(max_iter=50),
                         min_edge=0.02, train_frac=0.6)
    r_high = run_backtest(markets, histories,
                          model_factory=lambda: HistGBProbabilityModel(max_iter=50),
                          min_edge=0.50, train_frac=0.6)
    assert r_high.n_trades <= r_low.n_trades
