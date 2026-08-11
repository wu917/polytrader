"""LLM 评分：OpenAI 兼容接口的概率估计（可插拔，无 key 自动禁用）。

提示词要求 LLM 只输出一个 0-1 的概率数字（JSON），解析失败则弃用该评分。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from polytrader.data.http_client import HttpClient
from polytrader.logging_setup import get_logger

log = get_logger("ai.llm")

SYSTEM_PROMPT = (
    "You are a prediction-market probability estimator. "
    "Given a market question and description, estimate the probability that the "
    "YES outcome resolves true. Consider base rates, known facts, and the current date. "
    "Respond ONLY with a JSON object: {\"probability\": <0.0-1.0>, \"reason\": \"<one line>\"}."
)


class LLMScorer:
    """OpenAI 兼容 chat completions 客户端。"""

    def __init__(self, api_key: str = "", base_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-4o-mini", http: HttpClient | None = None,
                 timeout: int = 30, use_proxy: bool = False):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        # 安全：LLM API key 只应发给目标 API 服务商。默认不走代理
        # （代理可能为第三方，能截获 Authorization header）。
        if not self.base_url.startswith("https://"):
            raise ValueError(f"LLM base_url must be https, got: {base_url}")
        self.http = http or HttpClient(timeout=timeout)
        if not use_proxy:
            self.http.session.proxies.clear()
        self.enabled = bool(api_key)

    def score(self, question: str, description: str = "", market_category: str = "") -> float | None:
        """返回 P(YES) ∈ [0,1]；不可用时返回 None。"""
        if not self.enabled:
            return None
        user = f"Market: {question}\nCategory: {market_category}\nDetails: {(description or '')[:1500]}"
        try:
            resp = self.http.post(
                f"{self.base_url}/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0.0,
                    "max_tokens": 120,
                },
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            return _parse_probability(content)
        except Exception as exc:  # noqa: BLE001
            log.warning("LLM score failed: %s", exc)
            return None

    def score_many(self, questions: list[tuple[str, str, str]],
                   max_parallel: int = 5) -> list[float | None]:
        """顺序批量评分（保持简单可靠，不做并发）。"""
        return [self.score(q, d, c) for q, d, c in questions[:max_parallel]]


def _parse_probability(content: str) -> float | None:
    """从 LLM 输出提取概率：优先 JSON，其次裸数字。"""
    if not content:
        return None
    text = content.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "probability" in data:
            return _clamp(float(data["probability"]))
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    m = re.search(r"0?\.\d{2,4}|\b[01]\b", text)
    if m:
        return _clamp(float(m.group(0)))
    return None


def _clamp(p: float) -> float:
    return max(0.001, min(0.999, p))


def blend_probabilities(model_p: float, llm_p: float | None, llm_weight: float) -> float:
    """模型概率与 LLM 评分融合；llm_p 为 None 时退化为纯模型。"""
    if llm_p is None:
        return float(model_p)
    w = max(0.0, min(1.0, llm_weight))
    return float(w * llm_p + (1.0 - w) * model_p)
