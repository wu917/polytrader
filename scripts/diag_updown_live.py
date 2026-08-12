"""探测新上线的 5/15 分钟 BTC/ETH updown 市场。

拉全量活跃市场，检查 slug/end_date：updown 快速市场 end_date 应接近当前。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from polytrader.ai.features import _parse_ts
from polytrader.data.gamma_client import GammaClient
from polytrader.data.http_client import HttpClient

http = HttpClient(proxy="socks5h://127.0.0.1:7890")
gamma = GammaClient(http=http)

ms = []
offset = 0
while offset < 2000:
    batch = gamma.get_markets(limit=100, offset=offset, active=True)
    if not batch:
        break
    ms.extend(batch)
    offset += 100
print(f"active markets: {len(ms)}")

now = time.time()
updown = [m for m in ms if "updown" in m.slug.lower()]
print(f"updown active: {len(updown)}")
for m in sorted(updown, key=lambda x: x.end_date, reverse=True)[:10]:
    ts = _parse_ts(m.end_date)
    mins = (ts - now) / 60 if ts else None
    print(f"  {m.slug[:44]:46s} end={m.end_date[:19]} in={mins:.0f}min liq={m.liquidity:.0f}")

# 近 2 小时结算的任何市场
soon = []
for m in ms:
    ts = _parse_ts(m.end_date)
    if ts and 0 < ts - now < 7200:
        soon.append((ts - now, m))
soon.sort()
print(f"\nmarkets ending within 2h: {len(soon)}")
for secs, m in soon[:10]:
    print(f"  in {secs/60:5.1f}min  {m.slug[:50]:52s} liq={m.liquidity:9.0f}")
