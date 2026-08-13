# PolyTrader

Polymarket 预测市场自动化交易系统（Python 3.11+）。模块化设计：套利 / AI 概率 / LLM 判断 /
聪明钱跟单四大策略 + 完整风控，支持 dry-run / paper / live 三种执行模式；含 updown 5/15 分钟
快速市场专项工具（扫描、监控、LLM 模拟测算、审计）。

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
.venv/bin/python -m pytest tests/        # 127 个测试
```

网络说明：python requests 可直连 Polymarket/OKX（中国大陆环境实测可用，无需代理）；
`curl` 对 Polymarket 返回 000 是 curl 自身问题，不代表网络不通。

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
│   └── llm_updown.py    # updown 市场 LLM 方向判断（实时行情 + 锚定修正）
├── ai/
│   ├── models.py        # 可插拔：lightgbm（需 libomp）/ sklearn fallback
│   ├── features.py      # 14+ 维特征（元数据/订单簿/价格动量/类别）
│   ├── train.py         # 标签提取（resolved 市场）+ 训练 + isotonic 校准
│   ├── convergence.py   # 收敛/确定性折价回测（尾段确定侧买入）
│   └── llm_scorer.py    # OpenAI 兼容 LLM 概率评分（含 reason 提取与审计）
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

scripts/
├── run_llm_loop.py      # 多轮循环主任务（窗口内扫描 + 开单写 MySQL）
├── settle_worker.py     # 常驻结算进程（任务退出后结算不停止）
├── run_daemon.py        # 无限挂机守护进程（复用 run_llm_loop --rounds 1）
├── backfill_settlements.py # 兜底补结算（settle_worker 未运行时才需要）
└── ...（scan/monitor/simulate/web_dashboard 等）
```

**存储**：已开单队列存于本地 MySQL（`polytrader.pending_trades` 表）——
`run_llm_loop`/`run_daemon` 开单写入，`settle_worker` 常驻轮询结算，多进程共享、不随任务删除。

## 使用

### 四类策略

| 策略 | 原理 | 关键参数 |
|---|---|---|
| 套利 | YES+NO ask 和 < 1-ε 同时买入；互斥候选概率和 < 1-ε 全买 | `arbitrage.min_edge` |
| AI 概率 | 特征 → 模型 P(YES) vs ask 价，edge ≥ ε 买入 | `ai_probability.min_edge` |
| LLM 盘口 | 订单簿上下文进 prompt，LLM 双侧评估 edge（DeepSeek 等） | `llm_book`（`scripts/run_polytrader.py llm`） |
| LLM updown | 实时行情（Binance/OKX）→ LLM 判断窗口方向 vs 市场 ref | `llm_updown`（见下） |
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

# LLM 盘口扫描（DeepSeek 双侧评估，真实市场）
.venv/bin/python scripts/run_polytrader.py llm --proxy socks5h://127.0.0.1:7890
```

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
.venv/bin/python scripts/train_ai.py --samples 2000 --proxy socks5h://127.0.0.1:7890

# 模型质量评估（区分度 + 校准分桶）
.venv/bin/python scripts/run_polytrader.py backtest --samples 500 --proxy socks5h://127.0.0.1:7890
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

## 配置

- `config/config.yaml`：全部参数（策略开关、风控阈值、执行参数）
- `.env`（复制自 `.env.example`）：Polymarket 凭证、代理、LLM key
  （`LLM_API_KEY` / `LLM_BASE_URL` / `LLM_MODEL=deepseek-v4-flash`）
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
  可在 `HttpClient(proxy=...)` 处配置（注意 scripts 中 `socks5h://127.0.0.1:7890`
  为历史默认值，无 PySocks 时实际静默直连）
- MySQL 8.0.46 需手动启动（见快速开始）；机器重启后 `settle_worker status` 报
  DB 错误时先启动 mysqld

## 测试

```bash
.venv/bin/python -m pytest tests/        # 127 个测试（含端到端离线全链路、审计、reason 解析）
```
