# up or down（5/15 分钟）市场交易策略报告

> 生成日期：2026-08-12 ｜ 基于：外部调研（Blockworks Research / dev.to 实战文章 / GitHub 开源项目）+ 本项目实盘探明数据

## 一、市场机制（本项目实测）

| 事实 | 实测值 |
|---|---|
| 市场形态 | 7 币种（BTC/ETH/SOL/XRP/DOGE/HYPE/BNB）× 5M/15M 滚动窗口 |
| 数据源 | Gamma `/events/keyset?slug=*-updown-5m/15m-<ts>`（不在 /markets） |
| 窗口锚定 | 5m 与 15m 窗口**起点/终点均不同**，"涨"各自锚定各自窗口起点价（跨窗口价差无套利含义） |
| 流动性 | 真实存在（$170-$6.7K/市场），但**盘口为空壳**：bid 0.001-0.01 / ask 0.99+，部分市场无挂单 |
| 定价 | YES+NO 共识价和**精确 1.000**（14/14 市场）——静态二元套利无机会 |
| 结算 | 5/15 分钟窗口到期自动结算（Chainlink oracle） |

**核心含义**：盘口空壳 → **taker 市价单不可行，只能 maker 限价单**在中间价挂单成交。

## 二、策略清单（外部调研）

### A. 执行型（赚定价滞后与结构，非方向预测）

| 策略 | 原理 | 入场规则 | 来源 |
|---|---|---|---|
| **A1. 组合套利（Harvester）** | BTC 窗口内剧烈波动时，YES+NO 组合价瞬时 <$1（如 0.46+0.47），两边都买锁定 $1 结算 | 监控组合价 < 1-ε 出现即买（先买便宜腿，等波动回摆补第二腿）| Blockworks《A Game of Volatility》——**盈利交易者中最普遍** |
| **A2. 定价滞后套利（Seer）** | Polymarket 重定价比 Binance 慢几秒；BTC 跌 0.6% 后真实概率 ~78% 而盘口仍 ~54/46 | Binance WS 秒级信号 → 在 Polymarket 修正前买入正确侧 | Blockworks + dev.to |
| **A3. 尾段确定侧（Near-Resolution）** | 最后几秒赢家 98-99¢ ≠ $1，买入赚折价 | T-30s 内买 ≥0.98 侧 | dev.to——**与本项目收敛回测结论一致：折价仅 0.1-0.2%，无利可图** |
| **A4. 跨时间框架失同步** | 一个窗口已调整、另一个滞后（时序性，非静态价差） | 5m 信号 vs 15m 未动时介入 | dev.to |

### B. 方向型（预测模型 + 微观结构）

| 策略 | 原理 | 入场规则 | 来源 |
|---|---|---|---|
| **B1. 5m 前 90 秒动量/均值回归** | 新窗口前 30-90s：动量爆发（跟随）或上一窗口过度延伸（反转）| 窗口开盘 60s 内，动量/回归信号 + 价格确认 | dev.to《11,717 笔交易框架》 |
| **B2. ML 概率模型（Sentinel+Pulse）** | LightGBM：已完窗口特征预测下一窗口 + 当前窗口 80% 时点预测 | |P_fair − P_market| > 成本后 edge，Kelly 仓位 | GitHub（多个同类项目） |
| **B3. 动态对冲** | 持仓逆动超阈值（5m：1-2 分钟 >15%）→ 平仓反手 | 损失信号触发反转 | dev.to《Dynamic Hedging》 |

### C. 结构型

| 策略 | 原理 | 备注 |
|---|---|---|
| **C1. 做市** | 盘口空壳 = 做市空间大（挂中间价双边） | 需持续挂单 + live 签名，收益=价差 |
| **C2. 盘口失衡/Imbalance** | bid/ask 量比 + 跨合约 z-score | 当前盘口无真实挂单，暂不可用 |

### D. 结算锚与 Oracle 层（2026-07 斯坦福论文 + Chainlink 更新后）

| 策略 | 原理 | 依据 |
|---|---|---|
| **D1. Oracle Gap 定价** | 结算价 = **Chainlink Data Streams 时间戳报价**（非 Binance、非 VWAP）。Binance 与 Chainlink 聚合价可偏离 **0.3-0.8% 达 10-30 秒**（高波动窗口）→ 用 Chainlink 真实报价而非 Binance 比较 Polymarket 隐含概率，才能正确定价 | dev.to《Why My Polymarket Bot Watches Chainlink, Not Binance》 |
| **D2. PTB（T0 开盘价）先行** | 结算对比基准 = 窗口边界的 **Chainlink 第一个 tick**；前端显示滞后 ~20 秒。T0 解析器实时订阅 Chainlink 流取第一 tick → 入场前即知锚点 | dev.to《How to get the Price To Beat at T0》 |
| **D3. 结算时间戳不确定（T-90s 降仓）** | Chainlink Automation 触发延迟数秒 → 结算价取**自动化触发块**的价格，尾段含执行不确定性；作者规则：最后 90 秒缩小仓位 | dev.to 11,717 笔框架 |
| **D4. 近均衡窗口尾段流动性撤离** | 5m 市场 6% 近均衡窗口：做市商尾段撤流动性 + 净订单流 +50%（3.9 倍）→ 尾段冲击成本极高，**避免尾段 taker、尾段做市更有利** | 斯坦福论文 |
| **D5. 结算操纵推价（已关闭）** | 821 钱包尾段 10 秒在 Binance 推价（oracle 偏差 2.5bp、85% 同侧）赚 $8.2M；65% 翻转率，确定侧 90-100% 仍被翻转 34% | 斯坦福论文；**2026-08-07 起 5m/15m/4h 改 Chainlink TWAP（30s/60s）+ $1M 流动性激励，此策略已关闭** |
| **D6. Deribit IV 定价错价发现** | 用 BTC/ETH/SOL **期权隐含波动率** → 二元期权理论价（one-touch/digital）→ 与 Polymarket 价差；48h 实测定价误差 2% 内，限价单 + Quarter Kelly + 15%/30% 集中度上限 | @ClawdyBot 48h 报告 |
| **D7. 1d 市场单点锚套利** | 1h 锚=Binance 1h 蜡烛开盘（已定，无空间）；**1d 锚=noon ET 1 分钟收盘价（TWAP 未覆盖）→ 仍有推价窗口** | dev.to + 论文修复范围 |

## 三、可实施性评估（基于本项目基础设施）

| 策略 | 数据/执行需求 | 本项目现状 | 可行性 |
|---|---|---|---|
| A1 组合套利 | monitor_updown 已有 ARB 检测（共识价）；**需 maker 限价单**（空壳盘口 taker 不可行）| 检测 ✅ / 执行 ❌（live 签名未实现）| **半可行**：可先 paper 检测+模拟 |
| A2 定价滞后（修正：参照应为 **Chainlink** 而非 Binance，见 D1）| 需 Chainlink Data Streams WS（新组件）| 无 | 需开发；edge 真实但依赖延迟 |
| A3 尾段确定侧 | 已有（convergence） | 已回测：折价 0.1-0.2% **否决** | ❌ |
| B1 前 90s 动量 | monitor 扩展（窗口开盘检测）| 检测器可加 | ✅ 可实盘验证（观测） |
| B2 ML 模型 | 特征：动量/ATR/距结算/盘口失衡 | AI 引擎已有（LightGBM）| ✅ 可开发（需累积窗口结算样本训练） |
| B3 动态对冲 | 持仓+价格监控 | 需开发 | 可做（paper） |
| C1 做市 | maker 循环 + live 签名 | ❌ live 未实现 | 二期 |
| D1 Oracle Gap | Chainlink WS + 定价比较 | 需开发（feed 接入）| 可做（paper） |
| D2 PTB 先行 | Chainlink 流 T0 tick 解析 | 需开发 | 可做，价值高（入场定价基准）|
| D4 尾段流动性 | 监控器已有 secs_left | 可加尾段深度记录 | ✅ 低成本观测 |
| D6 IV 定价 | Deribit API（期权 IV）| 需开发 | 可做（paper 错价发现器）|

## 四、结论与建议

1. **定价参照已明确：Chainlink（结算锚）而非 Binance**——A2 修正为 D1（Oracle Gap），这是"已证实存在"的 edge 之一（Binance↔Chainlink 分歧 0.3-0.8% × 10-30 秒）
2. **A1（波动组合套利）与 D2（PTB 先行）是结构上最扎实的方向**：A1 依赖波动出现组合价 <1，D2 解决入场基准问题
3. **D5（结算推价）已关闭**（TWAP 生效），但 **D7（1d 市场 noon ET 锚）仍在 TWAP 覆盖外**——需注意合规与资本门槛
4. **A3 已被本项目回测否决**（收敛折价 0.1-0.2%）
5. **B1（5m 前 90 秒动量/回归）是无需新基建即可实盘验证的方向**：扩展 monitor_updown 记录窗口开盘 90s 的价格行为与结算结果，挂机 24-48h 积累样本（每 5 分钟一个窗口，7 币 ≈ 2000 样本/天）→ 统计动量/回归信号胜率
6. **建议优先级**：① 挂机采集（B1 信号 + 结算配对，成本为零）→ ② D2 PTB 先行 + D1 Oracle Gap paper 验证（Chainlink feed 接入，~50 行）→ ③ D6 IV 定价错价发现（Deribit API）→ ④ live 签名实现后试 A1/D1 maker 执行（真金白银前必须 2 周 paper）
7. **执行真相**（Blockworks + 斯坦福）：短期市场是"执行机器"——零售的波动养活了速度、结算锚信息与对冲优势的算法。纯方向预测（无延迟与锚信息优势）在这些市场长期期望为负；尾段冲击成本极高（近均衡窗口）

## 六、TWAP 时代实测（2026-08-12 测算）

- **测算方法**：最近 12 个已结算 btc 5m 窗口，对比结算方向 vs ①Binance T1 瞬时价方向 ②Binance 1s kline 30s TWAP 方向（结算锚近似）
- **结果**：瞬时一致性 **54.5%**（≈随机），TWAP30 一致性 **72.7%**——**实证确认 5m 结算锚为 30s TWAP**（2026-08-07 升级）
- **推论**：① D1/A2"Binance 瞬时滞后套利"已失效（锚不再是瞬时价）；② 结算方向可由尾段 30s TWAP 预测（72.7% 起步，误差来自 Binance↔Chainlink 聚合差异与触发延迟）；③ 新 edge = **TWAP 预测精度**（T1-30s 前预测 30s 均价区间 vs Polymarket 定价），而非执行速度
- 数据与脚本：`scripts/measure_lag.py`（实时滞后采样）、`scripts/measure_settlement.py`（结算锚验证）、`backtest_results/ptb_settlement_*.json`

## 五、参考来源
- Blockworks Research《A Game of Volatility》（trader 四画像：Seer/Harvester/Flipper + 做市）
- dev.to《Polymarket Short-Term Bots: 6 Profitable Strategies》
- dev.to《11,717 Trades: 5m/15m/1h Framework》（窗口开盘 90s 入场、T-90s 禁入、Chainlink oracle）
- dev.to《Dynamic Hedging in Short-Term Markets》
- dev.to《Why My Polymarket Bot Watches Chainlink, Not Binance》（Oracle Gap 0.3-0.8%）、《How to get the Price To Beat at T0》（PTB/T0 解析器）
- 斯坦福论文《Settlement Manipulation in Prediction Markets》（2026-07，$8.2M 结算操纵）+ Chainlink TWAP 升级（2026-08-07，$1M 流动性激励）报道
- @ClawdyBot 48h 实盘报告（Deribit IV 定价、限价单、Quarter Kelly）
- GitHub：4coinsbot（Late Entry V3）、MrFadiAi/Polymarket-bot（DipArb）、多个 ML 模型项目（Sentinel+Pulse/Regime-gated）
