"""Polymarket Relayer（CLOB V2 gasless）客户端。

Deposit Wallet 流程（新 API 用户，signatureType=3）：
  1. wallet_create(): 提交 WALLET-CREATE → relayer 部署 deposit wallet（gasless）
  2. 轮询 get_transaction() 拿 proxyAddress（deposit wallet 地址）
  3. execute(): 批量 gasless 交易（pUSD approve / CTF 操作）
  4. 下单走 CLOB V2（EOA 签名订单，funder = deposit wallet，signatureType=3）

认证：Relayer API Key 两个头（用户从 polymarket.com/settings?tab=api-keys 创建）。

⚠️ 真金白银相关：所有 execute/submit 都是链上交易（gas 由 Polymarket 承担），
调用前必须确认交易内容。未经 testnet 验证前不要对 mainnet 真实资金操作。
"""
from __future__ import annotations

import time

import requests

from polytrader.data.http_client import HttpClient, HttpError

RELAYER_HOST = "https://relayer-v2.polymarket.com"
DEPOSIT_WALLET_FACTORY = "0x00000000000Fb5C9ADea0298D729A0CB3823Cc07"

# WalletType（与 CLOB V2 一致）
EOA = 0
POLY_PROXY = 1
GNOSIS_SAFE = 2
DEPOSIT_WALLET = 3

# 终态
STATE_CONFIRMED = "STATE_CONFIRMED"
STATE_FAILED = "STATE_FAILED"
STATE_INVALID = "STATE_INVALID"


class RelayerClient:
    def __init__(self, api_key: str, api_key_address: str,
                 host: str = RELAYER_HOST, http: HttpClient | None = None):
        self.api_key = api_key
        self.api_key_address = api_key_address
        self.host = host.rstrip("/")
        self.http = http or HttpClient()

    @property
    def auth_ready(self) -> bool:
        return bool(self.api_key and self.api_key_address)

    def _headers(self) -> dict:
        if not self.auth_ready:
            raise RuntimeError("Relayer auth not configured: RELAYER_API_KEY + "
                               "RELAYER_API_KEY_ADDRESS required")
        return {
            "RELAYER_API_KEY": self.api_key,
            "RELAYER_API_KEY_ADDRESS": self.api_key_address,
            "Content-Type": "application/json",
        }

    # ---- 钱包部署 ----
    def wallet_create(self, signer: str,
                      to: str = DEPOSIT_WALLET_FACTORY,
                      metadata: str = "Deploy Deposit Wallet") -> dict:
        """提交 WALLET-CREATE，返回 relayer 交易（含 transactionID）。

        幂等：若 deposit wallet 已存在（充值/之前部署过），relayer 返回
        400 {"error": "deposit wallet already exists for signer ..."}，
        此时返回 {"state": "STATE_EXISTS", "signer": signer}。
        """
        body = {"type": "WALLET-CREATE", "from": signer, "to": to,
                "metadata": metadata}
        try:
            return self.http.post_json(f"{self.host}/submit", json_body=body,
                                       headers=self._headers())
        except requests.HTTPError as exc:
            text = ""
            if exc.response is not None:
                text = exc.response.text or ""
            if "already exists" in text:
                return {"state": "STATE_EXISTS", "signer": signer,
                        "message": text}
            raise

    def get_transaction(self, transaction_id: str) -> list[dict]:
        """查询 relayer 交易状态（含 proxyAddress/transactionHash）。"""
        data = self.http.get_json(f"{self.host}/transaction",
                                  params={"id": transaction_id},
                                  headers=self._headers())
        if isinstance(data, dict):
            data = data.get("transactions") or data.get("data") or [data]
        return list(data) if isinstance(data, list) else [data]

    def wait_transaction(self, transaction_id: str, poll: int = 3,
                         timeout: int = 180) -> dict:
        """轮询到终态，返回交易详情；失败抛 RuntimeError。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            rows = self.get_transaction(transaction_id)
            if rows:
                row = rows[0]
                state = str(row.get("state") or "")
                if state == STATE_CONFIRMED:
                    return row
                if state in (STATE_FAILED, STATE_INVALID):
                    raise RuntimeError(f"relayer transaction {state}: {row}")
            time.sleep(poll)
        raise TimeoutError(f"relayer transaction {transaction_id} not confirmed "
                           f"within {timeout}s")

    # ---- 通用 gasless 执行（approve / CTF 操作等）----
    def execute(self, transactions: list[dict], metadata: str = "") -> dict:
        """批量提交链上交易（relayer 代付 gas）。

        transactions: [{"to": <contract>, "data": <0x calldata>, "value": "0"}, ...]
        """
        body = {
            "type": "EXECUTE",
            "from": self.api_key_address,
            "transactions": transactions,
            "metadata": metadata,
        }
        return self.http.post_json(f"{self.host}/submit", json_body=body,
                                   headers=self._headers())
