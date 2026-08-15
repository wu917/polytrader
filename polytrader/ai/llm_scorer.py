"""LLM 评分：OpenAI 兼容接口的概率估计（可插拔，无 key 自动禁用）。

提示词要求 LLM 只输出一个 0-1 的概率数字（JSON），解析失败则弃用该评分。
"""
from __future__ import annotations

import json
import logging
import re
import time
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
                 timeout: int = 90, use_proxy: bool = False, audit_path: str | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.audit_path = audit_path
        # 安全：LLM API key 只应发给目标 API 服务商。默认不走代理
        # （代理可能为第三方，能截获 Authorization header）。
        if not self.base_url.startswith("https://"):
            raise ValueError(f"LLM base_url must be https, got: {base_url}")
        self.http = http or HttpClient(timeout=timeout)
        if not use_proxy:
            self.http.session.proxies.clear()
        self.enabled = bool(api_key)

    def score(self, question: str, description: str = "", market_category: str = "") -> float | None:
        """返回 P(YES) ∈ [0,1]；不可用时返回 None（自动重试 1 次）。"""
        p, _ = self.score_with_reason(question, description, market_category)
        return p

    def score_with_reason(self, question: str, description: str = "",
                          market_category: str = "") -> tuple[float | None, str | None]:
        """返回 (P(YES), reason)；不可用时 (None, None)（自动重试 1 次）。"""
        if not self.enabled:
            return None, None
        user = f"Market: {question}\nCategory: {market_category}\nDetails: {(description or '')[:1500]}"
        t0 = time.time()
        for attempt in range(2):  # 推理模型偶发超时/限流，重试一次
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
                        # 推理模型（deepseek-v4-flash 等）思维链会消耗大量 token，
                        # 800 不够 → content 被截断为空；4000 保证 content 有完整 JSON
                        "max_tokens": 4000,
                    },
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"]["content"]
                parsed = _parse_response(content)
                usage = data.get("usage") or {}
                # 统一日志：LLM 调用的 model/耗时/概率/理由（截断防刷屏）
                log.info("LLM %s attempt%d | %dms | p=%s | reason: %.120s | tokens=%s",
                         self.model, attempt + 1, int((time.time() - t0) * 1000),
                         parsed[0], str(parsed[1] or "")[:120],
                         {k: usage.get(k) for k in ("prompt_tokens", "completion_tokens")})
                self._audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                             "event": "llm_call",
                             "model": self.model, "attempt": attempt + 1,
                             "question": question[:200],
                             "prompt_preview": (description or "")[:300],
                             "content_preview": content[:300],
                             "p": parsed[0], "reason": parsed[1],
                             "ms": int((time.time() - t0) * 1000),
                             "usage": usage})
                if parsed[0] is not None:
                    return parsed
                log.warning("LLM content unparsable (attempt %d): %.100r", attempt + 1, content)
            except Exception as exc:  # noqa: BLE001
                log.warning("LLM score failed (attempt %d): %s", attempt + 1, exc)
                self._audit({"ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()),
                             "event": "llm_failed", "model": self.model,
                             "attempt": attempt + 1, "question": question[:200],
                             "error": str(exc)[:300]})
        return None, None

    def _audit(self, rec: dict):
        """LLM 调用审计（JSONL）。"""
        if not self.audit_path:
            return
        try:
            from pathlib import Path
            Path(self.audit_path).parent.mkdir(parents=True, exist_ok=True)
            with open(self.audit_path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:  # noqa: BLE001 审计失败不影响业务
            pass

    def score_many(self, questions: list[tuple[str, str, str]],
                   max_parallel: int = 5) -> list[float | None]:
        """顺序批量评分（保持简单可靠，不做并发）。"""
        return [self.score(q, d, c) for q, d, c in questions[:max_parallel]]


def _parse_response(content: str) -> tuple[float | None, str | None]:
    """从 LLM 输出提取 (probability, reason)：优先 JSON，其次裸数字。"""
    if not content:
        return None, None
    text = content.strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "probability" in data:
            p = _clamp(float(data["probability"]))
            reason = data.get("reason")
            return p, (str(reason) if reason else None)
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    m = re.search(r"0?\.\d{2,4}|\b[01]\b", text)
    if m:
        return _clamp(float(m.group(0))), None
    return None, None


def _parse_probability(content: str) -> float | None:
    """兼容旧接口：只取概率。"""
    p, _ = _parse_response(content)
    return p


def _clamp(p: float) -> float:
    return max(0.001, min(0.999, p))


def blend_probabilities(model_p: float, llm_p: float | None, llm_weight: float) -> float:
    """模型概率与 LLM 评分融合；llm_p 为 None 时退化为纯模型。"""
    if llm_p is None:
        return float(model_p)
    w = max(0.0, min(1.0, llm_weight))
    return float(w * llm_p + (1.0 - w) * model_p)
