---
name: live-trading-safety
description: PolyTrader 实盘交易安全规则（真实资金护栏、验证禁令、$1 损失教训）。当操作涉及实盘脚本、下单函数、CLOB API、充值或真实资金时必须先加载本 skill。
---

# PolyTrader 实盘交易安全规则

> 血泪教训：2026-08-14 实盘脚本验证时直接调用下单函数，损失 $1。
> 从此立下铁律，任何验证不得触达真实资金操作。

## 铁律（违反即事故）

1. **验证禁令**：验证实盘脚本只允许 `python -m py_compile` / `--help` /
   `pytest` / 静态走查 / **mock 下单函数**。哪怕 `--rounds 1 --size 1` 最小参数，
   LLM 一出信号就会真实下单扣款。
2. **真实下单必须用户单独确认**：金额、市场、授权三项都要用户明确点头。
3. **临时脚本不得 import 或调用**：place_fok / place_maker / order_v2 下单入口 /
   fund_deposit 充值 / eth_sendRawTransaction。
4. 充值/swap 类链上操作每次展示完整交易内容，等确认。

## 改动下单相关代码的流程

1. 先读项目 AGENTS.md 第 5 节（live 规则）
2. 改 `execution/signer.py` / `clob_client.py` / `order_v2.py` 下单逻辑后：
   跑 `tests/` 全量（187 个）+ 为改动补单测
3. 提交前触发 quant-guard agent 审查（.opencode/agents/quant-guard.md 护栏清单）
4. 保持"默认关闭 + 护栏"语义：live.enabled 默认 false、MAX_ORDER_USD 硬上限、
   价格带过滤不可绕过

## 关键事实（勿臆测）
- CLOB V2：pUSD 结算、deposit wallet 账户、POLY_* L2 HMAC、ERC-7739 签名
- 5m 盘 orderMinSize=5 shares、tick=0.01、FOK 最小 $1、每单手续费 ~$0.03
- updown 5m/15m 盘口常为空壳（bid 0.01/ask 0.99），maker 挂单也可能被吃
- 测试网 clob-staging 经当前代理不可达（000），主网经代理可达
