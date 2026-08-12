"""LLM updown 挂机循环：连续 N 轮模拟测算（subprocess 逐轮，天然跟随新窗口）。

每轮：simulate_llm_updown.py --wait 330（当前窗口 → LLM → 模拟成交 $100/笔 → 结算验证）
轮间间隔由结算等待自然形成；结果累计汇总。
用法: .venv/bin/python scripts/run_llm_loop.py --rounds 3 [--min-edge 0.04]
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--min-edge", type=float, default=0.04)
    ap.add_argument("--wait", type=int, default=330)
    args = ap.parse_args()

    generated: list[Path] = []
    for i in range(1, args.rounds + 1):
        print(f"\n=== LOOP {i}/{args.rounds} ===", flush=True)
        before = set(Path("backtest_results").glob("llm_updown_sim_*.json"))
        proc = subprocess.run(
            [sys.executable, "scripts/simulate_llm_updown.py",
             "--wait", str(args.wait), "--min-edge", str(args.min_edge)],
            cwd=ROOT, capture_output=True, text=True, timeout=args.wait + 240,
        )
        for line in proc.stdout.splitlines():
            if line.startswith(("windows:", "signals:", "  btc", "  eth", "  sol",
                                "  xrp", "  doge", "  bnb", "  hype", "settled",
                                "cumulative", "FINAL", "saved:", "  unsettled")):
                print("  " + line, flush=True)
        if proc.returncode != 0:
            print(f"  round failed rc={proc.returncode}: {proc.stderr[-300:]}")
            continue
        new = set(Path("backtest_results").glob("llm_updown_sim_*.json")) - before
        generated.extend(sorted(new, key=lambda p: p.stat().st_mtime))
        if i < args.rounds:
            print("  waiting for next 5m window...", flush=True)
            time.sleep(30)

    # 汇总
    all_trades = []
    for p in generated:
        try:
            d = json.loads(p.read_text())
            all_trades.extend(d.get("trades", []))
        except Exception:
            continue
    settled = [t for t in all_trades if t.get("pnl") is not None]
    print(f"\n=== SUMMARY ({len(generated)} rounds files) ===")
    print(f"trades={len(all_trades)} settled={len(settled)}")
    if settled:
        wins = sum(1 for t in settled if t["pnl"] > 0)
        total = sum(t["pnl"] for t in settled)
        avg_p = sum(t["llm_p"] for t in settled) / len(settled)
        avg_ref = sum(t["ref"] for t in settled) / len(settled)
        print(f"win_rate={wins / len(settled):.1%} ({wins}/{len(settled)}) "
              f"total_pnl=${total:+.2f}")
        print(f"avg_llm_p={avg_p:.3f} avg_ref={avg_ref:.3f} "
              f"(极端值占比: "
              f"{sum(1 for t in settled if t['llm_p'] <= 0.02 or t['llm_p'] >= 0.98) / len(settled):.0%})")
        for t in settled:
            print(f"  r{t.get('round', '?')} {t['slug']:34s} {t['side']:3s} "
                  f"llm_p={t['llm_p']:.3f} ref={t['ref']:.3f} "
                  f"pnl=${t['pnl']:+.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
