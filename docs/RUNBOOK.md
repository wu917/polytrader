# PolyTrader 操作手册（RUNBOOK）

> 面向日常运维：启动/停止、日志查看、数据复盘、故障排查。版本 2026-08-13。

## 1. 快速命令速查

| 操作 | 命令 |
|---|---|
| 启动守护挂机 | `.venv/bin/python scripts/run_daemon.py start` |
| 查看运行状态 | `.venv/bin/python scripts/run_daemon.py status` |
| 停止守护挂机 | `.venv/bin/python scripts/run_daemon.py stop` |
| 启动 Web 面板 | `.venv/bin/python scripts/web_dashboard.py --port 8787` |
| 打开面板 | 浏览器 → `http://127.0.0.1:8787` |
| 看全部日志 | `tail -f logs/llm_daemon_*/daemon.log` |
| 补结算（幂等） | `.venv/bin/python scripts/backfill_settlements.py --results logs/llm_daemon_*/llm_results.jsonl` |
| 收益率统计 | `.venv/bin/python scripts/summarize_rounds.py` |
| 运行测试 | `.venv/bin/python -m pytest tests/`（127 个） |

## 2. 守护进程（run_daemon.py）

### 2.1 启动参数

```bash
.venv/bin/python scripts/run_daemon.py start \
  --min-edge 0.04        # LLM 与市场隐含价最小分歧（开单门槛）
  --windows 5m           # 参与窗口：5m / 15m / 5m,15m
  --scan-interval 60     # 窗口内扫描间隔秒（0=每窗口 1 次）
  --settle-wait 180      # 窗口结束后等待结算秒数
  --size 1.0             # 每笔固定仓位 USD
  --max-rounds 0         # 0=无限（默认）；>0 跑完自动退出（测试用）
```

### 2.2 行为保证

- **持续执行**：fork + setsid 真守护进程，无限循环；每轮 = 一个 5m 窗口
  （对齐窗口起点 → 窗口内高频扫描 → 窗口结束后等待结算 → backfill 补结算）
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
| `daemon.log` | 全部输出：轮次/扫描/信号/LLM 错误/结算/心跳 | **日常看这个** |
| `llm_results.jsonl` | 事件流：`round`（每次扫描）/`trade_settled`/`heartbeat`/`summary` | 统计与复盘 |
| `audit_all.jsonl` | 调用审计：`http_request`（URL/状态/耗时）/`llm_call`（prompt/概率/reason/usage）/`trade_open` | 排查问题 |
| `status.json` | 最新快照（status 命令读取） | 面板/脚本读取 |
| `llm_loop_*.log` | 每轮 run_llm_loop 明细（同目录，冗余） | 深入排查 |
| `seen_slugs.txt` | 已交易盘口 | 去重记录 |

### 3.1 事件格式（llm_results.jsonl）

```json
{"type": "round", "round": 1, "trades": [...], "evaluations": [...], "config": {...}}
{"type": "trade_settled", "trade_id": "39a998e3", "slug": "btc-updown-5m-1786603200",
 "coin": "btc", "window": "5m", "side": "NO", "entry_price": 0.625,
 "settle_yes": 0.0, "win": 1, "pnl": 0.6, "backfilled": true}
{"type": "heartbeat", "round": 3, "trades": 3, "settled": 3, "total_pnl": 2.8}
{"type": "summary", "rounds": 5, "trades": 3, "settled": 3, "wins": 3, "total_pnl": 2.8}
```

`backfilled: true` = 等待期未结算、由 backfill 事后补查入账。

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

### 5.1 补结算（重要）

守护进程在窗口结束后等 `--settle-wait`（默认 180s）再查结算——绝大多数单子能查到；
若某单结算延迟，会在 results 中保持 `pnl: null`。**事后**运行：

```bash
.venv/bin/python scripts/backfill_settlements.py --results logs/llm_daemon_*/llm_results.jsonl
```

幂等：已入账的 trade_id 不会重复结算（按 trade_id 去重）。输出 `backfill_<ts>.csv` 明细。

### 5.2 收益率统计

```bash
.venv/bin/python scripts/summarize_rounds.py
```

汇总所有已结束会话（$1 统一口径）：各轮交易数/结算数/胜率/投入/收益率。

### 5.3 手工核对

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
| daemon.log 大量 `ERR request ... Connection reset` | 本地代理 127.0.0.1:7890 故障 | 检查 Clash 节点；代理恢复后守护自动重试，无需干预 |
| `ERR LLM content unparsable` | DeepSeek 瞬时返回空 content | 正常现象（有重试）；若连续出现检查 `LLM_API_KEY` 余额 |
| 结算一直 `still unsettled` | 结算延迟 > 查询窗口 | 等 5-10 分钟后重跑 backfill（幂等安全） |
| 面板显示"暂无结果数据" | 最新会话还没有任何轮次完成 | 等首个窗口轮次结束（≤8 分钟） |
| lightgbm 加载失败（OSError/libomp）| macOS 缺 libomp | 自动 fallback sklearn，无需处理；装 `brew install libomp` 恢复 |
| `windows: 0 markets` | 窗口刚创建 keyset 未返回/已结束窗口被过滤 | 正常（下一窗口自动重扫） |
| 某轮 0 交易 | edge 全部 < min_edge（LLM 与市场无分歧）| 正常，非故障 |

### 6.1 代理故障的完整恢复路径

1. 修复代理（切换 Clash 节点等）
2. 守护进程自动重试（每轮最多 3 次/请求），**无需重启守护**
3. 若某窗口因代理失败未扫描/未结算：`backfill_settlements.py` 补结算；
   错过的扫描窗口无法补（窗口已结算），属正常损耗

## 7. 安全边界（务必遵守）

- **live 真实下单被安全闸拦截**：`execution/broker.py` 的 `LiveBroker` 明确拒绝执行，
  需要 EIP-712 订单签名实现后才能打开（当前全部为 dry-run/paper 模拟）
- **仓位 $1/笔**：默认最小仓位验证信号，放大仓位前至少累积 100+ 结算样本
- **`seen_slugs.txt` 勿删**：删除会导致已交易盘口重新开单（同一窗口重复交易）
- **面板与守护进程的端口**：8787 默认；冲突时 `--port` 换端口

## 8. 例行巡检清单（每日）

1. `.venv/bin/python scripts/run_daemon.py status` —— 确认 RUNNING、看 pnl 趋势
2. `tail -30 logs/llm_daemon_*/daemon.log` —— 看最近轮次是否有异常
3. 浏览器打开 `http://127.0.0.1:8787` —— 确认面板数据在更新
4. 若连续多轮 0 交易：检查 min_edge 是否过高或 LLM 服务异常（audit_all 中 llm_call 占比）
5. 每周：跑 `summarize_rounds.py` 汇总收益率，评估策略是否继续
