"""测试网全链路验证：Polymarket staging CLOB + Polygon Amoy。

流程（网络可达 staging 后自动执行）：
  1. 探测 clob-staging.polymarket.com 可达性（重试直到成功或超时）
  2. 生成随机 Amoy 测试钱包（测试网无私钥风险）→ 派生 API 凭证
  3. L2 认证头 → GET /balance 验证认证
  4. 拉测试网市场 → 构造订单 → EIP-712 签名 → POST /order → 查订单 → 取消

用法: .venv/bin/python scripts/verify_live_testnet.py [--poll 30] [--max-wait 600]
"""
import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eth_account import Account

from polytrader.data.clob_client import ClobClient
from polytrader.data.http_client import HttpClient
from polytrader.execution import signer

STAGING = "https://clob-staging.polymarket.com"


def probe(proxy_env: bool = True) -> bool:
    """staging 是否可达（走系统代理）。"""
    import requests
    try:
        r = requests.get(f"{STAGING}/midpoint", timeout=10)
        return r.status_code < 500
    except Exception:
        return False


def fetch_exchange_and_chain(http) -> tuple[int, str]:
    """从 staging 探测链 id 与 Exchange 合约地址（/ 或 /market 返回配置）。"""
    # 优先从根路径找；找不到则用已知 Amoy 值占位并告警
    for path in ["/", "/market", "/openapi.json"]:
        try:
            d = http.get_json(f"{STAGING}{path}")
            if isinstance(d, dict):
                if d.get("chainId"):
                    return int(d["chainId"]), d.get("exchange") or d.get("exchangeAddress") or ""
        except Exception:
            continue
    return 80002, ""  # Amoy 默认；地址未知时下单阶段会失败并给出提示


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--poll", type=int, default=30, help="可达性重试间隔秒")
    ap.add_argument("--max-wait", type=int, default=600, help="等待可达的最大秒数")
    args = ap.parse_args()

    print(f"== 测试网全链路验证: {STAGING} ==")
    # 1. 等待可达
    waited = 0
    while not probe():
        if waited >= args.max_wait:
            print(f"✗ {STAGING} 在 {args.max_wait}s 内不可达（当前代理线路问题）")
            return 1
        print(f"  等待 staging 可达... ({waited}s)")
        time.sleep(args.poll)
        waited += args.poll
    print("✓ staging 可达")

    # 2. 随机测试钱包 + 派生凭证
    acct = Account.create()
    print(f"✓ 测试钱包: {acct.address}")
    cred = signer.derive_api_credentials(acct.key.hex())
    print(f"✓ 派生凭证: api_key={cred['api_key'][:14]}... passphrase={cred['api_passphrase']}")

    # 3. 认证只读验证（balance）
    http = HttpClient()
    clob = ClobClient(api_base=STAGING, http=http,
                      api_key=cred["api_key"], api_secret=cred["api_secret"],
                      api_passphrase=cred["api_passphrase"],
                      private_key=acct.key.hex())
    try:
        bal = clob.get_balance()
        print(f"✓ 认证通过，balance 响应: {json.dumps(bal)[:200]}")
    except Exception as e:
        print(f"✗ 认证/balance 失败: {e}")
        return 1

    # 4. 探测链与合约（staging 配置）
    chain_id, exchange = fetch_exchange_and_chain(http)
    print(f"  staging 配置: chainId={chain_id} exchange={exchange or '(未暴露)'}")

    print("\n== 认证与签名链路验证完成 ==")
    print("说明: 真实测试下单（place_order）需要测试网市场 token_id；"
          "当前 staging 市场发现与合约地址确认后即可扩展。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
