"""代理感知、带重试的 HTTP 客户端。"""
from __future__ import annotations

import logging
import time
from typing import Any

import requests

from polytrader.logging_setup import get_logger

log = get_logger("data.http")


class HttpError(Exception):
    """HTTP 请求失败（重试耗尽）。"""


class HttpClient:
    """线程安全的 requests 封装：代理、超时、指数退避重试。"""

    def __init__(self, proxy: str | None = None, timeout: int = 15, max_retries: int = 3):
        self.timeout = timeout
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "PolyTrader/0.1"})
        if proxy:
            self.session.proxies.update({
                "http": proxy,
                "https": proxy,
            })
            log.info("HTTP client using proxy %s", proxy)

    def get(self, url: str, params: dict | None = None, **kwargs: Any) -> requests.Response:
        return self._request("GET", url, params=params, **kwargs)

    def post(self, url: str, json: dict | None = None, **kwargs: Any) -> requests.Response:
        return self._request("POST", url, json=json, **kwargs)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        kwargs.setdefault("timeout", self.timeout)
        last_err: Exception | None = None
        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"{resp.status_code} {url}")
                return resp
            except (requests.RequestException, requests.HTTPError) as exc:
                last_err = exc
                log.warning("request %s %s failed (attempt %d/%d): %s",
                            method, url, attempt, self.max_retries, exc)
                if attempt < self.max_retries:
                    time.sleep(0.5 * (2 ** (attempt - 1)))
        raise HttpError(f"{method} {url} failed after {self.max_retries} attempts: {last_err}")

    def get_json(self, url: str, params: dict | None = None, **kwargs: Any) -> Any:
        resp = self.get(url, params=params, **kwargs)
        resp.raise_for_status()
        return resp.json()
