"""AI 引擎测试：特征、模型、训练、LLM、策略。"""
import numpy as np
import pytest

from polytrader.ai.features import extract_features, feature_matrix
from polytrader.ai.llm_scorer import LLMScorer, _parse_probability, blend_probabilities
from polytrader.ai.models import HistGBProbabilityModel, get_default_model
from polytrader.ai.train import build_dataset, extract_label, train_model
from polytrader.models import Market, OrderBook, OrderBookLevel, Outcome, SignalType
from polytrader.strategies.ai_probability import AIProbabilityStrategy


def make_market(slug: str, liq: float = 1000.0, vol: float = 100.0,
                category: str = "crypto", desc: str = "test market details",
                prices: list[str] | None = None, closed: bool = False) -> Market:
    prices = prices or ["0.50", "0.50"]
    return Market(
        condition_id=f"0x{slug}", question=f"Will {slug} happen?", slug=slug,
        category=category, description=desc, liquidity=liq, volume=vol,
        end_date="2026-12-31T00:00:00Z", closed=closed, active=not closed,
        outcomes=[
            Outcome(outcome_id="o1", token_id=f"{slug}-yes", price=prices[0], name="Yes"),
            Outcome(outcome_id="o2", token_id=f"{slug}-no", price=prices[1], name="No"),
        ],
    )


def book_for(token: str, ask: float, bid: float | None = None) -> OrderBook:
    return OrderBook(
        token_id=token,
        bids=[OrderBookLevel(bid if bid is not None else ask - 0.01, 100.0)],
        asks=[OrderBookLevel(ask, 100.0)],
    )


# ---------- 特征工程 ----------

def test_extract_features_shape_and_values():
    m = make_market("m1")
    book = book_for("m1-yes", 0.55)
    hist = [{"t": 1700000000 + i * 60, "p": 0.50 + i * 0.001} for i in range(120)]
    f = extract_features(m, book, hist, categories=["crypto"])
    assert f["liq_log"] > 0
    assert f["vol_log"] > 0
    assert 0 <= f["spread"] < 1
    assert f["depth_log"] > 0
    assert f["cat_crypto"] == 1.0
    assert f["ret_1h"] > 0  # 价格上行


def test_feature_matrix_columns_consistent():
    markets = [make_market(f"m{i}", category=("crypto" if i % 2 == 0 else "politics")) for i in range(4)]
    books = {m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, 0.5) for m in markets}
    X, cols = feature_matrix(markets, books)
    assert X.shape == (4, len(cols))
    assert "liq_log" in cols
    assert "cat_crypto" in cols
    assert "cat_politics" in cols


# ---------- 模型 ----------

def test_histgb_model_trains_and_predicts():
    rng = np.random.default_rng(42)
    X = rng.normal(size=(400, 6))
    y = ((X[:, 0] + X[:, 1]) > 0).astype(int)
    model = HistGBProbabilityModel(max_iter=50)
    model.fit(X, y)
    p = model.predict_proba(X[:10])
    assert p.shape == (10,)
    assert ((p >= 0) & (p <= 1)).all()
    acc = ((model.predict_proba(X) > 0.5).astype(int) == y).mean()
    assert acc > 0.7


def test_get_default_model_fallback():
    model = get_default_model()
    assert model is not None
    assert hasattr(model, "predict_proba")


# ---------- 训练流水线 ----------

def test_extract_label_winner():
    m = make_market("resolved", prices=["1.0", "0.0"], closed=True)
    assert extract_label(m) == 1
    m2 = make_market("resolved2", prices=["0.0", "1.0"], closed=True)
    assert extract_label(m2) == 0
    m3 = make_market("unresolved", prices=["0.5", "0.5"])
    assert extract_label(m3) is None


def test_build_dataset_and_train():
    markets = []
    rng = np.random.default_rng(7)
    for i in range(60):
        winner = "1.0" if i % 2 == 0 else "0.0"
        markets.append(make_market(f"r{i}", prices=[winner, "0.0" if winner == "1.0" else "1.0"],
                                   closed=True, liq=float(rng.uniform(100, 5000))))
    result = build_dataset(markets)
    assert result is not None
    X, y, cols = result
    assert X.shape[0] == 60
    assert y.sum() == 30

    artifact = train_model(X, y, calibrate=False)
    assert "model" in artifact
    p = artifact["model"].predict_proba(X[:5])
    assert ((p >= 0) & (p <= 1)).all()


def test_build_dataset_too_few_samples():
    assert build_dataset([make_market("solo", prices=["1.0", "0.0"], closed=True)]) is None


# ---------- LLM ----------

def test_parse_probability_variants():
    assert _parse_probability('{"probability": 0.62, "reason": "x"}') == pytest.approx(0.62)
    assert _parse_probability("0.73") == pytest.approx(0.73)
    assert _parse_probability("Probability: 0.55 based on...") == pytest.approx(0.55)
    assert _parse_probability("garbage") is None
    assert _parse_probability("") is None
    assert _parse_probability("1.5") == pytest.approx(0.999)  # clamp


def test_blend_probabilities():
    assert blend_probabilities(0.6, None, 0.3) == pytest.approx(0.6)  # LLM 不可用退化
    assert blend_probabilities(0.6, 0.8, 0.25) == pytest.approx(0.65)  # 0.25*0.8+0.75*0.6
    assert blend_probabilities(0.6, 0.8, 0.0) == pytest.approx(0.6)
    assert blend_probabilities(0.6, 0.8, 1.0) == pytest.approx(0.8)


def test_llm_scorer_disabled_without_key():
    scorer = LLMScorer(api_key="")
    assert scorer.enabled is False
    assert scorer.score("Q?") is None


# ---------- AI 策略 ----------

def _trained_strategy(**kwargs):
    rng = np.random.default_rng(3)
    X = rng.normal(size=(300, 8))
    y = ((X[:, 0] * 2 + X[:, 1]) > 0.5).astype(int)
    model = HistGBProbabilityModel(max_iter=60)
    model.fit(X, y)
    return AIProbabilityStrategy(model=model, **kwargs)


def test_ai_strategy_no_signal_without_books():
    s = _trained_strategy()
    assert s.scan([make_market("nobook")]) == []


def test_ai_strategy_liquidity_filter():
    s = _trained_strategy(min_liquidity_usd=10000.0)
    m = make_market("lowliq", liq=100.0)
    books = {m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, 0.5)}
    assert s.scan([m], books) == []


def test_ai_strategy_price_band_filter():
    s = _trained_strategy(min_price=0.10, max_price=0.90)
    m = make_market("extremeprice")
    books = {m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, 0.01)}  # 低于 min_price
    assert s.scan([m], books) == []


def test_ai_strategy_emits_signal_when_model_high_and_ask_low():
    # 构造模型高概率特征（训练分布中 y=1 的特征），市场价格给低 ask
    rng = np.random.default_rng(3)
    X = rng.normal(size=(300, 8))
    y = ((X[:, 0] * 2 + X[:, 1]) > 0.5).astype(int)
    model = HistGBProbabilityModel(max_iter=60)
    model.fit(X, y)
    hi_feat = np.array([2.0, 1.5, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]).reshape(1, -1)
    assert model.predict_proba(hi_feat)[0] > 0.8

    # 直接用模型预测值验证信号流：构造特征字典→mock 模型输出高概率
    class DummyModel:
        def predict_proba(self, row):
            return np.array([0.90])

    s = AIProbabilityStrategy(model=DummyModel(), min_edge=0.05)
    m = make_market("signalmarket")
    books = {m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, 0.60)}
    signals = s.scan([m], books)
    assert len(signals) == 1
    assert signals[0].type == SignalType.AI_PROBABILITY
    assert signals[0].edge == pytest.approx(0.30)


def test_ai_strategy_edge_too_small():
    class DummyModel:
        def predict_proba(self, row):
            return np.array([0.60])

    s = AIProbabilityStrategy(model=DummyModel(), min_edge=0.05)
    m = make_market("noedge")
    books = {m.outcomes[0].token_id: book_for(m.outcomes[0].token_id, 0.60)}
    assert s.scan([m], books) == []
