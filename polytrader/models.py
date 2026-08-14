"""核心数据模型：市场、订单簿、信号、持仓、交易记录。"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class Mode(str, Enum):
    DRY_RUN = "dry-run"
    PAPER = "paper"
    LIVE = "live"


class SignalType(str, Enum):
    ARBITRAGE = "arbitrage"
    AI_PROBABILITY = "ai_probability"
    COPYTRADE = "copytrade"


@dataclass
class Outcome:
    """市场中的单个结果（YES/NO 或分类选项）。"""

    outcome_id: str          # Polymarket outcome token id
    token_id: str            # 链上 token id
    price: str = ""          # Gamma 返回的原始价格字符串（可能为 "0.5"）
    name: str = ""


@dataclass
class Market:
    """Gamma API 的市场元数据 + 实时订单簿。"""

    condition_id: str
    question: str
    slug: str = ""
    category: str = ""
    description: str = ""
    end_date: str = ""
    liquidity: float = 0.0
    volume: float = 0.0
    closed: bool = False
    active: bool = True
    outcomes: list[Outcome] = field(default_factory=list)
    order_book: Optional["OrderBook"] = None

    @property
    def is_binary(self) -> bool:
        return len(self.outcomes) == 2


@dataclass
class OrderBookLevel:
    price: float
    size: float


@dataclass
class OrderBook:
    """某个 outcome token 的订单簿（CLOB 格式：价格=概率%*1000）。"""

    token_id: str
    bids: list[OrderBookLevel] = field(default_factory=list)
    asks: list[OrderBookLevel] = field(default_factory=list)
    timestamp: float = field(default_factory=time.time)
    min_order_size: float = 0.0      # CLOB 市场最小订单规模（份额），book API 返回
    tick_size: float = 0.0           # 价格最小增量

    def best_bid(self) -> Optional[OrderBookLevel]:
        return self.bids[0] if self.bids else None

    def best_ask(self) -> Optional[OrderBookLevel]:
        return self.asks[0] if self.asks else None

    def mid_price(self) -> Optional[float]:
        bb, ba = self.best_bid(), self.best_ask()
        if bb and ba:
            return (bb.price + ba.price) / 2
        return bb.price if bb else (ba.price if ba else None)

    def depth_usd(self, levels: int = 3) -> float:
        """前 N 档总挂单金额（美元）。"""
        return sum(l.price * l.size for l in self.bids[:levels] + self.asks[:levels])


@dataclass
class Signal:
    """策略输出的交易信号。"""

    type: SignalType
    market: Market
    outcome: Outcome
    side: Side
    probability: float        # 模型/策略认为的胜率（0-1）
    fair_price: float         # 策略认为的公允价
    edge: float               # 期望边际 = fair - 市场价（>0 才交易）
    market_price: float       # 当前可成交价
    size_usd: float = 0.0     # 建议金额（风控后）
    reason: str = ""
    timestamp: float = field(default_factory=time.time)
    extra: dict = field(default_factory=dict)


@dataclass
class Position:
    """持仓（按 outcome token 聚合）。"""

    token_id: str
    condition_id: str
    outcome_name: str
    shares: float = 0.0
    avg_cost: float = 0.0     # 每股成本（美元/股）

    @property
    def cost_basis(self) -> float:
        return self.shares * self.avg_cost

    @property
    def current_value(self, price: float) -> float:
        return self.shares * price


@dataclass
class Trade:
    """已执行/模拟执行的成交。"""

    signal: SignalType
    market_slug: str
    condition_id: str
    token_id: str
    side: Side
    price: float
    shares: float
    usd_value: float
    status: str = "filled"    # filled | rejected | timeout | cancelled
    mode: str = "dry-run"
    order_id: str = ""
    timestamp: float = field(default_factory=time.time)
    reason: str = ""

    @property
    def cost(self) -> float:
        return self.usd_value


@dataclass
class Order:
    """CLOB 订单（EIP-712 签名前的结构化数据）。

    对应 Polymarket CLOB 协议的 Order 类型：maker 支付 maker_amount 的
    USDC（6 位小数）换取 taker_amount 份的 outcome token（6 位小数）。
    价格隐含于 maker/taker amount 之比。
    """

    maker: str                 # 下单地址（私钥派生）
    taker: str                 # "0x0000000000000000000000000000000000000000" = 任意对手
    token_id: str              # CLOB assetId（0x + 32 字节）
    maker_amount: int          # 支付 USDC 数量（1e6 精度）
    taker_amount: int          # 期望获得份额（1e6 精度）
    fee_rate_bps: int = 0      # 手续费基点（taker 方）
    nonce: int = 0             # 防重放（CLOB 或本地维护）
    expiration: int = 0        # 过期时间戳（秒）
    signature: str = ""        # EIP-712 签名（0x + 65 字节 r/s/v）
    order_id: str = ""         # 下单后 CLOB 返回
    status: str = "pending"    # pending | live | matched | canceled | expired


@dataclass
class WalletProfile:
    """跟单目标钱包画像。"""

    address: str
    realized_profit_usd: float = 0.0
    unrealized_profit_usd: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    avg_trade_profit_usd: float = 0.0
    roi_pct: float = 0.0
    recent_activity: list[dict] = field(default_factory=list)
    source: str = ""          # leaderboard | data_api | seed
    score: float = 0.0        # 综合评分

    @property
    def qualified(self) -> bool:
        """是否达到跟单门槛（由外部按配置校验，此处只做基本检查）。"""
        return self.total_trades >= 1
