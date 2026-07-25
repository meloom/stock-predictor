"""collector_dashboard.py — the collector's coverage/backfill dashboard.

Two modes:
  python -m collector_dashboard [out.html]   # write a one-off snapshot (e.g. Artifact)
  python -m collector_dashboard serve [port] # LIVE local page — regenerated on every
                                             # request + auto-refresh; just refresh to
                                             # see the latest state.

Shows: overall backfill %, per-source quota, per-kind progress, a ticker×signal
freshness heatmap (with per-cell time detail), and the full QUEUE schedule at
per-signal × per-ticker × per-time granularity (state, last collected, next due).
"""
import json as _json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SIGNAL_LABELS = {
    "price.close": "Bars", "price.current": "Quote", "short.pct_float": "Short%",
    "opt.implied_move": "ImplMove", "fundamental.analyst_snapshot": "Analyst",
    "fundamental.statements": "Fundmls", "calendar.days_to_earnings": "Earn",
}
# signals shown in the per-ticker drill-down (feature, label)
DRILL_FEATURES = [
    ["price.close", "Bars"], ["price.volume", "Volume"], ["price.current", "Quote"],
    ["short.pct_float", "Short interest"], ["opt.implied_move", "Implied move"],
    ["fundamental.analyst_snapshot", "Analyst"], ["fundamental.statements", "Fundamentals"],
    ["calendar.days_to_earnings", "Days-to-earnings"],
    ["earnings.report_raw", "Earnings report (raw)"], ["earnings.analysis", "Earnings analysis (processed)"],
]


def _fresh_class(h):
    if h is None:
        return "miss"
    return "fresh" if h <= 24 else ("aging" if h <= 168 else "stale")


def _bar(pct, cls="run"):
    return (f'<div class="bar"><span class="fill {cls}" style="width:{pct}%"></span>'
            f'</div><span class="pct">{pct}%</span>')


def _due_label(q):
    if q["status"] == "done":
        return "done", "done"
    d = q["due_in_min"]
    if d is None:
        return "—", ""
    if d <= 0:
        return ("due now" if d == 0 else f"overdue {-d}m"), "duenow"
    if d < 90:
        return f"in {d}m", "sched"
    if d < 60 * 48:
        return f"in {round(d/60)}h", "sched"
    return f"in {round(d/1440)}d", "sched"


def render(report: dict, tickers: list[str]) -> str:
    o = report["overall"]; cols = report["matrix_cols"]; mat = report["matrix"]
    quota = "".join(f'<span class="pill"><b>{s}</b> {v}</span>' for s, v in report["quota"].items())
    cards = [("Backfill", f'{o["pct"]}%', f'{o["collected"]:,}/{o["tasks"]:,} tasks collected'),
             ("Data points", f'{o["data_points"]:,}', "rows in the store"),
             ("Due now", f'{o["due_now"]:,}', "tasks ready to run"),
             ("Signals live", f'{len(report["signals"])}', "features with data")]
    card_html = "".join(f'<div class="card"><div class="eyebrow">{t}</div>'
                        f'<div class="metric">{v}</div><div class="sub">{s}</div></div>'
                        for t, v, s in cards)

    qrows = ""
    for k in report["kinds"]:
        cls = "warn" if k["errors"] else ("ok" if k["pct"] == 100 else "run")
        last = f'{k["last_run_h"]}h ago' if k["last_run_h"] is not None else "—"
        err = f'<span class="chip crit">{k["errors"]} err</span>' if k["errors"] else ""
        due = f'<span class="chip">{k["due_now"]} due</span>' if k["due_now"] else ""
        qrows += (f'<tr><td class="mono kind">{k["kind"]}</td><td class="src">{k["source"]}</td>'
                  f'<td class="barcell">{_bar(k["pct"], cls)}</td>'
                  f'<td class="mono num">{k["collected"]}/{k["total"]}</td>'
                  f'<td>{due}{err}</td><td class="mono muted">{last}</td></tr>')

    # heatmap with per-cell time detail in the tooltip
    head = "".join(f'<th class="rot"><span>{SIGNAL_LABELS.get(c, c)}</span></th>' for c in cols)
    body = ""
    shown = [t for t in tickers if t in mat] + [t for t in tickers if t not in mat]
    for t in shown:
        cells = ""; row = mat.get(t, {})
        for c in cols:
            cell = row.get(c); h = cell["fresh_h"] if cell else None
            if cell:
                span = f'{cell["first"]}→{cell["last"]}' if cell["first"] != cell["last"] else cell["first"]
                tip = f'{t} · {SIGNAL_LABELS.get(c, c)} — {cell["count"]} pts · {span} · collected {h}h ago'
            else:
                tip = f'{t} · {SIGNAL_LABELS.get(c, c)} — no data yet'
            cells += f'<td class="cell {_fresh_class(h)}" title="{tip}"></td>'
        body += f'<tr><th class="tick mono">{t}</th>{cells}</tr>'

    # full queue schedule — per signal × ticker × time
    grows = ""
    for q in report["queue"]:
        lbl, dcls = _due_label(q)
        state = ("err" if q["error"] else ("done" if q["status"] == "done"
                 else ("duenow" if (q["due_in_min"] or 1) <= 0 else "sched")))
        col_h = f'{q["collected_h"]}h ago' if q["collected_h"] is not None else "never"
        tries = f'<span class="chip crit">×{q["attempts"]}</span>' if q["attempts"] else ""
        grows += (f'<tr><td class="mono tk">{q["scope"]}</td>'
                  f'<td class="mono kind">{q["kind"]}</td><td class="src">{q["source"]}</td>'
                  f'<td><span class="tag {state}">{state}</span></td>'
                  f'<td class="mono muted">{col_h}</td>'
                  f'<td class="mono due {dcls}">{lbl}</td><td>{tries}</td></tr>')

    srows = ""
    for s in report["signals"]:
        fresh = f'{s["fresh_h"]}h' if s["fresh_h"] is not None else "—"
        srows += (f'<tr><td class="mono">{s["feature"]}</td><td class="mono num">{s["rows"]:,}</td>'
                  f'<td class="mono num">{s["scopes"]}</td>'
                  f'<td class="mono muted">{s["latest_event"] or "—"}</td>'
                  f'<td><span class="dot {_fresh_class(s["fresh_h"])}"></span>'
                  f'<span class="mono">{fresh}</span></td></tr>')

    drill_opts = "".join(f"<option>{t}</option>" for t in shown)
    drill_json = _json.dumps(DRILL_FEATURES)

    return f"""{_CSS}
<main>
  <header>
    <div class="brand"><span class="live"></span>S1 Data Collector</div>
    <div class="gen mono">live · {report["generated_at"][:19].replace("T", " ")} UTC</div>
  </header>
  <section class="cards">{card_html}</section>
  <section class="quota"><span class="lbl">Rate-limit quota</span>{quota}</section>

  <section class="panel"><h2>Queue &amp; backfill progress</h2>
    <table class="queue"><thead><tr><th>Kind</th><th>Source</th><th>Progress</th>
    <th>Collected</th><th>State</th><th>Last run</th></tr></thead><tbody>{qrows}</tbody></table>
  </section>

  <section class="panel"><h2>Coverage &amp; freshness <span class="muted">— {len(shown)} tickers × {len(cols)} signals · hover a cell for counts &amp; date span</span></h2>
    <div class="legend"><span><i class="cell fresh"></i>≤24h</span><span><i class="cell aging"></i>≤7d</span>
    <span><i class="cell stale"></i>&gt;7d</span><span><i class="cell miss"></i>missing</span></div>
    <div class="heatwrap"><table class="heat"><thead><tr><th class="corner"></th>{head}</tr></thead>
    <tbody>{body}</tbody></table></div>
  </section>

  <section class="panel"><h2>Queue schedule <span class="muted">— every task by ticker × signal × next-run ({len(report["queue"])} tasks)</span></h2>
    <input id="qf" class="filter" placeholder="filter by ticker, signal, or state…" oninput="filterQ()">
    <div class="qwrap"><table class="qsched"><thead><tr><th>Ticker</th><th>Signal</th><th>Src</th>
    <th>State</th><th>Collected</th><th>Next run</th><th>Retries</th></tr></thead>
    <tbody id="qbody">{grows}</tbody></table></div>
  </section>

  <section class="panel"><h2>Collection detail <span class="muted">— pick a ticker, expand a signal for daily density &amp; the exact collection timestamps</span></h2>
    <select id="tk" class="filter" onchange="loadTicker()">{drill_opts}</select>
    <div id="detail" class="detail"></div>
  </section>

  <section class="panel"><h2>Signals in the store</h2>
    <table class="signals"><thead><tr><th>Feature</th><th>Rows</th><th>Tickers</th>
    <th>Latest event</th><th>Freshness</th></tr></thead><tbody>{srows}</tbody></table>
  </section>
</main>
<script>
function filterQ(){{var v=document.getElementById('qf').value.toLowerCase();
  document.querySelectorAll('#qbody tr').forEach(function(r){{
    r.style.display=r.textContent.toLowerCase().indexOf(v)>-1?'':'none';}});}}
const DRILL={drill_json};
function loadTicker(){{
  var wrap=document.getElementById('detail'); wrap.innerHTML='';
  DRILL.forEach(function(f){{
    var row=document.createElement('div'); row.className='sigrow';
    var btn=document.createElement('button'); btn.className='sigbtn'; btn.textContent='\\u25B8 '+f[1];
    var body=document.createElement('div'); body.className='sigbody';
    btn.onclick=function(){{toggleSig(btn,body,f[0],f[1]);}};
    row.appendChild(btn); row.appendChild(body); wrap.appendChild(row);
  }});
}}
async function toggleSig(btn,body,feat,label){{
  if(body.dataset.open==='1'){{body.dataset.open='0';body.innerHTML='';btn.textContent='\\u25B8 '+label;return;}}
  var tk=document.getElementById('tk').value;
  btn.textContent='\\u25BE '+label; body.innerHTML='<div class="dmeta muted">loading…</div>';
  var r=await fetch('/api/detail?scope='+encodeURIComponent(tk)+'&feature='+encodeURIComponent(feat));
  var d=await r.json(); body.dataset.open='1';
  var mx=Math.max.apply(null,[1].concat(d.daily.map(function(x){{return x.count;}})));
  var bars=d.daily.map(function(x){{var h=Math.round(4+18*x.count/mx);
    return '<span class="dbar" style="height:'+h+'px" title="'+x.date+': '+x.count+' point(s)"></span>';}}).join('');
  var ts=d.stamps.slice(0,80).map(function(s){{
    return '<tr><td class="mono">'+s.event_time+'</td><td class="mono muted">'+String(s.ingested_at).replace('T',' ').slice(0,19)+'</td></tr>';}}).join('');
  body.innerHTML='<div class="dmeta mono">'+d.total+' points \\u00b7 '+(d.first_event||'\\u2014')+' \\u2192 '+(d.last_event||'\\u2014')+'</div>'+
    '<div class="density" title="daily density (one bar per event date)">'+(bars||'<span class="muted">no data collected yet</span>')+'</div>'+
    (d.stamps.length?'<details><summary>exact collection timestamps ('+d.stamps.length+')</summary>'+
    '<div class="tswrap"><table class="ts"><thead><tr><th>event date</th><th>collected at (ingested_at)</th></tr></thead><tbody>'+ts+'</tbody></table></div></details>':'');
}}
document.addEventListener('DOMContentLoaded',loadTicker);
</script>"""


_CSS = """<style>
:root{--bg:#f6f8fa;--panel:#fff;--line:#d8dee4;--ink:#1c2128;--mut:#656d76;--accent:#0d9488;
  --fresh:#1a7f37;--aging:#9a6700;--stale:#cf222e;--miss:#eaeef2;--card:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#0e1116;--panel:#161b22;--line:#242b34;--ink:#cdd5de;
  --mut:#7d8792;--accent:#2dd4bf;--fresh:#3fb950;--aging:#d29922;--stale:#f85149;--miss:#21262d;--card:#12171e}}
:root[data-theme="light"]{--bg:#f6f8fa;--panel:#fff;--line:#d8dee4;--ink:#1c2128;--mut:#656d76;--accent:#0d9488;--fresh:#1a7f37;--aging:#9a6700;--stale:#cf222e;--miss:#eaeef2;--card:#fff}
:root[data-theme="dark"]{--bg:#0e1116;--panel:#161b22;--line:#242b34;--ink:#cdd5de;--mut:#7d8792;--accent:#2dd4bf;--fresh:#3fb950;--aging:#d29922;--stale:#f85149;--miss:#21262d;--card:#12171e}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
main{max-width:1180px;margin:0 auto;padding:32px 24px 64px}
header{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;
  padding-bottom:20px;border-bottom:1px solid var(--line);margin-bottom:24px}
.brand{font-size:20px;font-weight:650;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
.live{width:9px;height:9px;border-radius:50%;background:var(--accent);animation:pulse 2.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 60%,transparent)}70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
@media (prefers-reduced-motion:reduce){.live{animation:none}}
.gen{font-size:12px;color:var(--mut)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);font-weight:600}
.metric{font-size:30px;font-weight:680;letter-spacing:-.02em;margin:4px 0 2px;font-family:ui-monospace,Menlo,monospace;font-variant-numeric:tabular-nums}
.sub{font-size:12.5px;color:var(--mut)}
.quota{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:26px}
.quota .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);font-weight:600}
.pill{font-family:ui-monospace,monospace;font-size:12px;background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:5px 12px}
.pill b{color:var(--accent)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin-bottom:22px}
h2{font-size:15px;font-weight:640;margin:0 0 16px;letter-spacing:-.01em}
h2 .muted{font-weight:400;font-size:13px}.muted{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}
.queue th,.signals th,.qsched th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:600;padding:0 10px 10px;border-bottom:1px solid var(--line)}
.queue td,.signals td{padding:9px 10px;border-bottom:1px solid var(--line)}
.queue tr:last-child td,.signals tr:last-child td{border-bottom:none}
.kind{font-weight:600;color:var(--ink)}.src{color:var(--mut);font-size:12px}
.num{text-align:right}.barcell{width:42%}
.bar{display:inline-block;width:calc(100% - 46px);height:7px;border-radius:4px;background:var(--miss);vertical-align:middle;overflow:hidden}
.fill{display:block;height:100%;border-radius:4px;background:var(--accent)}
.fill.ok{background:var(--fresh)}.fill.warn{background:var(--stale)}.fill.run{background:var(--accent)}
.pct{display:inline-block;width:40px;text-align:right;font-family:ui-monospace,monospace;font-size:12px;color:var(--mut)}
.chip{font-family:ui-monospace,monospace;font-size:11px;padding:2px 7px;border-radius:6px;background:var(--miss);color:var(--mut);margin-right:5px}
.chip.crit{background:color-mix(in srgb,var(--stale) 18%,transparent);color:var(--stale)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--mut);margin-bottom:14px}
.legend i{width:12px;height:12px;border-radius:3px;display:inline-block;vertical-align:-2px;margin-right:5px}
.heatwrap{overflow-x:auto}
.heat{border-collapse:separate;border-spacing:2px}
.heat .corner{width:56px}
.heat th.rot{height:70px;width:26px;padding:0;vertical-align:bottom}
.heat th.rot span{display:inline-block;writing-mode:vertical-rl;transform:rotate(180deg);font-size:11px;color:var(--mut);font-weight:600;padding-bottom:6px}
.heat .tick{text-align:right;font-size:11px;color:var(--mut);padding-right:8px;font-weight:500;position:sticky;left:0;background:var(--panel)}
.cell{width:26px;height:16px;border-radius:3px;background:var(--miss);padding:0}
.cell.fresh{background:var(--fresh)}.cell.aging{background:var(--aging)}.cell.stale{background:var(--stale)}
.cell.miss{background:var(--miss);border:1px solid var(--line)}
.filter{width:100%;max-width:340px;margin-bottom:12px;padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font-size:13px;font-family:ui-monospace,monospace}
.filter:focus{outline:2px solid var(--accent);outline-offset:1px}
.qwrap{max-height:460px;overflow:auto;border:1px solid var(--line);border-radius:10px}
.qsched{font-size:12.5px}
.qsched thead th{position:sticky;top:0;background:var(--panel);z-index:1;padding:10px}
.qsched td{padding:7px 10px;border-bottom:1px solid var(--line)}
.qsched .tk{font-weight:600}
.tag{font-family:ui-monospace,monospace;font-size:10.5px;text-transform:uppercase;letter-spacing:.04em;padding:2px 7px;border-radius:5px;background:var(--miss);color:var(--mut)}
.tag.done{background:color-mix(in srgb,var(--fresh) 16%,transparent);color:var(--fresh)}
.tag.duenow{background:color-mix(in srgb,var(--accent) 18%,transparent);color:var(--accent)}
.tag.err{background:color-mix(in srgb,var(--stale) 18%,transparent);color:var(--stale)}
.tag.sched{background:var(--miss);color:var(--mut)}
.due.duenow{color:var(--accent);font-weight:600}.due.done{color:var(--fresh)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:7px;background:var(--miss)}
.dot.fresh{background:var(--fresh)}.dot.aging{background:var(--aging)}.dot.stale{background:var(--stale)}
.detail{margin-top:6px}
.sigrow{border-bottom:1px solid var(--line)}.sigrow:last-child{border-bottom:none}
.sigbtn{width:100%;text-align:left;background:none;border:none;color:var(--ink);font:inherit;
  font-size:13px;font-weight:600;padding:11px 4px;cursor:pointer;font-family:ui-monospace,monospace}
.sigbtn:hover{color:var(--accent)}.sigbtn:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.sigbody{padding:0 4px}.sigbody:empty{padding:0}
.dmeta{font-size:12px;padding:2px 0 10px}
.density{display:flex;align-items:flex-end;gap:2px;min-height:24px;padding:6px 0 12px;flex-wrap:wrap}
.dbar{width:5px;background:var(--fresh);border-radius:1px;display:inline-block}
details{margin:0 0 12px}summary{cursor:pointer;font-size:12px;color:var(--accent);padding:4px 0}
.tswrap{max-height:240px;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:8px}
.ts{font-size:12px}.ts th{position:sticky;top:0;background:var(--panel);text-align:left;padding:8px 10px;
  font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);border-bottom:1px solid var(--line)}
.ts td{padding:5px 10px;border-bottom:1px solid var(--line)}
</style>"""


def _page(report, tickers, auto_refresh=0):
    meta = f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh else ""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{meta}<title>S1 Data Collector</title></head><body>'
            + render(report, tickers) + '</body></html>')


def serve(port=8787):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs
    from collector import default_collector
    from universe import UNIVERSE

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            col = default_collector()
            parsed = urlparse(self.path)
            if parsed.path == "/api/detail":          # drill-down: one ticker × signal
                q = parse_qs(parsed.query)
                payload = _json.dumps(col.signal_detail(
                    q.get("scope", [""])[0], q.get("feature", [""])[0])).encode()
                ctype = "application/json"
            else:                                     # the dashboard, regenerated live
                payload = _page(col.coverage_report(), UNIVERSE, auto_refresh=0).encode()
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)

        def log_message(self, *a):
            pass

    print(f"S1 collector dashboard → http://localhost:{port}  (live; refresh to update, Ctrl-C to stop)")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve(int(sys.argv[2]) if len(sys.argv) > 2 else 8787)
        return
    from collector import default_collector
    from universe import UNIVERSE
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("runtime/collector_dashboard.html")
    report = default_collector().coverage_report()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report, UNIVERSE))
    print(f"wrote {out}  (backfill {report['overall']['pct']}%, {report['overall']['data_points']:,} points)")


if __name__ == "__main__":
    main()
