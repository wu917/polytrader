# PolyTrader TODO

记录时间：2026-08-17 00:10（用户提出，尚未开始）

## 1. 开单数据核对增强（保证数据准确性）

- [ ] 从 Polymarket 拉取历史交易记录（CLOB `/data/trades` / data-api 交易接口）
- [ ] 与 DB 中 `pending_trades` 订单对比
- [ ] **Poly 历史交易单独存新表**（如 `poly_trades`），后续与 `pending_trades` 定期对比
- [ ] 对比不一致时告警/修复（数据准确性兜底）

## 2. 分类型下单存储评估（仍汇总到 pending_trades）

- [ ] 评估按订单类型分表存储：分钟级盘口（updown 5m/15m）、赛事跟单（copytrade）、
      股票/商品日级（equity）等
- [ ] 不同类型后续新增也纳入该设计
- [ ] **同时必须汇总保存到当前 `pending_trades` 表**（统一查询/结算入口）

## 3. 模拟/实盘存储与配置管理评估

- [ ] 评估模拟（paper/simulate）与实盘（live）**存储库区分**（数据隔离）
- [ ] 评估接入 nacos 等统一配置管理机制（替代/补充当前 `config.yaml` + 热文件方案）
