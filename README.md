# PolyTrader

Polymarket 预测市场自动化交易系统（Python 3.11+）。模块化设计：套利 / AI 概率 / LLM 判断 /
聪明钱跟单四大策略 + 完整风控，支持 dry-run / paper / live 三种执行模式；含 updown 5/15 分钟
快速市场专项工具（扫描、监控、LLM 模拟测算、审计）以及通用事件盘、股票/商品日级盘专项模块。

> ⚠️ **盈利性声明**：本系统提供行业标准策略与风控框架，但**不保证持续盈利**。
> 预测市场存在对手方风险、滑点、模型失效等风险。请先用 dry-run/paper 模式长期验证，
> 再决定是否投入真实资金。live 模式当前被安全闸拦截（见下文）。
> **实测结论（诚实）**：截至 2026-08-12 的所有真实数据验证中，未发现可稳定盈利的策略——
> updown 市场共识价和精确 1.000（无静态套利）、LLM 判断 20 轮 50% 胜率（无 alpha）、
> 收敛折价仅 0.1-0.2%（无利可图）。详见 `docs/updown-strategies-report.md`。

## 快速开始

```bash
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.txt

# 前置：本地 MySQL 8.0.46（已开单队列存储），连接参数见 .env 的 POLY_DB_*
# 启动: ../mysql-8.0.46/bin/mysqld --basedir=../mysql-8.0.46 --datadir=../mysql-8.0.46/data \
#   --port=3306 --bind-address=127.0.0.1 --socket=../mysql-8.0.46/mysql.sock \
#   --pid-file=../mysql-8.0.46/mysql.pid --log-error=../mysql-8.0.46/mysql.err --daemonize

# 离线全链路模拟（合成市场，无网络，不依赖 MySQL）
.venv/bin/python scripts/run_polytrader.py offline

# 运行测试
.venv/bin/python -m pytest tests/        # 187 个测试
```

网络说明：本机访问 Polymarket **经本地 HTTP 代理 127.0.0.1:7897**（.env `HTTP_PROXY`/
`HTTPS_PROXY` 与代码 `_req`/`HttpClient` 均显式配置此端口；裸直连不通）。若在无代理
环境运行，需在 `HttpClient(proxy=...)` 显式配置可用代理。

## 架构

```
polytrader/
├── config.py            # 配置：config.yaml + POLY_ 环境变量 + .env 凭证
├── models.py            # Market / OrderBook / Signal / Trade / WalletProfile
├── db.py                # 已开单队列的 MySQL 存储（pending_trades 表，POLY_DB_* 连接）
├── data/
│   ├── gamma_client.py  # 市场发现（Gamma API，含 JSON 字符串数组兼容）
│   ├── clob_client.py   # 订单簿 REST + WS 实时订阅
│   └── data_api.py      # 历史价格（CLOB /prices-history）+ 成交记录（/trades）
├── strategies/
│   ├── arbitrage.py     # 二元 YES/NO 互补 + 分类市场概率和套利
│   ├── ai_probability.py# 模型概率 vs 市场价格 → 期望边际
│   ├── llm_book.py      # LLM 盘口策略（盘口上下文进 prompt，双侧 edge）
│   ├── llm_updown.py    # updown 市场 LLM 方向判断（实时行情 + 锚定修正）
│   ├── event_market.py  # 通用事件盘（LLM 判 P(YES)，双侧 edge + RR/EV 过滤）
│   ├── equity_updown.py # 股票/商品日级 updown（日 K + 大盘局势）
│   └── equity_context.py# 股票/商品盘上下文构建（日 K 特征 + SPY/QQQ/VXX 局势）
├── ai/
│   ├── models.py        # 可插拔：lightgbm（需 libomp）/ sklearn fallback
│   ├── features.py      # 14+ 维特征（元数据/订单簿/价格动量/类别）
│   ├── train.py         # 标签提取（resolved 市场）+ 训练 + isotonic 校准
│   ├── convergence.py   # 收敛/确定性折价回测（尾段确定侧买入）
│   └── llm_scorer.py    # OpenAI 兼容 LLM 概率评分（含 reason 提取与审计）
├── copytrade/
│   ├── wallet_analysis.py  # 钱包画像：FIFO 盈亏 / 胜率 / 评分
│   ├── leaderboard.py      # 目标钱包发现：官方排行榜（/v1/leaderboard，MONTH/PNL）
│   │                       #   + 市场成交聚合（私有排行榜 API 的公开替代）
│   └── mirror.py           # 镜像引擎：资格过滤 / 去重 / 滑点容忍 / 套利冲单过滤 /
│                           #   活动流（/activity）轮询信号
├── risk/
│   ├── kelly.py         # 分数 Kelly 仓位
│   └── risk_manager.py  # 敞口 / 日损熔断 / 回撤熔断 / 冷却 / 价格带
└── execution/
    ├── broker.py        # dry-run（模拟）/ paper（真实取价模拟）/ live（安全拒绝）
    ├── order_manager.py # Kelly 定仓 + 套利组原子性（组内全成或全弃）
    ├── order_v2.py      # CLOB V2 订单 + ERC-7739 签名
    ├── chain.py         # 链上广播（sign + eth_sendRawTransaction + RPC 轮换）
    ├── relayer.py       # gasless 钱包操作（POLY_RELAYER_*）
    └── signer.py        # ClobAuth + POLY_* L2 HMAC

scripts/
├── run_llm_loop.py      # 多轮循环主任务（窗口内扫描 + 开单写 MySQL）
├── settle_worker.py     # 常驻结算进程（任务退出后结算不停止）
├── run_daemon.py        # 无限挂机守护进程（复用 run_llm_loop --rounds 1）
├── backfill_settlements.py # 兜底补结算（settle_worker 未运行时才需要）
├── run_live_loop.py     # 5m 加密 updown 实盘循环（FOK 吃单，盘口预检 + token 校验）
├── run_copytrade_loop.py # 跟单循环：排行榜 → 活动流轮询 → 镜像 BUY（paper 默认，--live 实盘 FOK）
├── run_event_live_loop.py # 通用事件盘实盘循环（maker GTC post_only）
├── run_equity_live_loop.py # 股票/商品日级实盘循环（FOK 吃单）
├── scan_event_markets.py  # 通用事件盘扫描 + LLM 评估
├── scan_equity_updown.py  # 股票/商品日级盘扫描 + LLM 评估
├── simulate_equity_updown.py # 股票/商品日级模拟回测
└── ...（scan/monitor/simulate/web_dashboard 等）
```

**存储**：已开单队列存于本地 MySQL（`polytrader.pending_trades` 表）——
`run_llm_loop`/`run_daemon`/`*_live_loop`/`run_copytrade_loop`/`simulate_*` 开单写入，
`settle_worker` 常驻轮询结算，多进程共享、不随任务删除。`window` 枚举含
`5m/15m/daily/event/copytrade`；`status` 除 `pending/settled` 外新增
`cancelled`（实盘订单最终未成交时释放占坑，见"跟单交易"）。

## 使用

### 四类策略

| 策略 | 原理 | 关键参数 |
|---|---|---|
| 套利 | YES+NO ask 和 < 1-ε 同时买入；互斥候选概率和 < 1-ε 全买 | `arbitrage.min_edge` |
| AI 概率 | 特征 → 模型 P(YES) vs ask 价，edge ≥ ε 买入 | `ai_probability.min_edge` |
| LLM 盘口 | 订单簿上下文进 prompt，LLM 双侧评估 edge（DeepSeek 等） | `llm_book`（`scripts/run_polytrader.py llm`） |
| LLM updown | 实时行情（Binance/OKX）→ LLM 判断窗口方向 vs 市场 ref | `llm_updown`（见下） |
| 事件盘 | 全量二元市场（选举/宏观/地缘），LLM 世界知识判 P(YES)，双侧 edge + RR/EV 过滤 | `event_market`（`scan_event_markets`） |
| 股票/商品盘 | 日 K 特征 + 大盘局势（SPY/QQQ/VXX）→ LLM 判日级涨跌 vs 隐含价 | `equity_updown`（`scan_equity_updown`） |
| 跟单 | 官方排行榜（/v1/leaderboard MONTH/PNL）选聪明钱 → 活动流轮询 → 镜像其 BUY（去重/滑点容忍/套利冲单过滤） | `copytrade.*`（`run_copytrade_loop`） |

### 模式

- **dry-run**：无网络，按信号价模拟成交（测试/演示）
- **paper**：真实拉取市场/订单簿，模拟成交（策略验证）—— **默认推荐，收益率回测用它**
- **live**：真实下单（EIP-712 签名 + CLOB 提交已实现，见下文"实盘交易（live）"）。
  默认**关闭**且有多重安全护栏，绝不盲目下单

```bash
# paper 模式真实扫描
.venv/bin/python scripts/run_polytrader.py paper --proxy http://127.0.0.1:7897

# LLM 盘口扫描（DeepSeek 双侧评估，真实市场）
.venv/bin/python scripts/run_polytrader.py llm --proxy http://127.0.0.1:7897
```

### 实盘交易（live / CLOB V2）

> ⚠️ **真金白银**。Polymarket 已于 2026 年迁移到 **CLOB V2**（pUSD 结算、deposit wallet
> 账户模型、POLY_* L2 认证、ERC-7739 订单签名）。以下为已**实测打通**的完整链路。

**账户模型（V2）**：用户 EOA（签名者）→ Polymarket 自动部署 **deposit wallet**
（链上合约，`0x00000000000Fb5C9ADea0298D729A0CB3823Cc07` 工厂 CREATE2 部署，
充值/下单资金都在 deposit wallet）。订单由 EOA 签 ERC-7739-wrapped 签名
（signatureType=3 / POLY_1271），maker = signer = deposit wallet 地址。

**启用步骤**：
1. `.env` 填写：
   - `POLYMARKET_PRIVATE_KEY`（EOA 私钥，签名者）
   - `POLYMARKET_DEPOSIT_WALLET`（deposit wallet 地址，从 polymarket.com Settings→Wallet 复制）
   - `POLYMARKET_RELAYER_API_KEY` / `_ADDRESS`（settings?tab=api-keys 创建，gasless 钱包操作）
2. 充值 pUSD 到 deposit wallet（三选一）：
   - 官方桥 API：`POST https://bridge.polymarket.com/deposit`（BSC/ETH 等跨链，最小 $5，
     自动兑换 pUSD）；或链上脚本 `scripts/fund_deposit.py`（Polygon USDC→pUSD 自动转入，
     走本地代理访问 Paraswap、`wait_tx` 后校验 status=1——revert 不再误报成功，
     见 RUNBOOK 9.2）
3. 下单验证：`scripts/run_live_loop.py` 实盘实测 + tests/test_order_v2.py 单测

**CLOB V2 认证（新协议）**：
- L1：ClobAuth EIP-712 签名 → `GET /auth/derive-api-key`（POLY_ADDRESS/SIGNATURE/TIMESTAMP/NONCE 头）
- L2：`POLY_*` 5 头 + HMAC-SHA256（message = timestamp + METHOD + path + body，
  secret 为 urlsafe base64）

**订单（V2）**：11 字段 signed struct（salt/maker/signer/tokenId/makerAmount/takerAmount/
side/signatureType/timestamp/metadata/builder），domain version "2"，
verifyingContract = CTF Exchange V2（`0xE111180000d2663C0091e4f400237545B87B996B`）。
deposit wallet 订单签名 = ERC-7739 wrapped（EOA 签嵌套 TypedDataSign，636 hex 字符），
与官方 `@polymarket/clob-client-v2` **逐字节一致**（单测交叉验证）。
精度（官方文档规则，`order_v2.calc_amounts` 实现）：tick=0.01 → 价格 ≤2 位小数、
份额 ≤2 位小数（BUY 份额向上取整保证隐含价精确落 tick）、USD ≤4 位小数；
FOK/marketable BUY 最小 $1；每单有手续费（fee estimate ~$0.03）。
5m 盘实测约束：`orderPriceMinTickSize=0.01`、`orderMinSize=5 shares`（book API 返回）。
**tick/negRisk 自动解析**（`place_order` 统一入口，2026-08-16 修复）：下单前按 token 查
`GET /tick-size` 与 `GET /neg-risk`（各 600s 缓存），tick=0.001 市场份额按 5 位精度计算、
负风险市场改用 NEG_RISK_EXCHANGE_V2 合约签名（domain separator 不同）——修复了部分市场
`invalid POLY_1271 signature`（tick 精度错位 + negRisk 合约不匹配）。

**已实测（2026-08-14，$1 真实成交）**：
认证 → 签名 → 下单（200 matched）→ 链上成交（tx status=1）→ 持仓 2.2222 shares
全链路打通。充值：`fund_deposit.py`（USDC→pUSD 经 Paraswap 聚合器，$1.10 → 1.0999 pUSD，
几乎无损）。

**坏单过滤（实盘与模拟通用）**：预期成交价须在 **[0.20, 0.85]**（`run_live_loop`
按吃单侧盘口价预检，空壳盘口超范围则过滤；`simulate_*` 同规则），避免在空壳
盘口上以极端价格成交。**注意**：买 NO 时预检须看 **NO 侧自身 ask**（勿用
1-bid 对称估算——空壳盘 bid/ask 不对称时失真，曾致 FOK 以 0.01 档极端价成交，
2026-08-16 修复）。
**实测（2026-08-14）**：5m 盘空壳盘口下 FOK 100% 无法
成交（无对手盘，400 "couldn't be fully filled"）；改 maker GTC post_only 后
挂单可成功，但远价 maker 单可能被对手盘吃掉（$1@0.10 曾被 MATCHED 后结算归零，
损失 $1）——maker 单的风险不在价格远近而在有无对手盘，验证脚本严禁真实下单
（见 AGENTS.md 第 5 节）。

**实现**：`execution/order_v2.py`（V2 订单 + ERC-7739 签名）、`execution/chain.py`
（链上广播：sign + eth_sendRawTransaction + RPC 轮换）、`execution/relayer.py`
（gasless 钱包操作）、`execution/signer.py`（ClobAuth + POLY_* L2 HMAC）、
`data/clob_client.py`（post/get/cancel order V2）、`scripts/fund_deposit.py`（充值）。
`scripts/run_live_loop.py` 的 `place_order` 是四个实盘循环（5m/事件盘/股票盘/跟单）
**共用的统一下单入口**（FOK 吃单 / GTC maker post_only 二选一，自动解析 tick 与
negRisk，见上）。
（下单验证由 run_live_loop 实盘实测 + 单测覆盖；verify_live_order_v2 已删除——无护栏真实下单的过时入口）

**安全护栏**：凭证只存 `.env`（gitignore）；`SENSITIVE_FIELDS` 遮盖；链上交易
（充值/swap）每次展示交易内容；真实下单前需确认。

**实盘运行（`scripts/run_live_loop.py`，与 run_llm_loop 统一窗口扫描语义）**：
```bash
HTTPS_PROXY=http://127.0.0.1:7897 PYTHONPATH=. .venv/bin/python -u \
  scripts/run_live_loop.py --rounds 3 --min-edge 0.04 --log logs/live_loop.log
```
- **单笔 $1 硬上限**（`MAX_ORDER_USD=1.0`，代码写死；`--size` 超 1 或 ≤0 直接拒绝启动）
- **FOK 吃单**（marketable，立即成交或拒绝）：下单前盘口预检（预期成交价 ∉ [0.20, 0.85] 过滤）、
  verify_token 校验（60s 缓存）、窗口剩余 <30s 跳过；成交后 mark_filled 实际成交价
- 防重复开单（同窗口 slug 只试一次）、每轮复查余额、下单前 `verify_token` 校验
  （规避 5m 市场新建时 CLOB token 未生效的偶发 `invalid token id`）
- 代理读 `HTTPS_PROXY` 环境变量（缺省回退 http://127.0.0.1:7897）
- 成交写入 `pending_trades`（`mode='live'` + orderID + fill_price + LLM 建议），
  settle_worker 自动结算

**近期实测结论（2026-08-14）**：
- **50 轮模拟（坏单过滤 [0.20,0.85] + 市价模拟）**：50 轮仅 10 笔通过过滤，
  **9 笔结算全赢 +$10.67（胜率 100%，收益率 +118%）**——过滤后的信号质量极高
- **实盘 10 轮**：0 成交（updown 盘口长期空壳 bid 0.01/ask 0.99，坏单过滤挡掉全部信号）；
  1 笔 15m maker 挂单 90s 未成交、撤单"失败"实为 `CANCELED_MARKET_RESOLVED`
  （市场结算自动取消，**资金自动释放无残留**）
- **结构性现实**：updown 5m/15m 盘口几乎总是空壳 → 坏单过滤下实盘成交机会极少；
  过滤后的机会（50 轮模拟）胜率极高，但需盘口出现真实流动性才可成交

### 跟单交易（copytrade）

第四策略：**官方月度排行榜选聪明钱 → 活动流轮询 → 镜像其 BUY**。数据链路
（2026-08 官方文档确认的公开端点，均在 data-api）：

1. **目标发现**：`/v1/leaderboard?timePeriod=MONTH&orderBy=PNL`（官方每月排行榜，
   pnl 为官方口径期间盈亏）；`--period DAY/WEEK/MONTH/ALL`、`--category`（OVERALL/
   POLITICS/SPORTS/CRYPTO 等）、`--top-n`（默认 20）、`--min-profit` 过滤。
   排行榜源无交易数/活跃时间字段 → `min_trades` 与活跃度检查自动跳过
2. **钱包画像**：`wallet_analysis.py`（FIFO 盈亏/胜率/评分）供聚合数据源使用；
   官方排行榜直接取 pnl 排序
3. **实时监听**：`/activity?user=<wallet>` 每 `--poll` 秒轮询，TRADE 事件含
   `transactionHash`，可靠去重（`copytrade_seen` 表持久化，跨轮生效）
4. **镜像信号**（`MirrorEngine.scan_activity`）：仅 BUY 侧、仅 YES
   （outcomeIndex=0）、滑点容忍（基础 `--max-slippage` 0.05，随活动年龄每
   分钟 +0.01 动态放宽、封顶 0.15）、超龄不跟（`--max-age-seconds` 默认 600s）
5. **套利/冲单过滤**（默认开启）：同一钱包同一市场 `--wash-window`（默认
   1800s）内出现 SELL（买卖往返/冲单），或双 BUY 反向且双腿间隔 ≤ `--arb-gap`
   （默认 60s，防对冲误判为套利）→ 该市场 BUY 全部过滤；`--no-wash-filter` 关闭
6. **执行**：默认 **paper**（DryRunBroker 模拟成交）；`--live` 时 FOK 真实下单
   （吃单侧 ask 预检 + 价格带 [0.30, 0.90]，沿用 run_live_loop 的 `place_order`
   统一下单入口）→ 入库 MySQL（`window='copytrade'`，live 单 `mode='live'`）
   → settle_worker 自动结算

```bash
# paper 模拟（默认，不碰真实资金）
.venv/bin/python scripts/run_copytrade_loop.py --rounds 5 --log logs/copytrade.log

# 实盘试跑（⚠️ 需用户显式授权：FOK 真实下单，$1/笔）
.venv/bin/python scripts/run_copytrade_loop.py --live --max-live-orders 2 --rounds 5

# 无限挂机（--rounds 0；持仓上限热更新见下）
.venv/bin/python scripts/run_copytrade_loop.py --live --max-live-orders 5 --rounds 0
```

**持仓上限（实盘）**：`--max-live-orders`（默认 2）为实盘总开单硬上限，按 DB
未结算 live 单数控制——**结算释放自动补单**，保持同时持仓 ≤ 上限。paper 模式用
`--max-open-positions`（默认 10）控制未结算 copytrade 单数。**热更新**：运行中
改 `logs/copytrade_limit.txt`（一个数字）即生效，无需重启（仅 live 生效，改小
立即收紧、改大立即放行）。

**delayed 成交回填**：FOK 返回 `delayed`（排队确认中、无成交价）时登记
`pending_fills`，每轮 `get_order_auth` 轮询；MATCHED 后回填 fill_price/tx；
**600s 超时仍未成交 → DB 置 `status='cancelled'` 释放占坑**（避免假单永久
pending 占持仓名额）。

**护栏**：单笔 $1 硬上限（`MAX_ORDER_USD`，`--live` 时 `--size` 超 1 拒绝启动）；
凭证只从 `.env`；结果写 `backtest_results/copytrade_results_*.jsonl`；
**崩溃兜底**：未捕获异常（含 BaseException）traceback 写
`logs/copytrade_crash.log`（防 stderr 被 nohup 丢弃导致崩溃原因不可见）。

### 股票/商品盘（日级 updown）与通用事件盘

**股票/商品盘**（`EquityUpdownStrategy`）：日级 Up-or-Down 市场，结算 =
当日收盘 vs 前一日收盘（Pyth Close）。输入为日 K 技术特征 + 大盘局势
（SPY/QQQ/VXX），参考价 ref 来自 Gamma outcomePrices（市场隐含 P(涨)）。
数据源 stockanalysis.com 公开 API（免费无 key）；商品/指数用 ETF 代理
（XAUUSD→GLD、XAGUSD→SLV、WTI→USO、HSI→EWH、UKX→EWU），prompt 已标注。
支持 17 个标的：NVDA/TSLA/MSFT/AAPL/AMZN/GOOGL/META/COIN/PLTR + SPY/QQQ/NDX
/金/银/WTI/恒生/富时100。

```bash
# 扫描当日股票/商品盘 + LLM 评估（paper 模式）
.venv/bin/python scripts/scan_equity_updown.py --list-only    # 只列盘口不调 LLM
.venv/bin/python scripts/scan_equity_updown.py --min-edge 0.05

# 日级模拟回测：信号 → 模拟成交 → 入库（window='daily'）→ settle_worker 结算
PYTHONPATH=. .venv/bin/python scripts/simulate_equity_updown.py \
    --min-edge 0.05 --min-liquidity 200 --size 100

# 日级实盘循环：首个 |edge|>=min_edge 信号 FOK $size 真实下单
PYTHONPATH=. .venv/bin/python scripts/run_equity_live_loop.py --size 1 --min-edge 0.05
```

**通用事件盘**（`EventMarketStrategy`）：不绑定股票/商品，覆盖任意活跃二元
市场（选举/宏观/地缘/商业）。复用 LLMBookStrategy 评估骨架，增加收益比
`RR=(1-p)/p` 与期望值 `EV=P(win)×(1-p)-(1-P(win))×p`，开单条件 =
edge 阈值 + RR 下限（默认 1.5，即买入价 ≤0.40 或 ≥0.60 侧）+ EV>0。

```bash
# 事件盘扫描 + LLM 评估
PYTHONPATH=. .venv/bin/python scripts/scan_event_markets.py --list-only
PYTHONPATH=. .venv/bin/python scripts/scan_event_markets.py \
    --no-db --min-vol 5000 --min-edge 0.05 --min-rr 1.5

# 事件盘实盘循环：maker GTC 限价单（post_only 挂中间价等成交，非 FOK 吃单）
# 盘口空壳 taker 不可行，只能 maker；挂单后轮询状态，未成交自动撤单
PYTHONPATH=. .venv/bin/python scripts/run_event_live_loop.py \
    --size 1 --min-edge 0.05 --min-rr 1.5 --wait 600
```

> ⚠️ 四个实盘循环脚本（`run_live_loop` / `run_event_live_loop` /
> `run_equity_live_loop` / `run_copytrade_loop --live`）均涉及**真实资金**，
> 默认每轮最多 1 笔、$1/笔，
> 资金预检（deposit wallet pUSD 需覆盖 size + 手续费）。实盘/模拟成交均
> 写入 `pending_trades`（`mode` 字段区分 `live`/`simulate`），由
> `settle_worker` 常驻进程自动结算。`run_live_loop` 为 **FOK 吃单**模式
> （`--fok-slip` 滑点容忍，默认 0.01；`--per-round` 每轮最多开单数），
> 参数见 `--help`。

### updown 5/15 分钟快速市场（专项工具链）

市场数据源：Gamma `/events/keyset?slug=btc-updown-5m-<ts>&...`（**不在 /markets 列表**）。

```bash
# 1) 扫描当前窗口全部币种盘口 + 套利检测
.venv/bin/python scripts/scan_updown.py

# 2) 综合监控器（ARB/DIP/TAIL 检测 + 累积 CSV）
.venv/bin/python scripts/monitor_updown.py --interval 15 --rounds 36

# 3) LLM 模拟测算：最新盘口 → LLM 判断 → 模拟成交（$1/笔）→ 等结算 → 盈亏
.venv/bin/python scripts/simulate_llm_updown.py --wait 480

# 4) 守护进程挂机（推荐）：持续后台执行 + 统一日志 + 崩溃自动重启
.venv/bin/python scripts/run_daemon.py start   # 无限轮（每 5m 窗口一轮，窗口内 30s 扫描）
.venv/bin/python scripts/run_daemon.py status  # 运行状态 + 最新统计
.venv/bin/python scripts/run_daemon.py stop    # SIGTERM 优雅停止

# 4.5) 常驻结算进程：主任务只开单，结算由它独立持续处理（任务退出后继续结算）
#       run_llm_loop / run_daemon 启动时自动拉起，一般无需手动启动
.venv/bin/python scripts/settle_worker.py status  # 查看运行状态 + pending 单数
.venv/bin/python scripts/settle_worker.py stop    # 停止常驻结算（pending 会保留）
# 待结算队列存于本地 MySQL：polytrader.pending_trades 表（多进程共享，不随任务删除）
# MySQL 连接参数可用 POLY_DB_HOST/PORT/USER/PASS/NAME 环境变量覆盖（默认 127.0.0.1:3306 root 空密码）

# 5) 补结算（仅当结算进程未运行时需要手动补查，幂等）
.venv/bin/python scripts/backfill_settlements.py --results <results.jsonl>

# 6) 多轮循环测试（不对齐整点、窗口内 30s 扫描、开单即交常驻结算，测试/临时用，长跑建议用 4）
#    常用参数：--scan-interval 30（窗口内扫描间隔）--stop-before 40（窗口结束前停止秒数）
.venv/bin/python scripts/run_llm_loop.py --rounds 20 --out-dir backtest_results
```

### Web 统计面板

```bash
.venv/bin/python scripts/web_dashboard.py --port 8787   # 浏览器打开 http://127.0.0.1:8787
```

- **统一结果展示**：胜率 / 总盈亏 / 收益率 / 胜-负 / 投入 卡片 + 累计盈亏 SVG 曲线 +
  分币种、分方向统计 + 交易明细表（5s 自动刷新，无外部依赖）
- 数据源自动选择**最新守护会话**（`logs/llm_daemon_*/llm_results.jsonl`），回退到
  `backtest_results/llm_results_*.jsonl`
- 接口：`/`（页面）、`/api/stats`（聚合 JSON）、`/api/sessions`（会话列表）

### 守护进程输出规范（单目录，一处看全部日志）

```
logs/llm_daemon_<ts>/          ← 每次 start 一个会话目录
├── daemon.log                 ← 全部输出（轮次/扫描/信号/错误/心跳）集中查看
├── llm_results.jsonl          ← 结果事件流（round / trade_settled / heartbeat / summary）
├── audit_all.jsonl            ← 调用级审计（http_request / llm_call / trade_open）
├── status.json                ← 实时快照（rounds/trades/win_rate/pnl），status 命令读取
├── llm_loop_*.log             ← 每轮 run_llm_loop 明细
├── seen_slugs.txt             ← 已交易盘口去重（跨轮持久，每盘口只开一单）
└── rounds_tmp/                ← 每轮临时产物（合并后清理）
```

**关键行为约束**：
- 每盘口只开一单（seen-file 持久去重）
- 固定仓位 `--size`（默认 $1/笔）
- 窗口过滤：剩余 <30s 的窗口跳过；`endDate` 按 **UTC** 解析（勿按本地时区）
- 币种白名单：BTC / ETH / BNB / SOL / HYPE

### AI 模型训练与回测

```bash
# 训练（从已解决市场取标签）
.venv/bin/python scripts/train_ai.py --samples 2000 --proxy http://127.0.0.1:7897

# 模型质量评估（区分度 + 校准分桶）
.venv/bin/python scripts/run_polytrader.py backtest --samples 500 --proxy http://127.0.0.1:7897
```

**回测局限（诚实声明）**：当前 backtest 为 in-sample 评估（历史价格特征 + 结算标签），
accuracy/校准曲线偏乐观。真实可交易回测需滚动时间切片（train 窗口 → 预测未来市场），
属二期工作。

## 实测发现与结论（截至 2026-08-12）

> 📖 日常运维请直接看 **[docs/RUNBOOK.md](docs/RUNBOOK.md)**（守护进程/面板/日志/故障排查完整手册）；
> 策略调研详见 [docs/updown-strategies-report.md](docs/updown-strategies-report.md)。

要点：

1. **updown 市场已上线**：7 币 × 5M/15M 滚动窗口，数据在 `/events/keyset`；
   盘口为空壳（bid 0.001-0.01 / ask 0.99+），taker 不可行、只能 maker 限价单
2. **共识价和精确 1.000**：14/14 市场，静态二元套利无机会
3. **跨窗口价差不是错价**：5m 与 15m 窗口起点/终点不同，"涨"锚定各自起点价，
   P(15m涨) ≥ P(5m涨) 不成立
4. **结算锚 = Chainlink 30s TWAP**（2026-08-07 升级，实测一致性 72.7% vs 瞬时 54.5%）：
   "Binance 瞬时价滞后套利"已失效，新 edge = TWAP 预测精度
5. **结算推价操纵已关闭**（斯坦福论文 $8.2M 事件后 TWAP 生效）
6. **LLM 判断无稳定 alpha（诚实结论）**：早期 20 轮 9 笔 8 结算 4W/4L（50%，-$0.55）；
   高频扫描后 10 轮 11 笔 7W4L 63.6% +$7.54、daemon 5 轮 3 笔 3W0L +$2.80——
   样本仍小，盈亏在运气范围内波动（首次 LLM 输出退化经 prompt 加固后修复）
7. **收敛/确定性折价无利可图**：尾段 ≥0.9 侧买入胜率 100% 但折价仅 0.1-0.2%
8. **CLOB orderMinSize=5 shares（2026-08-14 实测）**：`--size $1` 在价格 >0.2 时
   份额 <5 被拒单（`Size (1.54) lower than the minimum: 5`）；空壳盘口过滤
   （预期成交价 ∉ [0.20, 0.85]）在 $1/笔下拦截了绝大部分信号。待办见 docs/RUNBOOK.md 9.5

## 配置

- `config/config.yaml`：全部参数（策略开关、风控阈值、执行参数）
- `.env`（复制自 `.env.example`）：Polymarket 凭证、代理、LLM key
  （`LLM_API_KEY` / `LLM_BASE_URL=https://opencode.ai/zen/go/v1` /
  `LLM_MODEL=deepseek-v4-flash`；OpenAI 兼容端点，实测直连可达）
- 环境变量覆盖：`POLY_RISK__MAX_DAILY_LOSS_USD=50`（`POLY_` + `__` 分隔路径）

## 风控清单

单标的上限 / 总敞口上限 / 日亏损熔断 / 最大回撤熔断 / 持仓数上限 /
价格带过滤（0.03-0.97）/ 同市场冷却 / Kelly 分数仓位（默认 0.25）/ 套利组原子性。

## 已知环境问题

- macOS 缺 `libomp` 时 lightgbm 无法加载（OSError），系统自动 fallback 到
  sklearn HistGradientBoosting（无需 OpenMP）；装好 libomp 后自动恢复 lightgbm
  （`brew install libomp` 后仍需代理通畅）
- Polymarket 私有排行榜 API 不可用，跟单数据源采用市场成交聚合替代
- Binance 无 `HYPEUSDT`：HYPE 行情自动 fallback OKX
- gamma-api 偶发 `SSL EOF` 瞬时抖动（分钟级）：扫描失败不中断循环、settle_worker
  自动重试，网络恢复即自愈，无需干预
- 本机无本地代理：python requests 直连 Polymarket/OKX 实测可用；若将来需要代理，
  可在 `HttpClient(proxy=...)` 处配置（注意 scripts 中 `http://127.0.0.1:7897`
  为历史默认值，无 PySocks 时实际静默直连）
- MySQL 8.0.46 需手动启动（见快速开始）；机器重启后 `settle_worker status` 报
  DB 错误时先启动 mysqld

## 测试

```bash
.venv/bin/python -m pytest tests/        # 187 个测试（含端到端离线全链路、审计、reason 解析）
```
