"""PolyTrader Web 统计面板：开单/结算统计 + 胜率/盈亏统一展示。

用法:
    .venv/bin/python scripts/web_dashboard.py [--port 8787] [--auto-refresh 5]

数据源（优先级）:
    1. logs/llm_daemon_*/llm_results.jsonl  （守护进程统一会话，取最新）
    2. backtest_results/llm_results_*.jsonl （旧挂机输出，取最新）

端点:
    /            单页面板（自包含 HTML/JS/SVG，无 CDN 依赖）
    /api/stats   聚合 JSON（供前端与外部程序使用）
    /api/sessions 会话列表
"""
import argparse
import json
import sys
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent


def load_session_files():
    """返回 [(会话名, 文件路径), ...] 按时间倒序。"""
    sessions = []
    for d in sorted((ROOT / "logs").glob("llm_daemon_*"), reverse=True):
        rf = d / "llm_results.jsonl"
        if rf.exists():
            sessions.append((d.name, rf))
    for rf in sorted((ROOT / "backtest_results").glob("llm_results_*.jsonl"),
                     key=lambda p: p.stat().st_mtime, reverse=True):
        sessions.append((rf.stem, rf))
    return sessions


def parse_session(path: Path):
    """解析一个结果文件 → 统计 dict。"""
    trades = []            # 全部开单（round 事件）
    settled = []           # 结算事件（去重 by trade_id）
    seen_ids = set()
    rounds = 0
    for line in path.read_text().splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        t = rec.get("type")
        if t == "round":
            rounds += 1
            for tr in rec.get("trades", []):
                tr.setdefault("round", rec.get("round"))
                trades.append(tr)
        elif t == "trade_settled":
            tid = rec.get("trade_id")
            if tid:
                if tid in seen_ids:
                    continue
                seen_ids.add(tid)
            settled.append(rec)
    # 未结算：round 事件中 pnl=null 且无对应结算事件的
    settled_slugs = {s.get("slug") for s in settled}
    pending = [t for t in trades
               if t.get("pnl") is None and t.get("slug") not in settled_slugs]
    wins = sum(1 for s in settled if s.get("win") in (1, "1"))
    total = sum(float(s.get("pnl") or 0) for s in settled)
    invest = sum(float(s.get("size_usd") or 0) for s in settled)
    # 分币种 / 分方向（按结算）
    by_coin = OrderedDict()
    by_side = OrderedDict()
    curve = []            # 累计盈亏曲线（按结算时间）
    cum = 0.0
    for s in sorted(settled, key=lambda x: x.get("ts", "")):
        coin = s.get("coin") or "?"
        side = s.get("side") or "?"
        pnl = float(s.get("pnl") or 0)
        by_coin.setdefault(coin, {"trades": 0, "wins": 0, "pnl": 0.0})["trades"] += 1
        by_coin[coin]["wins"] += 1 if s.get("win") in (1, "1") else 0
        by_coin[coin]["pnl"] += pnl
        by_side.setdefault(side, {"trades": 0, "wins": 0, "pnl": 0.0})["trades"] += 1
        by_side[side]["wins"] += 1 if s.get("win") in (1, "1") else 0
        by_side[side]["pnl"] += pnl
        cum += pnl
        curve.append({"ts": s.get("ts", ""), "cum": round(cum, 2)})
    return {
        "rounds": rounds,
        "trades": len(trades),
        "settled": len(settled),
        "pending": len(pending),
        "wins": wins,
        "losses": len(settled) - wins,
        "win_rate": round(wins / max(len(settled), 1) * 100, 1),
        "total_pnl": round(total, 2),
        "invest": round(invest, 2),
        "return_pct": round(total / invest * 100, 2) if invest else 0.0,
        "by_coin": dict(by_coin),
        "by_side": dict(by_side),
        "curve": curve,
        "trades": sorted(trades, key=lambda x: x.get("round", 0)),
        "settled": sorted(settled, key=lambda x: x.get("ts", "")),
    }


def load_stats():
    sessions = load_session_files()
    all_stats = []
    for name, path in sessions[:20]:
        try:
            st = parse_session(path)
        except Exception as e:
            st = {"error": str(e)}
        all_stats.append({"name": name, "stats": st})
    # 最新会话作为主视图
    main = all_stats[0] if all_stats else None
    return {"main": main, "sessions": all_stats}


PAGE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>PolyTrader 面板</title>
<style>
  body { font-family: -apple-system, "PingFang SC", sans-serif; background:#0f1115;
         color:#e6e6e6; margin:0; padding:20px; }
  h1 { font-size:20px; margin:0 0 4px; }
  .sub { color:#888; font-size:12px; margin-bottom:16px; }
  .cards { display:flex; gap:12px; flex-wrap:wrap; margin-bottom:16px; }
  .card { background:#181b22; border:1px solid #262b36; border-radius:8px;
          padding:12px 18px; min-width:120px; }
  .card .v { font-size:24px; font-weight:700; }
  .card .k { font-size:12px; color:#888; }
  .pos { color:#2ecc71; } .neg { color:#e74c3c; }
  table { border-collapse:collapse; width:100%; font-size:13px; }
  th,td { border:1px solid #262b36; padding:6px 8px; text-align:left; }
  th { background:#181b22; }
  tr:nth-child(even) { background:#13161c; }
  .grid { display:grid; grid-template-columns: 1fr 1fr; gap:16px; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
  .box { background:#181b22; border:1px solid #262b36; border-radius:8px; padding:14px; }
  .box h2 { font-size:14px; margin:0 0 10px; color:#aaa; }
  .pending { color:#f39c12; }
  .sess { font-size:11px; color:#888; margin-bottom:8px; }
</style>
</head>
<body>
<h1>PolyTrader 交易面板</h1>
<div class="sub" id="sub">加载中…</div>
<div class="cards" id="cards"></div>
<div class="grid">
  <div class="box"><h2>累计盈亏（按结算时间）</h2><svg id="curve" width="100%" height="160"></svg></div>
  <div class="box"><h2>分币种</h2><table id="byCoin"></table>
    <h2 style="margin-top:14px">分方向</h2><table id="bySide"></table></div>
</div>
<div class="box" style="margin-top:16px"><h2>交易明细</h2><table id="trades"></table></div>
<script>
let INTERVAL = 5;
async function load() {
  try {
    const r = await fetch('/api/stats', {cache:'no-store'});
    const d = await r.json();
    render(d);
  } catch(e) { document.getElementById('sub').textContent = '加载失败: ' + e; }
}
function fmt(v, sign) {
  const n = Number(v);
  const cls = sign ? (n > 0 ? 'pos' : (n < 0 ? 'neg' : '')) : '';
  return `<span class="${cls}">${n.toFixed ? (Number.isInteger(n) ? n : n.toFixed(2)) : n}</span>`;
}
function render(d) {
  const m = d.main;
  if (!m) { document.getElementById('sub').textContent = '暂无结果数据（守护进程尚未产生交易）'; return; }
  const s = m.stats, name = m.name;
  document.getElementById('sub').innerHTML =
    `会话: ${name} &nbsp;|&nbsp; 轮次: ${s.rounds} &nbsp;|&nbsp; 开单: ${s.trades} &nbsp;|&nbsp; 结算: ${s.settled} &nbsp;|&nbsp; 待结算: <span class="pending">${s.pending}</span> &nbsp;|&nbsp; 每 ${INTERVAL}s 自动刷新`;
  const cards = [
    ['胜率', `${s.win_rate}%`, ''],
    ['总盈亏', `$${s.total_pnl}`, s.total_pnl > 0 ? 'pos' : (s.total_pnl < 0 ? 'neg' : '')],
    ['收益率', `${s.return_pct}%`, s.return_pct > 0 ? 'pos' : (s.return_pct < 0 ? 'neg' : '')],
    ['胜/负', `${s.wins} / ${s.losses}`, ''],
    ['投入', `$${s.invest}`, ''],
  ];
  document.getElementById('cards').innerHTML = cards.map(c =>
    `<div class="card"><div class="v ${c[2]}">${c[0]==='胜/负'?c[1]:c[1]}</div><div class="k">${c[0]}</div></div>`
  ).join('');
  // 累计盈亏曲线（SVG）
  const cv = s.curve;
  const svg = document.getElementById('curve');
  if (cv.length < 2) { svg.innerHTML = '<text x="8" y="20" fill="#666">结算样本不足</text>'; }
  else {
    const W = 760, H = 150, pad = 30;
    const xs = cv.map((_,i)=>pad + i*(W-pad*2)/Math.max(cv.length-1,1));
    const vals = cv.map(c=>c.cum);
    const lo = Math.min(...vals, 0), hi = Math.max(...vals, 0);
    const ys = v => H - pad - (v-lo)/(Math.max(hi-lo, 1e-9))*(H-pad*2);
    const pts = xs.map((x,i)=>`${x},${ys(vals[i])}`).join(' ');
    svg.innerHTML = `<polyline points="${pts}" fill="none" stroke="${vals[vals.length-1]>=0?'#2ecc71':'#e74c3c'}" stroke-width="2"/>
      <line x1="${pad}" y1="${ys(0)}" x2="${W-pad}" y2="${ys(0)}" stroke="#444" stroke-dasharray="4"/>`;
  }
  // 分币种
  document.getElementById('byCoin').innerHTML = rows(s.by_coin, ['币种','交易','胜','盈亏']);
  document.getElementById('bySide').innerHTML = rows(s.by_side, ['方向','交易','胜','盈亏']);
  // 交易明细
  const trs = s.settled.slice().reverse().map(x => `<tr>
    <td>${x.round ?? ''}</td><td>${x.slug}</td><td>${x.side}</td>
    <td>${x.entry_price}</td><td>${x.settle_yes}</td>
    <td>${x.win == 1 ? '✅' : (x.win == 0 ? '❌' : '')}</td>
    <td class="${Number(x.pnl)>0?'pos':(Number(x.pnl)<0?'neg':'')}">$${x.pnl}</td>
    <td>${x.backfilled ? '补' : ''}</td></tr>`).join('');
  document.getElementById('trades').innerHTML =
    `<tr><th>轮</th><th>盘口</th><th>方向</th><th>入场价</th><th>结算</th><th>结果</th><th>PnL</th><th></th></tr>` + trs;
}
function rows(obj, heads) {
  const keys = Object.keys(obj || {});
  if (!keys.length) return '<tr><td colspan="4">暂无</td></tr>';
  return `<tr>${heads.map(h=>`<th>${h}</th>`).join('')}</tr>` +
    keys.map(k => { const v = obj[k];
      return `<tr><td>${k}</td><td>${v.trades}</td><td>${v.wins}</td>
              <td class="${v.pnl>0?'pos':(v.pnl<0?'neg':'')}">$${v.pnl.toFixed(2)}</td></tr>`; }).join('');
}
load();
setInterval(load, INTERVAL*1000);
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path.startswith("/api/stats"):
            data = json.dumps(load_stats(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        elif self.path.startswith("/api/sessions"):
            data = json.dumps([{"name": n, "path": str(p)}
                               for n, p in load_session_files()],
                              ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, fmt, *args):
        pass  # 静默访问日志


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"PolyTrader dashboard: http://127.0.0.1:{args.port}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
