"""代理感知、带重试的 HTTP 客户端。"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any

import requests

from polytrader.logging_setup import get_logger

log = get_logger("data.http")


class HttpError(Exception):
    """HTTP 请求失败（重试耗尽）。"""


class HttpClient:
    """线程安全的 requests 封装：代理、超时、指数退避重试、可选调用审计。"""

    def __init__(self, proxy: str | None = None, timeout: int = 15, max_retries: int = 3,
                 audit_path: str | None = None):
        self.timeout = timeout
        self.max_retries = max_retries
        self.audit_fh = None
        if audit_path:
            Path(audit_path).parent.mkdir(parents=True, exist_ok=True)
            self.audit_fh = open(audit_path, "a", encoding="utf-8")
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PolyTrader/0.1"})
        if proxy:
            self.session.proxies.update({
                "http": proxy,
                "https": proxy,
            })
            log.info("HTTP client using proxy %s", proxy)

    def close(self):
        """关闭审计文件（若开启）。"""
        if self.audit_fh:
            self.audit_fh.close()
            self.audit_fh = None

    def _audit(self, rec: dict):
        """写一行 JSONL 审计记录（线程安全粒度足够：单线程脚本）。"""
        if self.audit_fh:
            try:
                self.audit_fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                self.audit_fh.flush()
            except Exception:  # noqa: BLE001 审计失败不影响业务
                pass

    def get(self, url: str, params: dict | None = None, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, params=params, **kwargs)

    def post(self, url: str, json: dict | None = None, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, json=json, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_err: Exception | None = None
        t0 = time.time()
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"{resp.status_code} {url}")
                ms = int((time.time() - t0) * 1000)
                # 行情 K 线端点（binance klines / okx candles）响应完整输出
                # （6 根 K 线 ~1.1KB，便于核对行情）；其他端点截断防刷屏
                preview_len = 1500 if ("/klines" in url or "/candles" in url) else 200
                body_preview = resp.text[:preview_len]
                # 统一日志：每次请求的 url/status/耗时/响应预览（截断防刷屏）
                log.info("→ %s %s | %s %dms | resp: %.200s",
                         method, url[:150], resp.status_code, ms,
                         body_preview.replace("\n", " ")[:preview_len])
                self._audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                             "event": "http_request",
                             "method": method, "url": url[:200],
                             "params": {k: str(v)[:100] for k, v in (kwargs.get("params") or {}).items()},
                             "attempts": attempt,
                             "status": resp.status_code,
                             "ms": ms,
                             "resp_preview": body_preview})
                return resp
            except (requests.RequestException, requests.HTTPError) as exc:
                last_err = exc
                log.warning("request %s %s failed (attempt %d/%d): %s",
                            method, url, attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
        self._audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                     "event": "http_failed",
                     "method": method, "url": url[:200],
                     "params": {k: str(v)[:100] for k, v in (kwargs.get("params") or {}).items()},
                     "error": str(last_err)[:300]})
        raise HttpError(f"{method} {url} failed after {self.max_retries} attempts: {last_err}")

    def get_json(self, url: str, params: dict | None = None, **kwargs: Any) -> Any:
        resp = self.get(url, params=params, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def post_json(self, url: str, json_body: dict | None = None,
                  headers: dict | None = None, **kwargs: Any) -> Any:
        resp = self.post(url, json=json_body, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()

    def delete_json(self, url: str, params: dict | None = None,
                    headers: dict | None = None, **kwargs: Any) -> Any:
        resp = self._request("DELETE", url, params=params, headers=headers, **kwargs)
        resp.raise_for_status()
        if not resp.text:
            return {}
        try:
            return resp.json()
        except Exception:
            return {"status": resp.status_code, "raw": resp.text[:200]}
