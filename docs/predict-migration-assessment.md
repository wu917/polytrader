# 切换到 Predict 平台交易评估报告

> 日期：2026-08-13 ｜ 基于：官方文档（dev.predict.fun）+ BNB 测试网 API 实测 + 本地代码现状
> 结论先行：**数据层迁移成本低（API 同构，1-2 天），但结算机制（Pyth vs Chainlink TWAP）
> 与费率（2%）会改变策略经济性；执行层需按 Predict 认证/下单重写（0.5-1 天）。**

## 一、平台身份（实测确认）

**Predict（predict.fun）是一个独立的全新预测市场平台**，与 Polymarket 无品牌关系：

| 维度 | 实测事实 | 来源 |
|---|---|---|
| 链 | **BNB Chain**（主网 `api.predict.fun` / 测试网 `api-testnet.predict.fun`）| dev.predict.fun FAQ |
| 结算币 | **USDT**（生息市场 `isYieldBearing=true`，USDT 质押生息）| /v1/markets 实测 |
| 基础设施 | ap-northeast-1（日本，非地理限制区）| dev.predict.fun FAQ |
| 官方 SDK | TypeScript `@predictdotfun/sdk` + **Python `predict-sdk`（PyPI）** | dev.predict.fun |
| API Key | Discord ticket 申请；测试网免 key；限流 240 req/min | dev.predict.fun FAQ |
| 认证 | `x-api-key` header + **JWT**（钱包签 auth message）| dev.predict.fun auth 文档 |
| 生态 | 与 Binance 钱包深度集成（币安钱包 API 可交易、可积累 Predict Points 积分）| Binance 开发者文档 |
| 账户 | EOA 或 Smart Wallet（Predict Account/Privy），网页自动创建 | dev.predict.fun FAQ |

## 二、与 Polymarket 机制对照（updown 市场）

| 维度 | Polymarket（现有） | Predict（实测） |
|---|---|---|
| 市场命名 | `btc-updown-5m-1786607700` | **完全一致**（测试网实测同 slug 格式）|
| 市场发现 | Gamma `/events/keyset?slug=...`（逐 slug 查）| **`/v1/markets?marketVariant=CRYPTO_UP_DOWN`（一次拉全部，更简单）** |
| 市场 ID | condition_id + clobTokenIds | `id`（int）+ `conditionId`（同名存在）+ outcomes[].`onChainId`（= token_id 等价物）|
| 订单簿 | CLOB `/book?token_id=` | `/v1/markets/{id}/orderbook`（bids/asks/lastOrderSettled）|
| 历史价格 | CLOB `/prices-history` | `/v1/markets/{id}/timeseries` |
| 成交记录 | data-api `/trades`（公开）| `/v1/orders/matches`（**需 JWT**，按 signer/公开性待验证）|
| **结算 oracle** | **Chainlink 30s TWAP**（2026-08-07 升级）| **Pyth Network 价格源**（"时间范围末尾价"；相等则 50-50）|
| 结算规则 | 尾段 30s TWAP vs 锚 | 窗口首/末 Pyth 价比较（瞬时/短窗口，非 TWAP）|
| 手续费 | taker 阶梯费率 | **feeRateBps=200（2%）**，存在 maker rebate（测试网有 "rebate 25%" 市场）|
| 价格精度 | 4 位小数 | decimalPrecision=2（**tick 0.01**）|
| 撤单 | 链上 tx（gas）| **API remove**（`DELETE /v1/orders`，无 gas）|
| 链上协议 | Polygon + Gnosis 条件代币 | **BNB + 同构 ERC-1155 ConditionalTokens + CTF_EXCHANGE** |
| 风控字段 | — | spreadThreshold=0.06 / shareThreshold=100（市场自带）|
| 跨平台 | — | 字段 `polymarketConditionIds` / `kalshiMarketTicker`（关联存在，共享未确认）|

## 三、本地代码改造点清单（polytrader → predict）

### 数据层（改造量：中，~1-2 天）

| 模块 | 现状 | 改造 |
|---|---|---|
| `data/gamma_client.py` | /markets + /events/keyset | → `/v1/markets`（marketVariant/status/cursor 分页）；字段映射：`outcomes[].onChainId`→token_id、`conditionId` 同名、`id`→market_id |
| `data/clob_client.py` | REST book + WS 订阅 | → `/v1/markets/{id}/orderbook`；WS 协议不同（Predict 有独立 WS 文档：topics/heartbeats）|
| `data/data_api.py` | /prices-history + /trades | → `/v1/markets/{id}/timeseries`；成交改 `/v1/orders/matches`（认证约束待验证）|
| **updown 发现** | fetch_windows 逐 slug keyset + 30s 过滤 | → `marketVariant=CRYPTO_UP_DOWN&status=OPEN` 一次拉取（**简化**）；窗口过滤逻辑保留（endDate 解析改 Predict 时间格式）|
| 行情源 | Binance/OKX 现货 | 保留（Pyth 结算但 Binance 仍是行情参考）；**若做 Pyth 价预测需接 Pyth Hermes API** |

### 策略层（改造量：小~中）

| 策略 | 影响 |
|---|---|
| LLM updown | 逻辑不变（LLM 判断 vs 市场 ref）；ref 价来源从 Gamma outcomePrices → Predict outcomes（bestAsk/bestBid）|
| 套利/收敛 | 逻辑不变；**结算锚从 Chainlink TWAP → Pyth 末尾价**：TWAP 预测 edge 失效，需重新实测 Pyth 结算行为（瞬时 vs 短窗口）|
| 跟单/聪明钱 | matches 公开性决定可行性 |

### 执行层（改造量：中，~0.5-1 天，关键）

| 项 | Polymarket | Predict |
|---|---|---|
| 认证 | HMAC 三元组 + EIP-712 订单签名 | **API key + JWT**（钱包签 auth message，更简单）；订单仍是签名订单（SDK OrderBuilder/ethers）|
| 下单 | POST /order（token_id）| POST /v1/orders（`marketId + outcomeIndex + side + strategy LIMIT/MARKET + size(wei) + price`）|
| 撤单 | 链上 gas | **API remove（免费）** |
| 前置 | — | 链上 approvals（USDT ERC-20 + ERC-1155，一次性）|
| SDK | 无官方 Python | **官方 predict-sdk（PyPI）** |

### 基础设施层（小）

- `config.yaml`：新增 `predict.*`（base_url/chain_id/fee）；凭证字段改 `PREDICT_API_KEY` / 钱包私钥
- `.env.example` 更新；审计/面板/守护进程**无需改造**（数据源已抽象为 llm_results.jsonl 事件流）
- 测试网（免 key）→ 主网（Discord 申请 key）两阶段

## 四、经济性评估（关键差异）

1. **费率 2% 是最大变量**：$1/笔 × 2% = 每笔固定 -$0.02。当前实测 50-63% 胜率、$1 仓位下
   期望收益 ≈ 0 附近——2% 费率会显著恶化；**若 maker rebate 可观（测试网 25% 样例）可能抵消**，需实测主网真实费率与 rebate 规则
2. **结算 oracle 从 Chainlink TWAP 变 Pyth 末尾价**：我们"TWAP 预测精度 72.7%"的 edge 假设失效；
   Pyth 为瞬时/短窗口价 → 回到"Binance↔Pyth 滞后"套利形态（Pyth 聚合多源，滞后更小），
   **需重新实测**（复用 `measure_settlement.py` 方法论，接 Pyth Hermes feed）
3. **tick 0.01 vs 0.0001**：价格粒度变粗，edge 计算与 maker 挂单价需按 0.01 对齐
4. **流动性未知**：测试网 orderbook 为空壳；主网流动性（含 Binance 钱包引流）待验证——
   空壳盘口下 maker 挂单可能长期不成交
5. **地理/网络**：服务器在日本，中国大陆访问可能比 Polymarket（Cloudflare/CDN）更快、代理依赖更低

## 五、风险与不确定项

| 风险 | 说明 |
|---|---|
| 主网 API key 申请 | Discord ticket 人工审核，可能非即时 |
| 平台 beta 期 | REST API 官方标注 beta；接口可能变动 |
| 流动性风险 | 新平台主网深度未知，updown 空壳盘口概率高（与 Polymarket 早期相同）|
| 结算机制实证缺口 | Pyth 末尾价的精确取价逻辑（是否短窗口加权）未实测——**上线前必须用 measure_settlement 方法论跑 24-48h 结算样本** |
| 合规 | 平台地区限制政策未完全披露（ap-northeast-1 暗示无限制，仍需确认条款）|
| 与 Polymarket 的关联字段 | `polymarketConditionIds` 含义未验证（可能是镜像市场引用，非共享流动性）|

## 六、结论与建议路径

**结论**：Predict 是"API 同构、机制微调"的平台——数据层是 Polymarket 的简化版
（一次拉取 updown 市场、API 撤单、官方 Python SDK），**迁移技术成本低（2-3 天）**；
但**结算 oracle（Pyth）与费率（2%）两个变量会改变策略经济性**，不能直接平移结论。

**建议路径（先验证后迁移）**：
1. **Day 1（免费，测试网）**：数据层适配（/v1/markets + orderbook + timeseries）+ 结算机制实测
   （Pyth 末尾价 vs Binance 瞬时一致性，复用 measure_settlement.py）
2. **Day 2**：LLM updown 策略移植（测试网 paper 模式），跑 1-2 天积累样本；
   同时 Discord 申请主网 API key
3. **Day 3+**：主网灰度（$1/笔 maker 限价单），重点观测：真实费率/rebate、成交率、Pyth 结算一致性
4. **决策点**：若 Pyth 结算可预测性 ≈ Chainlink TWAP（>70% 一致性）且 maker rebate 覆盖 2% 费率
   → 值得迁移；否则保持 Polymarket 并等待其生态成熟

## 附：参考来源

- dev.predict.fun（FAQ / API 文档 / llms.txt 端点全清单 / TS+PY SDK）
- https://api-testnet.predict.fun/v1/markets（实测：CRYPTO_UP_DOWN 市场、slug 格式、Pyth 结算描述、feeRateBps=200）
- Binance 开发者文档（w3w prediction：订单簿 WS API、Predict Points）
- https://www.npmjs.com/package/@predictdotfun/sdk（官方 SDK）
