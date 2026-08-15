# AGENTS.md — PolyTrader 开发引导

本文件是后续所有开发的**强制遵循规则**：环境事实、架构定义、数据流、开发约定。
改动前先读本文件；与本文件冲突的改动需先更新本文件再实施。

---

## 1. 项目是什么

Polymarket 预测市场的 **updown 5m/15m 快速市场模拟交易工具链**（LLM 方向判断 + 成交 + 结算统计）。
**默认以 dry-run/paper 模拟为主**（收益率回测必须用模拟模式）；live 实盘已实现
（EIP-712 签名 + CLOB 下单）但**默认关闭**，启用需 `config.yaml` 显式 `live.enabled: true`，
且有不可绕过的安全护栏（见第 5 节）。

## 2. 本机环境事实（实测确认，勿臆测）

| 项 | 值 |
|---|---|
| Python | 3.11.15，venv 用 `.venv/bin/python` |
| 依赖 | `requirements.txt`（含 `pymysql`；LLM 用 requests 直连） |
| MySQL | 8.0.46 arm64，安装于 `../mysql-8.0.46/`（workspace 下，非 brew） |
| MySQL 连接 | `127.0.0.1:3306`，user `root`，密码见 `.env` 的 `POLY_DB_PASS`，库 `polytrader` |
| LLM | DeepSeek（`.env`：`LLM_API_KEY` / `LLM_BASE_URL=https://api.deepseek.com/v1` / `LLM_MODEL=deepseek-chat`） |
| 网络 | **必须走本地 HTTP 代理 `127.0.0.1:7897`**（.env `HTTP_PROXY`/`HTTPS_PROXY` 与代码 `_req` 均硬编码此端口）：requests 需显式 `proxies=`（默认 trust_env 有时不生效）；裸 TCP/curl 直连 Polymarket 全不通（000）。主网 CLOB/Gamma 经代理可达 |
| 网络抖动 | gamma-api 偶发 `SSL EOF` 抖动（几分钟级），扫描失败**不中断循环**，重试即可 |
| 测试网 | `clob-staging.polymarket.com` 经当前代理**不可达**（000）——测试网全链路验证需换可用代理节点 |

**⚠️ 2026-08-15 迁移到新机器后的环境差异（以本条为准，上文为旧沙箱记录）**：

- 仓库为**全新 clone，零运行产物**：无 `.venv/`、无 `.env`、无 `logs/`、无 `backtest_results/`、无 jsonl 结果，MySQL `polytrader` 库与 `pending_trades` 表是否已建**待确认**
- 本机无 `../mysql-8.0.46` 目录、无 mysql 客户端命令；但 **mysqld 已在 `127.0.0.1:3306` 运行**（PID 21368，来源未知，需 pymysql 或装客户端才能查库）
- 初始化待办：`~/.local/bin/python3.11 -m venv .venv` → `.venv/bin/pip install -r requirements.txt` → `cp .env.example .env`（填 POLY_DB_*、LLM key、代理）→ 确认 MySQL 建库建表 → 跑 `pytest` 验证
- 网络实测：Polymarket（gamma/clob）直连 000 **需代理** `http://127.0.0.1:7897`（代理客户端不常开，用时先探测）；DeepSeek API、mcp.context7.com、mcp.grep.app **直连可达**
- 项目级 opencode 配置已部署：`.opencode/agents/quant-guard.md`（实盘安全审查）+ `.opencode/skills/live-trading-safety/`（真实资金铁律），见全局记忆第 4 节
- 2026-08-15 已补齐：`.env`（旧项目拷贝 + 代理补 `http://127.0.0.1:7897`）、`logs/`（旧实盘日志）、`backtest_results/seen_slugs.txt`（84 条去重）、`scripts/migrate_pending_trades_schema.py`（旧表结构迁移，已执行）；代理端口 7890 → 7897 已全量替换（代码/README/AGENTS.md）

**沙箱限制（重要）**：只能写 workspace（`~/.reasonix/global-workspace/`）与 `/tmp`；
`/opt`、home 根目录、系统级命令（如 `ps`）被禁止。装软件只能在 workspace 内解压运行。

**运行中的常驻服务**：mysqld（3306）+ settle_worker（PID 文件 `settle_worker.pid`）。
机器重启后需手动启动 mysqld：
```
../mysql-8.0.46/bin/mysqld --basedir=../mysql-8.0.46 --datadir=../mysql-8.0.46/data \
  --port=3306 --bind-address=127.0.0.1 --socket=../mysql-8.0.46/mysql.sock \
  --pid-file=../mysql-8.0.46/mysql.pid --log-error=../mysql-8.0.46/mysql.err --daemonize
```

## 3. 架构与数据流（核心，改动前必读）

```
run_llm_loop.py（主任务，多轮循环）
 ├─ 每轮 = 一个 5m/15m 窗口：启动即扫描当前窗口（不对齐整点，enter mid-window）
 │    窗口内每 --scan-interval(30)s 扫一次，窗口结束前 --stop-before(40)s 停止
 ├─ 每次扫描 = subprocess 调 simulate_llm_updown.py --wait 0（只开单不等结算）：
 │    gamma-api 拉 5 币 updown 市场 → clob 盘口 → Binance/OKX 行情 → DeepSeek 评估 P(YES)
 │    → 双侧 edge = |llm_p − ref|，≥ min_edge(默认 0.04) 开单（$1/笔，ref 价成交）
 ├─ 开单 → INSERT 到 MySQL polytrader.pending_trades（幂等，trade_id 去重）
 └─ 启动时自动拉起 settle_worker（已运行则跳过）；退出不等待结算、不影响结算

settle_worker.py（常驻独立进程，任务停止后结算不停止）
 ├─ start/stop/status/run 子命令；fork+setsid 后台化，PID 文件 settle_worker.pid
 ├─ 每 --poll(30)s 轮询 MySQL：SELECT * FROM pending_trades WHERE status='pending'
 └─ 结算成功 → 写 trade_settled 事件回 results_file（文件被删则降级同目录 llm_results.jsonl）
      → UPDATE status='settled', settle_yes/win/pnl/settled_at

run_daemon.py（无限挂机）：每轮复用 run_llm_loop --rounds 1；结算交给 settle_worker
backfill_settlements.py（兜底补结算）：仅当 settle_worker 未运行时才需要手动跑

实盘循环脚本（真实资金，默认关闭，需用户显式确认）：
run_live_loop.py（5m 加密 updown）：每轮一个 5m 窗口，窗口内 --scan-interval 扫、
  --stop-before(80s) 停；信号过坏单过滤[0.20,0.85] + 盘口预检 + verify_token(60s 缓存)
  + 窗口剩余 <30s 闸后走 **FOK 吃单**（marketable，--fok-slip 滑点容忍封顶 0.85），
  成交后 mark_filled 实际成交价
run_event_live_loop.py（通用事件盘）：maker GTC，--wait/--poll 轮询、未成交撤单
run_equity_live_loop.py（股票/商品日级）：FOK 吃单
下单精度（官方规则，order_v2.calc_amounts 实现）：tick=0.01 → 价格 2 位小数、
份额 2 位（BUY 向上取整保证隐含价精确落 tick）、USD 4 位小数；5m 盘
orderMinSize=5 shares、orderPriceMinTickSize=0.01（book API 返回）
```

**开单条件（完整判定链）**：ref 价可解析 → 价格带 `0.03 ≤ ref ≤ 0.97` → 行情上下文拉取成功
→ 窗口剩余 `secs_left ≥ 20s` → LLM 评分 → `yes_edge = p−ref_yes` / `no_edge = (1−p)−ref_no`
→ 任一侧 ≥ min_edge 开单 → `seen_slugs.txt` 跨轮去重（每 slug 只开一单）。

## 4. 数据库规则

库 `polytrader`，表 `pending_trades`（字段含中文注释，`SHOW FULL COLUMNS` 可查）：
`trade_id`(PK) / `slug` / `coin` / `window`(5m|15m) / `side`(YES|NO) / `entry_price` / `size_usd`
/ `round` / `results_file` / `status`(pending|settled) / `settle_yes`(1.0=涨 0.0=跌) / `win` / `pnl` / `created_at` / `settled_at`

- 连接参数：`polytrader/db.py` 默认值（`POLY_DB_*` 环境变量可覆盖）；代码连库一律走 `polytrader.db` 模块
- **mysql 客户端操作中文必带 `--default-character-set=utf8mb4`**，否则中文乱码（已踩坑）
- 数据读写用 `polytrader/db.py`：`insert_pending` / `fetch_pending` / `mark_settled` / `count_pending`
- DECIMAL 列经 pymysql 读出是 `Decimal`，运算前必须转 `float`（已踩坑）

## 5. 开发与验证约定

- 代码注释、提交说明用中文；标识符、文件名、命令用英文
- 回复用户用简体中文；思考也保持中文
- 改 `run_llm_loop.py` 时同步检查 `run_daemon.py`（它 subprocess 调 run_llm_loop 且传参数）与 `settle_worker.py`（共享 MySQL pending）
- 验证三件套：`python -m py_compile <改动文件>` + `.venv/bin/python -m pytest tests/`（186 个）
  + 实跑观察关键日志（无对齐等待 / 30s 间隔 / 窗口结束前 40s 停 / [db] 入库 / settle worker ensured）
- 长任务（多轮窗口）用 `run_in_background` 后台跑，按每轮 5-10 分钟估算耗时
- `run_llm_loop` 的 SUMMARY 是"已结算部分"——结算由 settle_worker 异步追加到结果文件，看完整收益率需 `settle_worker status` 显示 pending=0 后重读结果文件
- 凭证/密钥只进 `.env`（已 gitignore），绝不写进代码或提交；`.env` 中已有：DeepSeek key、`POLY_DB_*`
- **live 实盘规则**：`config.yaml` 的 `live.enabled` 默认 false 且**禁止 env 覆盖**
  （在 ENV_PROTECTED_PATHS）；改 `execution/signer.py` / `clob_client.py` 下单逻辑后必须
  跑 `tests/` 全量 + 新增/更新对应单测；任何实盘改动默认保持"默认关闭 + 护栏"语义
- **实盘脚本验证禁止真实下单（已踩坑，$1 损失）**：`run_live_loop.py` /
  `run_event_live_loop.py` / `run_equity_live_loop.py` / `verify_live_*.py` 是真实交易
  入口——即使 `--rounds 1` / `--size 1` 最小参数，LLM 一出 edge 信号就走 place_fok/
  place_maker 真实下单、扣 deposit wallet 的 pUSD。**任何临时验证脚本不得直接调用下单
  函数**；验证只做 py_compile / --help / pytest / 静态走查 / mock 下单函数。需要真实
  跑盘前必须用户单独确认金额与授权。教训细节见全局记忆「实盘脚本验证禁止真实下单」

## 6. 常用命令速查

```bash
# 主任务（多轮测试）
.venv/bin/python scripts/run_llm_loop.py --rounds 3 --windows 5m --out-dir backtest_results --min-edge 0.04

# 常驻结算
.venv/bin/python scripts/settle_worker.py status|start|stop

# 无限挂机
.venv/bin/python scripts/run_daemon.py start|status|stop

# 测试
.venv/bin/python -m pytest tests/ -q

# MySQL
../mysql-8.0.46/bin/mysql -h127.0.0.1 -P3306 -uroot -p<密码> --default-character-set=utf8mb4 polytrader
```
