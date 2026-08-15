# PolyTrader 操作手册（RUNBOOK）

> 面向日常运维：启动/停止、日志查看、数据复盘、故障排查。版本 2026-08-16。

## 1. 快速命令速查

| 操作 | 命令 |
|---|---|
| 启动守护挂机 | `.venv/bin/python scripts/run_daemon.py start` |
| 查看运行状态 | `.venv/bin/python scripts/run_daemon.py status` |
| 停止守护挂机 | `.venv/bin/python scripts/run_daemon.py stop` |
| 常驻结算进程：查看状态 | `.venv/bin/python scripts/settle_worker.py status` |
| 常驻结算进程：停止 | `.venv/bin/python scripts/settle_worker.py stop` |
| 多轮循环测试 | `.venv/bin/python scripts/run_llm_loop.py --rounds 3 --windows 5m` |
| 5m 加密盘实盘循环 | `PYTHONPATH=. .venv/bin/python scripts/run_live_loop.py --rounds 3` |
| 跟单循环（paper 模拟，默认） | `.venv/bin/python scripts/run_copytrade_loop.py --rounds 5 --log logs/copytrade.log` |
| 跟单循环（实盘 FOK，需授权） | `.venv/bin/python scripts/run_copytrade_loop.py --live --max-live-orders 2 --rounds 5` |
| 事件盘实盘循环（maker） | `PYTHONPATH=. .venv/bin/python scripts/run_event_live_loop.py --size 1 --min-edge 0.05` |
| 股票/商品盘日级实盘循环 | `PYTHONPATH=. .venv/bin/python scripts/run_equity_live_loop.py --size 1 --min-edge 0.05` |
| 股票/商品盘日级模拟回测 | `PYTHONPATH=. .venv/bin/python scripts/simulate_equity_updown.py --size 100` |
| 事件盘/股票盘扫描评估 | `.venv/bin/python scripts/scan_event_markets.py` / `scripts/scan_equity_updown.py` |
| 启动 Web 面板 | `.venv/bin/python scripts/web_dashboard.py --port 8787` |
| 打开面板 | 浏览器 → `http://127.0.0.1:8787` |
| 看全部日志 | `tail -f logs/llm_daemon_*/daemon.log` |
| 补结算（仅 settle_worker 未运行时需要） | `.venv/bin/python scripts/backfill_settlements.py --results logs/llm_daemon_*/llm_results.jsonl` |
| 收益率统计 | `.venv/bin/python scripts/summarize_rounds.py` |
| 查询待结算队列 | `.venv/bin/python scripts/settle_worker.py status`（pending trades 数） |
| 运行测试 | `.venv/bin/python -m pytest tests/`（187 个） |

> **结算分工**：`run_llm_loop` / `run_daemon` / `*_live_loop` / `simulate_*` 只负责开单
> （写入 MySQL `pending_trades`），**结算由常驻进程 `settle_worker` 独立处理**——
> 任务退出后结算不停止，一般无需手动 backfill。

## 2. 守护进程（run_daemon.py）

### 2.1 启动参数

```bash
.venv/bin/python scripts/run_daemon.py start \
  --min-edge 0.04        # LLM 与市场隐含价最小分歧（开单门槛，默认 0.04）
  --windows 5m           # 参与窗口：5m / 15m / 5m,15m
  --scan-interval 30     # 窗口内扫描间隔秒（默认 30；0=每窗口 1 次）
  --settle-wait 180      # [已弃用] 结算由 settle_worker 常驻处理，此参数不再生效
  --size 1.0             # 每笔固定仓位 USD
  --max-rounds 0         # 0=无限（默认）；>0 跑完自动退出（测试用）
```

### 2.2 行为保证

- **持续执行**：fork + setsid 真守护进程，无限循环；每轮 = 一个 5m 窗口
  （启动即扫描当前窗口、**不对齐整点**；窗口内每 30s 扫描一次，窗口结束前 40s 停止；
  结算由常驻 `settle_worker` 处理，守护进程不等待结算）
- **崩溃自愈**：轮次异常捕获 + 指数退避重试（10s → 300s 封顶），连续失败在
  `daemon.log` 可观测，不中断整体循环
- **优雅停止**：`stop` 发 SIGTERM，当前轮结束（最多等一轮时长）后退出；
  超过 10s 未退出则 SIGKILL
- **每盘口一单**：`seen_slugs.txt` 跨轮持久去重，同一 slug 永不重复开单

### 2.3 status 输出解读

```
daemon RUNNING pid=12340 (pid file: .../logs/llm_daemon.pid)
  session: llm_daemon_20260813_143619
  rounds: 5  trades: 3  settled: 3  win_rate: 100.0%  pnl: $2.8
  last_event: 2026-08-13T15:27:53  last_error: None
```

- `session`：当前会话目录名（每次 start 新建）
- `rounds`：已完成的窗口轮数（≠ 扫描次数）
- `trades/settled/win_rate/pnl`：会话累计统计（实时）
- `last_error`：最近一次轮次错误（None = 正常）

## 3. 日志与数据规范（一处看全部）

每次 `start` 产生一个会话目录 `logs/llm_daemon_<ts>/`：

| 文件 | 内容 | 用途 |
|---|---|---|
| `daemon.log` | 全部输出：轮次/扫描/信号/LLM 错误/心跳 | **日常看这个** |
| `llm_results.jsonl` | 事件流：`round`（每次扫描）/`trade_settled`/`heartbeat`/`summary` | 统计与复盘 |
| `audit_all.jsonl` | 调用审计：`http_request`（URL/状态/耗时）/`llm_call`（prompt/概率/reason/usage）/`trade_open` | 排查问题 |
| `status.json` | 最新快照（status 命令读取） | 面板/脚本读取 |
| `llm_loop_*.log` | 每轮 run_llm_loop 明细（同目录，冗余） | 深入排查 |
| `seen_slugs.txt` | 已交易盘口 | 去重记录 |

**结算相关（不在会话目录）**：

| 项 | 位置 | 说明 |
|---|---|---|
| 待结算队列 | MySQL `polytrader.pending_trades` 表 | 所有任务共享；`settle_worker status` 看 pending 数 |
| settle_worker 日志 | `logs/settle_worker.log` | 结算进程的轮询与入账记录 |
| settle_worker PID | `settle_worker.pid`（仓库根） | 常驻进程状态 |

### 3.1 事件格式（llm_results.jsonl）

```json
{"type": "round", "round": 1, "trades": [...], "evaluations": [...], "config": {...}}
{"type": "trade_settled", "trade_id": "39a998e3", "slug": "btc-updown-5m-1786603200",
 "coin": "btc", "window": "5m", "side": "NO", "entry_price": 0.625,
 "settle_yes": 0.0, "win": 1, "pnl": 0.6, "backfilled": true}
{"type": "heartbeat", "round": 3, "trades": 3, "settled": 3, "total_pnl": 2.8}
{"type": "summary", "rounds": 5, "trades": 3, "settled": 3, "wins": 3, "total_pnl": 2.8}
```

`backfilled: true` = 由 settle_worker（或 backfill）事后补查入账——结算通常发生在
主任务退出后，事件由常驻结算进程异步追加到结果文件。

### 3.2 审计事件格式（audit_all.jsonl）

```json
{"event": "http_request", "ts": "...", "method": "GET", "url": "https://gamma-api...",
 "status": 200, "ms": 123, "size": 4567}
{"event": "llm_call", "model": "deepseek-v4-flash", "prompt_kw": {"slug": "btc-...", "side": "YES"},
 "probability": 0.47, "reason": "...", "ms": 812, "usage": {"prompt_tokens": 420, "completion_tokens": 45}}
{"event": "trade_open", "trade_id": "...", "slug": "...", "side": "NO", "llm_p": 0.30,
 "ref": 0.625, "edge": 0.075, "size_usd": 1.0, "entry_price": 0.625}
```

## 4. Web 面板

```bash
.venv/bin/python scripts/web_dashboard.py --port 8787
# 浏览器: http://127.0.0.1:8787 （5s 自动刷新）
```

- **数据源选择**：最新守护会话（`logs/llm_daemon_*/llm_results.jsonl`）→ 若无则回退
  到 `backtest_results/llm_results_*.jsonl`（旧挂机）
- **展示**：胜率/总盈亏/收益率/胜-负/投入 卡片；累计盈亏 SVG 曲线；分币种、分方向表；
  交易明细（轮次/盘口/方向/入场价/结算/结果/PnL，backfill 标注"补"）
- **接口**：`/api/stats`（聚合 JSON）、`/api/sessions`（会话列表）、`/`（页面）
- 面板只读，不触碰任何交易状态；与守护进程可独立启停

## 5. 数据复盘

### 5.1 结算（常驻自动，一般无需手动）

- **自动**：常驻 `settle_worker` 每 30s 轮询 MySQL 待结算队列，结算完成即写回
  结果文件并更新 DB 状态（`settle_worker status` 显示 `pending trades` 数）
- **结算查询两级兜底**（`fetch_settle`，settle_worker 与 backfill 共用）：
  先 `events/keyset?slug=` 主市场路径；查空时回退 `GET /markets?slug=&closed=true`
  直查——**已结算的衍生盘市场（`-1pt5`/`-away` 后缀）默认不返回，必须带
  `closed=true`**（2026-08-15/16 实测修复：此前衍生盘单永久 pending 占持仓名额）
- **结果文件缺失也结算**：`results_file` 已删（如 live 单未建结果文件）时仍执行
  `mark_settled` 更新 DB，不再永久 pending 空转（2026-08-15 修复）
- **主任务退出后结算不停止**：run_llm_loop/run_daemon 退出不影响结算，单子一直在队列里
- **何时需要手动 backfill**：仅当 settle_worker 未运行（如未启动/被停）且需要补结算时：
  ```bash
  .venv/bin/python scripts/backfill_settlements.py --results logs/llm_daemon_*/llm_results.jsonl
  ```
  幂等：已入账的 trade_id 不会重复结算（按 trade_id 去重）。输出 `backfill_<ts>.csv` 明细。

### 5.2 待结算队列查询（MySQL）

```bash
# 用 settle_worker status 看 pending 数量
.venv/bin/python scripts/settle_worker.py status

# 直接查库（密码见 .env 的 POLY_DB_PASS；中文操作务必加 --default-character-set=utf8mb4）
../mysql-8.0.46/bin/mysql -h127.0.0.1 -P3306 -uroot -p"$POLY_DB_PASS" \
  --default-character-set=utf8mb4 polytrader \
  -e "SELECT trade_id, mode, slug, side, entry_price, status, pnl FROM pending_trades;"
```

`mode` 字段区分实盘（`live`）与模拟（`simulate`）单——`*_live_loop` /
`run_copytrade_loop --live` 写入 `live`，`simulate_*` / `run_llm_loop` /
跟单 paper 写入 `simulate`。`status` 枚举：`pending`（待结算）/ `settled` /
`cancelled`（实盘订单最终未成交释放占坑，见 9.8）。

> **MySQL 实例说明**：本机 MySQL 8.0.46 安装于 `../mysql-8.0.46/`（workspace 下），
> 库 `polytrader` 表 `pending_trades`。机器重启后需手动启动：
> ```bash
> ../mysql-8.0.46/bin/mysqld --basedir=../mysql-8.0.46 --datadir=../mysql-8.0.46/data \
>   --port=3306 --bind-address=127.0.0.1 --socket=../mysql-8.0.46/mysql.sock \
>   --pid-file=../mysql-8.0.46/mysql.pid --log-error=../mysql-8.0.46/mysql.err --daemonize
> ```
> 连接参数统一在 `.env` 的 `POLY_DB_*`（代码经 `polytrader/db.py` 读取，凭证不入库）。

### 5.3 收益率统计

```bash
.venv/bin/python scripts/summarize_rounds.py
```

汇总所有已结束会话（$1 统一口径）：各轮交易数/结算数/胜率/投入/收益率。

### 5.4 手工核对

```bash
# 看某会话全部结算
grep '"type": "trade_settled"' logs/llm_daemon_*/llm_results.jsonl

# 看某笔交易的 LLM 判断依据（reason）
grep '"event": "llm_call"' logs/llm_daemon_*/audit_all.jsonl | grep <trade_id 或 slug>
```

## 6. 故障排查

| 症状 | 原因 | 处理 |
|---|---|---|
| `status` 显示 stale pid | 守护被强杀（SIGKILL）| 直接重新 `start`（旧 pid 自动覆盖） |
| daemon.log 大量 `ERR request ... Connection reset` | 网络抖动/被墙 | 扫描失败不中断循环，自动重试；若持续，检查网络 |
| `ERR LLM content unparsable` | DeepSeek 瞬时返回空 content | 正常现象（有重试）；若连续出现检查 `LLM_API_KEY` 余额 |
| `settle_worker status` 显示 `pending trades` 不降 | 结算延迟或 gamma-api 抖动 | 常驻进程每 30s 自动重试，网络恢复即入账；等待即可 |
| 衍生盘单（`-1pt5`/`-away` 后缀）永久 pending | 已结算衍生盘不在 /markets 默认查询结果 | 已修复：`fetch_settle` 回退查询带 `closed=true`；仍有存量单可手动 `backfill_settlements.py` 补查 |
| copytrade 进程崩溃后静默无日志 | 崩溃前无 traceback 输出（stderr 被丢弃） | 查 `logs/copytrade_crash.log`（外层兜底自动写完整 traceback，含 BaseException） |
| `[db] insert_pending FAILED` / `[db] fetch_pending FAILED` | MySQL 未启动/连接参数错误 | 检查 mysqld 是否运行、`.env` 的 `POLY_DB_PASS` 是否与库一致 |
| 结算一直 `still unsettled`（backfill 手动跑时） | 结算延迟 > 查询窗口 | 等 5-10 分钟后重跑 backfill（幂等安全） |
| 面板显示"暂无结果数据" | 最新会话还没有任何轮次完成 | 等首个窗口轮次结束（≤8 分钟） |
| lightgbm 加载失败（OSError/libomp）| macOS 缺 libomp | 自动 fallback sklearn，无需处理；装 `brew install libomp` 恢复 |
| `windows: 0 markets` | 窗口刚创建 keyset 未返回/已结束窗口被过滤 | 正常（下一窗口自动重扫） |
| 某轮 0 交易 | edge 全部 < min_edge（LLM 与市场无分歧）| 正常，非故障 |

### 6.1 网络故障的完整恢复路径

1. 网络恢复后，扫描与结算**自动重试**，无需重启任何进程
2. 结算由 settle_worker 常驻处理：即使 run_llm_loop/run_daemon 已退出，
   待结算单仍在 MySQL 队列中，网络恢复即自动入账
3. 若 settle_worker 也未运行：启动它（`settle_worker.py start`）即继续处理，
   或手动跑 `backfill_settlements.py` 补结算；错过的扫描窗口无法补（窗口已结算），属正常损耗

## 7. 安全边界（务必遵守）

- **live 实盘默认关闭**：EIP-712 签名 + CLOB 下单已实现，但 `config.yaml` 的
  `live.enabled` 默认 `false`（env 无法覆盖，只能改文件）。启用前必须 testnet 验证 +
  资金准备（见 README"实盘交易（live）"）
- **实盘护栏（不可绕过）**：无凭证拒绝 / 单笔 > `live.max_order_usd`（默认 $10）拒绝 /
  价格不在 [0.03, 0.97] 拒绝 / USDC 不足拒绝 / 下单前终端输入 `yes` 确认 /
  不自动重试下单 / 实盘部分成交**不自动回滚**（真实成交不可撤销）
- **坏单过滤**：实盘与模拟成交的预期成交价须在 **[0.25, 0.85]**，空壳盘口超范围则跳过，
  避免极端价格成交
- **paper 是默认推荐模式**：收益率回测、策略验证一律用 paper（真实取价模拟成交）
- **仓位 $1/笔**：默认最小仓位验证信号，放大仓位前至少累积 100+ 结算样本
- **`seen_slugs.txt` 勿删**：删除会导致已交易盘口重新开单（同一窗口重复交易）
- **面板与守护进程的端口**：8787 默认；冲突时 `--port` 换端口

## 8. 例行巡检清单（每日）

1. `.venv/bin/python scripts/run_daemon.py status` —— 确认 RUNNING、看 pnl 趋势
2. `.venv/bin/python scripts/settle_worker.py status` —— 确认 RUNNING、pending 数正常下降
3. `tail -30 logs/llm_daemon_*/daemon.log` —— 看最近轮次是否有异常
4. 浏览器打开 `http://127.0.0.1:8787` —— 确认面板数据在更新
5. 若连续多轮 0 交易：检查 min_edge 是否过高或 LLM 服务异常（audit_all 中 llm_call 占比）
6. 每周：跑 `summarize_rounds.py` 汇总收益率，评估策略是否继续

## 9. 实盘（CLOB V2）充值 + 下单操作

> 前提：`.env` 已配 `POLYMARKET_PRIVATE_KEY` / `POLYMARKET_DEPOSIT_WALLET` /
> `POLYMARKET_RELAYER_API_KEY(_ADDRESS)`。所有操作均为**真实资金/链上交易**，
> 执行前确认交易内容。

### 9.1 查余额（只读）
```bash
PYTHONPATH=. .venv/bin/python -c "
from polytrader.execution import chain
print('EOA pUSD:', chain.call_balance(chain.PUSD, '0x<你的EOA>')/1e6)
print('deposit pUSD:', chain.call_balance(chain.PUSD, '0x<deposit wallet>')/1e6)"
```

### 9.2 充值（Polygon USDC → pUSD 自动转入 deposit wallet）
```bash
PYTHONPATH=. .venv/bin/python scripts/fund_deposit.py --amount 1.10   # 充值 $1.10
PYTHONPATH=. .venv/bin/python scripts/fund_deposit.py --dry-run       # 只报价不广播
```
流程：approve USDC → Paraswap swap（USDC→pUSD，几乎 1:1）→ transfer pUSD → deposit wallet。
EOA 需留 ~1 POL 付 gas。官方桥 API（BSC 等跨链，最小 $5）：
```bash
curl -X POST https://bridge.polymarket.com/deposit -H "Content-Type: application/json" \
  -d '{"address": "<deposit wallet>"}'        # 拿桥地址
curl https://bridge.polymarket.com/supported-assets   # 确认支持链/最小额
curl "https://bridge.polymarket.com/status/<桥地址>"   # 查到账进度
```

**2026-08-16 修复（充值链路）**：
- Paraswap 为境外 API，请求走本地代理（`.env` 的 `HTTPS_PROXY`，缺省直连超时）
- `build_tx` 只传 `slippage`、**不传 destAmount**（v5 接口两者互斥，400
  "Cannot specify both"；且报价 destAmount 过期会导致 swap revert）——
  执行时按最新市场价 ± 容忍
- `wait_tx` 后校验 `status=1`：**revert 的交易不再误报成功**（此前报
  "confirmed ✓" 但实际链上失败）

### 9.3 下单验证（已由实盘实测 + 单测覆盖）

下单链路验证：`run_live_loop.py` 实盘实测（FOK 真实下单全链路）+
`tests/test_order_v2.py`（V2 订单构建/ERC-7739 签名/官方 SDK 交叉验证）。
原 `verify_live_order_v2.py` 已删除——无护栏直接真实下单的过时入口（教训见 AGENTS.md 第 5 节）。

**统一下单入口 `place_order`**（`scripts/run_live_loop.py`，跟单/5m/事件盘/股票盘
四个实盘循环共用）：下单前按 token 自动解析市场参数（各 600s 缓存）——
- `GET /tick-size`：tick=0.001 市场份额按 **5 位精度**计算（calc_amounts），
  保证隐含价落 tick 网格
- `GET /neg-risk`：负风险市场用 **NEG_RISK_EXCHANGE_V2** 合约签名
  （domain separator 不同），否则 CLOB 验签失败

两处修复对应 2026-08-16 实测的 `invalid POLY_1271 signature` 根因
（tick 精度错位 + negRisk 合约不匹配，此前部分事件盘下单必失败）。

### 9.4 查订单/持仓/结算
```bash
# 持仓（只读，无需认证）
curl "https://data-api.polymarket.com/positions?user=<deposit wallet>&limit=10"
# 结算：gamma 该市场 outcomePrices 变 0.0/1.0 即定盘
```

### 9.5 关键坑位（已踩过）
- tokenId 在 POST body 必须是**字符串**（大整数 int 序列化 → 400 Invalid order payload）
- 精度（官方规则）：tick=0.01 → 价格 ≤2 位小数、份额 ≤2 位小数（BUY 向上取整）、
  USD ≤4 位小数；FOK BUY 最小 $1（$0.99 会被拒）；5m 盘 orderMinSize=5 shares
  （<5 shares 会被拒）
- **TODO（待办）**：`orderMinSize=5 shares` 意味着 `--size $1` 在价格 >0.2 时
  份额必 <5，CLOB 直接拒单（实测 2026-08-14：Size (1.54) lower than minimum: 5）。
  修法二选一：① `--size` 默认提高到 $5（@0.5 得 10 shares）；② place_maker 前
  预检 `size/price < 5` → 跳过该信号并警告（避免每次拒单刷屏）。当前行为：
  拒单打印 `❌ 下单失败` 且不进 pending（无脏数据，仅浪费一次下单尝试）
- 每单有 ~$0.03 手续费（余额需覆盖 order + fee）
- ERC-7739 签名必须由 **EOA** 签嵌套 TypedDataSign（maker=signer=deposit wallet，
  verifyingContract=CTF Exchange V2）；与官方 SDK 逐字节一致（单测交叉验证）
- 链上广播：raw tx 需 0x 前缀；swap 交易 gas 需 estimate（300k 默认会 out of gas）
- RPC 走系统代理时偶发 SSL EOF → 已做多端点轮换容错

### 9.6 实盘循环脚本（四套）

| 脚本 | 盘面 | 执行方式 | 典型调用 |
|---|---|---|---|
| `run_live_loop.py` | 5m 加密 updown | **FOK 吃单**（--fok-slip 滑点容忍，默认 0.01） | `--rounds 3 --size 1 --min-edge 0.04 --fok-slip 0.01` |
| `run_event_live_loop.py` | 通用事件盘 | maker GTC（post_only） | `--size 1 --min-edge 0.05 --min-rr 1.5 --wait 600` |
| `run_equity_live_loop.py` | 股票/商品日级 | FOK 吃单 | `--size 1 --min-edge 0.05 --min-liquidity 200` |
| `run_copytrade_loop.py --live` | 全品类跟单 | FOK 吃单（镜像目标成交） | `--live --max-live-orders 2 --rounds 5` |

四者默认每轮最多 1 笔、$1/笔；成交写入 `pending_trades`（`mode='live'`），
由 `settle_worker` 自动结算。maker 单挂单后轮询订单状态（`--wait`/`--poll`/
`--wait-fill`），未成交自动撤单。坏单过滤：预期成交价须在 [0.20, 0.85]（跟单
为 [0.30, 0.90]），超范围跳过。**均为真实资金脚本**——验证阶段严禁直接运行
或调用其下单函数（见 AGENTS.md 第 5 节）。

### 9.7 实盘运行要点（run_live_loop）

```bash
# 启动 3 轮实盘（本机代理 7897；代理走 HTTPS_PROXY 环境变量，缺省回退 7897）
HTTPS_PROXY=http://127.0.0.1:7897 PYTHONPATH=. .venv/bin/python -u \
  scripts/run_live_loop.py --rounds 3 --min-edge 0.04 --size 1 --log logs/live_loop.log
```

**硬性风控（代码写死，不可覆盖）**：
- 单笔 `$1` 上限（`MAX_ORDER_USD=1.0`）；`--size` >1 或 ≤0 直接拒绝启动
- 同窗口 slug 只尝试一次（防重复开单）；每 2 轮复查余额，不足跳过
- 下单前 `verify_token`（GET /tick-size）校验，CLOB 侧无效跳过
- 坏单过滤 [0.20, 0.85] 按**吃单侧自身 ask** 预检（买 NO 看 NO 侧 ask，勿用
  1-bid 估算——空壳盘 bid/ask 不对称时失真，曾致 FOK 以 0.01 档成交，已修复）

**常见现象与处理**：
| 现象 | 原因 | 处理 |
|---|---|---|
| 0 成交（信号全被过滤） | updown 盘口长期空壳（bid 0.01/ask 0.99），坏单过滤 [0.20,0.85] 挡掉 | 正常；等盘口有真实流动性 |
| `invalid token id` | 5m 市场新建时 CLOB token 未生效（瞬态） | 已加 verify_token 自动跳过 |
| 撤单返回非 200（挂单疑似残留） | 市场已结算 → 订单 `CANCELED_MARKET_RESOLVED` | **资金自动释放**，无需人工处理 |
| 挂单 90s 未成交 | 空壳盘口无对手单 | 自动撤单，等下一窗口 |
| DeepSeek 偶发超时（30-90s） | 代理通道慢 | 自动重试（最多 3 次），主循环不崩 |

**结算查询**：成交单 `mode='live'` 入库，含 orderID/fill_price/LLM 建议
（llm_p/ref_price/edge/llm_reason/llm_model），settle_worker 自动结算：
```sql
SELECT trade_id, slug, side, entry_price, fill_price, llm_p, edge, win, pnl
FROM pending_trades WHERE mode='live' ORDER BY created_at DESC LIMIT 20;
```

### 9.8 跟单实盘（run_copytrade_loop --live，2026-08-16）

```bash
# 实盘试跑（⚠️ 需用户显式授权：FOK 真实下单，$1/笔，总上限 2 笔）
.venv/bin/python scripts/run_copytrade_loop.py --live --max-live-orders 2 --rounds 5

# 无限挂机（--rounds 0）
.venv/bin/python scripts/run_copytrade_loop.py --live --max-live-orders 5 --rounds 0

# paper 模式（默认，模拟成交不碰资金）：--rounds N / --poll 20 / --refresh-interval 1800
.venv/bin/python scripts/run_copytrade_loop.py --rounds 5 --log logs/copytrade.log
```

**开单决策链**：排行榜（data-api `/v1/leaderboard`，默认 MONTH/PNL，`--period`/
`--category`/`--top-n`/`--min-profit` 可选）→ 钱包画像 → `/activity?user=<wallet>`
活动流轮询（`--poll` 秒）→ 套利/冲单过滤 → 吃单侧 ask 预检 + 价格带
[0.30, 0.90]（`--min-buy-price`/`--max-buy-price`）→ FOK 下单（沿用
`run_live_loop.place_order`，自动 tick/negRisk 解析）→ 入库
`pending_trades`（`window='copytrade'`、`mode='live'`）→ settle_worker 结算。

**持仓上限与热更新**：
- `--max-live-orders`（默认 2）：实盘总开单硬上限，按 DB **未结算 live 跟单单**
  数控制，结算释放自动补单；paper 模式用 `--max-open-positions`（默认 10）
- **热更新（仅 live）**：运行中改 `logs/copytrade_limit.txt`（纯数字）即生效，
  无需重启——改小立即收紧（waiting）、改大立即放行；文件缺失/非法回退启动参数
- 每轮与轮内逐笔双重复查上限（防一轮内多信号突破）

**套利/冲单过滤（默认开启）**：同一钱包同一市场 `--wash-window`（默认 1800s）
内出现 SELL（买卖往返/冲单），或双 BUY 反向且双腿间隔 ≤ `--arb-gap`（默认
60s，防对冲误判）→ 该市场 BUY 全部过滤；`--no-wash-filter` 关闭。

**delayed 成交回填**：FOK 返回 `delayed`（排队确认中、无成交价）→ 登记
`pending_fills` → 每轮 `get_order_auth` 轮询 → MATCHED 回填 fill_price/tx
（`mark_filled` + `order_status='matched'`）；**600s 超时**仍未成交 → 最终
确认一次，MATCHED 则回填，否则 DB 置 `status='cancelled'` 释放占坑（避免
delayed 假单永久 pending 占持仓名额）。

**护栏**：单笔 $1 硬上限（`MAX_ORDER_USD`，`--live` 时 `--size` >1 拒绝启动）；
凭证只从 `.env`（缺 key 拒绝启动）；资金预检（pUSD 需覆盖 size + 手续费）；
崩溃 traceback 自动写 `logs/copytrade_crash.log`（含 BaseException，防 stderr
被 nohup 丢弃——2026-08-16 连续 3 次下单后静默死亡的排查即靠此文件定位
UnboundLocalError 并已修复）。

**常见现象与处理**：

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动即停（`资金不足`） | deposit wallet pUSD 不够 size + 手续费 | `fund_deposit.py` 充值后重跑（9.2） |
| 一直 `waiting: live pending N >= max M` | 未结算 live 单已满上限 | 结算释放自动恢复；或改 `logs/copytrade_limit.txt` 调大 |
| `filter(live): ... ∉ [0.30, 0.90]` | 吃单侧 ask 超价格带（空壳盘口） | 正常过滤，等有流动性 |
| `❌ 下单失败: invalid POLY_1271 signature` | 罕见残余：tick/negRisk 缓存过期 | 已修复自动解析；重启进程清缓存 |
| 进程静默死亡无日志 | 未捕获异常 | 查 `logs/copytrade_crash.log` 尾部 traceback |
