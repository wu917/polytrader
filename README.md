# PolyTrader

Polymarket 预测市场自动化交易系统（Python 3.11+）。模块化设计，三大盈利策略 + 完整风控，
支持 dry-run / paper / live 三种执行模式。

> ⚠️ **盈利性声明**：本系统提供行业标准策略与风控框架，但**不保证持续盈利**。
> 预测市场存在对手方风险、滑点、模型失效等风险。请先用 dry-run/paper 模式长期验证，
> 再决定是否投入真实资金。live 模式当前被安全闸拦截（见下文）。

## 快速开始

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 离线全链路模拟（合成市场，无网络）
.venv/bin/python scripts/run_polytrader.py offline

# 运行测试
.venv/bin/python -m pytest tests/
```

中国大陆网络环境需要代理（config.yaml 的 `network.proxy`，如 `socks5h://127.0.0.1:7890`）。

## 架构

```
polytrader/
├── config.py            # 配置：config.yaml + POLY_ 环境变量 + .env 凭证
├── models.py            # Market / OrderBook / Signal / Trade / WalletProfile
├── data/
│   ├── gamma_client.py  # 市场发现（Gamma API，含 JSON 字符串数组兼容）
│   ├── clob_client.py   # 订单簿 REST + WS 实时订阅
│   └── data_api.py      # 历史价格（CLOB /prices-history）+ 成交记录（/trades）
├── strategies/
│   ├── arbitrage.py     # 二元 YES/NO 互补 + 分类市场概率和套利
│   ├── ai_probability.py# 模型概率 vs 市场价格 → 期望边际
│   └── (copytrade)      # 见 copytrade/
├── ai/
│   ├── models.py        # 可插拔：lightgbm（需 libomp）/ sklearn fallback
│   ├── features.py      # 14+ 维特征（元数据/订单簿/价格动量/类别）
│   ├── train.py         # 标签提取（resolved 市场）+ 训练 + isotonic 校准
│   └── llm_scorer.py    # OpenAI 兼容 LLM 概率评分（可插拔，无 key 禁用）
├── copytrade/
│   ├── wallet_analysis.py  # FIFO 盈亏 / 胜率 / 评分
│   ├── leaderboard.py      # 从市场成交聚合钱包（排行榜私有 API 的替代）
│   └── mirror.py           # 目标资格过滤 / 去重 / 滑点容忍 / 镜像信号
├── risk/
│   ├── kelly.py         # 分数 Kelly 仓位
│   └── risk_manager.py  # 敞口 / 日损熔断 / 回撤熔断 / 冷却 / 价格带
└── execution/
    ├── broker.py        # dry-run（模拟）/ paper（真实取价模拟）/ live（安全拒绝）
    └── order_manager.py # Kelly 定仓 + 套利组原子性（组内全成或全弃）
```

## 使用

### 三大策略

| 策略 | 原理 | 关键参数 |
|---|---|---|
| 套利 | YES+NO ask 和 < 1-ε 同时买入；互斥候选概率和 < 1-ε 全买 | `arbitrage.min_edge` |
| AI 概率 | 特征 → 模型 P(YES) vs ask 价，edge ≥ ε 买入 | `ai_probability.min_edge` |
| 跟单 | 聚合市场成交找出盈利钱包 → 镜像其 BUY（去重/滑点过滤） | `copytrade.*` |

### 模式

- **dry-run**：无网络，按信号价模拟成交（测试/演示）
- **paper**：真实拉取市场/订单簿，模拟成交（策略验证）
- **live**：真实下单 —— **当前被安全闸拦截**。需要实现 EIP-712 订单签名
  （`POLYMARKET_PRIVATE_KEY` + API key/secret/passphrase 已支持配置读取，
  签名逻辑待实现，绝不盲目下单）

```bash
# paper 模式真实扫描
.venv/bin/python scripts/run_polytrader.py paper --proxy socks5h://127.0.0.1:7890
```

### AI 模型训练与回测

```bash
# 训练（从已解决市场取标签）
.venv/bin/python scripts/train_ai.py --samples 2000 --proxy socks5h://127.0.0.1:7890

# 模型质量评估（区分度 + 校准分桶）
.venv/bin/python scripts/run_polytrader.py backtest --samples 500 --proxy socks5h://127.0.0.1:7890
```

**回测局限（诚实声明）**：当前 backtest 为 in-sample 评估（历史价格特征 + 结算标签），
accuracy/校准曲线偏乐观。真实可交易回测需滚动时间切片（train 窗口 → 预测未来市场），
属二期工作。

## 配置

- `config/config.yaml`：全部参数（策略开关、风控阈值、执行参数）
- `.env`（复制自 `.env.example`）：Polymarket 凭证、代理、LLM key
- 环境变量覆盖：`POLY_RISK__MAX_DAILY_LOSS_USD=50`（`POLY_` + `__` 分隔路径）

## 风控清单

单标的上限 / 总敞口上限 / 日亏损熔断 / 最大回撤熔断 / 持仓数上限 /
价格带过滤（0.03-0.97）/ 同市场冷却 / Kelly 分数仓位（默认 0.25）/ 套利组原子性。

## 已知环境问题

- macOS 缺 `libomp` 时 lightgbm 无法加载（OSError），系统自动 fallback 到
  sklearn HistGradientBoosting（无需 OpenMP）；装好 libomp 后自动恢复 lightgbm。
- Polymarket 私有排行榜 API 不可用，跟单数据源采用市场成交聚合替代。

## 测试

```bash
.venv/bin/python -m pytest tests/        # 77 个测试（含端到端离线全链路）
```
