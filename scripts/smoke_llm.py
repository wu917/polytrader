"""LLM 评分冒烟测试：DeepSeek V4 端到端。

用法: .venv/bin/python scripts/smoke_llm.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.ai.llm_scorer import LLMScorer
from polytrader.config import load_config


def main() -> int:
    cfg = load_config()
    scorer = LLMScorer(api_key=cfg.llm_api_key, base_url=cfg.llm_base_url,
                       model=cfg.llm_model, timeout=60)
    print(f"LLM enabled={scorer.enabled} base={scorer.base_url} model={scorer.model}")

    if not scorer.enabled:
        print("FAIL: no LLM_API_KEY")
        return 1

    # 简单概率问题
    p = scorer.score("Will BTC be above $100k by end of 2025?", "Crypto market question", "crypto")
    print(f"score(btc): {p}")

    # 诊断：打印原始响应（content/reasoning 结构）
    resp = scorer.http.post(
        f"{scorer.base_url}/chat/completions",
        json={
            "model": scorer.model,
            "messages": [
                {"role": "system", "content": "You are a prediction-market probability estimator. Respond ONLY with a JSON object: {\"probability\": <0.0-1.0>, \"reason\": \"<one line>\"}."},
                {"role": "user", "content": "Market: Will BTC be above $100k by end of 2025? Category: crypto"},
            ],
            "temperature": 0, "max_tokens": 1000,
        },
        headers={"Authorization": f"Bearer {scorer.api_key}"},
    )
    d = resp.json()
    msg = d["choices"][0]["message"]
    print(f"[diag] content={msg.get('content')!r}")
    rc = msg.get("reasoning_content", "")
    print(f"[diag] reasoning_content len={len(rc)} head={rc[:150]!r}")
    print(f"[diag] usage={d.get('usage')}")
    print(f"[diag] finish_reason={d['choices'][0].get('finish_reason')}")
    if p is None:
        print("FAIL: no score returned (content parsing?)")
        return 1

    # 真实市场风格问题
    p2 = scorer.score(
        "Will the Fed cut rates at the September 2025 meeting?",
        "FOMC meeting scheduled for September 16-17, 2025. Fed funds futures imply ~60% chance of a cut.",
        "economics",
    )
    print(f"score(fed): {p2}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
