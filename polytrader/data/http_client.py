"""代理感知、带重试的 HTTP 客户端。"""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import requests

from polytrader.logging_setup import get_logger

log = get_logger("data.http")


class HttpError(Exception):
    """HTTP 请求失败（重试耗尽）。"""


class HttpClient:
    """线程安全的 requests 封装：代理、超时、指数退避重试、可选调用审计。

    代理熔断（circuit breaker，2026-08-17）：
    - 本机代理节点间歇性中断（一天数十次全域 000），中断期间每个请求
      拖满 3×15s 重试 → 一轮扫描几百请求 = 重试黑洞（数十分钟空转）
    - 连续 CB_THRESHOLD 次整体失败 → 熔断打开：后续请求快速失败（不走
      网络不重试），每 CB_PROBE_INTERVAL 放行一个探测请求（half-open）
    - 探测成功 → 熔断关闭自动恢复；线程安全（对账线程/主循环共享）
    """

    # 熔断参数
    CB_THRESHOLD = 5        # 连续整体失败次数 → 打开熔断
    CB_COOLDOWN = 60.0      # 熔断打开时长（探测周期外快速失败）
    CB_PROBE_INTERVAL = 20.0  # 熔断期间探测请求最小间隔

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
        # 熔断状态（线程安全）
        self._cb_lock = threading.Lock()
        self._cb_failures = 0
        self._cb_opened_at = 0.0
        self._cb_open_until = 0.0
        self._cb_last_probe = 0.0

    # ---- 熔断控制 ----

    def _cb_should_fast_fail(self) -> bool:
        """熔断打开时：非探测请求快速失败。探测请求放行（更新探测时间）。"""
        with self._cb_lock:
            now = time.time()
            if not self._cb_open_until or now >= self._cb_open_until:
                return False
            # 熔断窗口内：限速放行探测
            if now - self._cb_last_probe >= self.CB_PROBE_INTERVAL:
                self._cb_last_probe = now
                return False
            return True

    def _cb_record_success(self) -> None:
        with self._cb_lock:
            if self._cb_open_until:
                was_open = True
            else:
                was_open = False
            self._cb_failures = 0
            self._cb_open_until = 0.0
            if was_open:
                log.info("circuit CLOSED: 代理恢复，熔断解除")

    def _cb_record_failure(self) -> None:
        with self._cb_lock:
            self._cb_failures += 1
            if self._cb_failures >= self.CB_THRESHOLD and not self._cb_open_until:
                now = time.time()
                self._cb_opened_at = now
                self._cb_open_until = now + self.CB_COOLDOWN
                self._cb_last_probe = now  # 打开瞬间不放行探测，等 PROBE_INTERVAL
                log.warning("circuit OPEN: 连续 %d 次失败（代理中断？），"
                            "%.0fs 内快速失败、每 %.0fs 探测一次",
                            self._cb_failures, self.CB_COOLDOWN,
                            self.CB_PROBE_INTERVAL)

    @property
    def circuit_open(self) -> bool:
        """当前是否处于熔断打开状态（诊断用）。"""
        with self._cb_lock:
            return bool(self._cb_open_until and time.time() < self._cb_open_until)

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
        # 熔断打开：快速失败（不走网络不重试），探测请求除外
        if self._cb_should_fast_fail():
            raise HttpError(f"{method} {url} fast-fail: circuit open（代理中断，等待探测恢复）")
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
                # 请求明细（url/status/耗时/响应预览）降级 DEBUG：
                # 高频轮询（activity/trades/positions 等）逐条 INFO 曾致
                # polytrader.log 膨胀至 82MB（2026-08-16）；DEBUG 开启时
                # 仍可核对，失败/重试保留 WARNING（见下方 except 分支）
                log.debug("→ %s %s | %s %dms | resp: %.200s",
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
                self._cb_record_success()
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
        self._cb_record_failure()
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
