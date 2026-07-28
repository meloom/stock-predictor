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
try:
    from core import MARKET_SCOPE
    from schema import SCHEMA as TYPED_SCHEMA
except Exception:
    MARKET_SCOPE = "_market"; TYPED_SCHEMA = {}

SIGNAL_LABELS = {
    "price.close": "Bars", "price.current": "Quote", "short.pct_float": "Short%",
    "opt.implied_move": "ImplMove", "fundamental.analyst_snapshot": "Analyst",
    "fundamental.statements": "Fundmls", "earnings.next_date": "NextEarn",
}
# signals shown in the per-ticker drill-down (feature, label)
# drill-down features -> the SAME typed-table names used by the inspector + coverage
# matrix (one canonical name per signal, no kind/label drift). `bars` covers OHLCV
# incl. volume — volume is a column of bars, not its own signal.
DRILL_FEATURES = [
    ["price.close", "bars"], ["price.current", "quotes"],
    ["short.pct_float", "short_interest"], ["opt.implied_move", "options_implied"],
    ["fundamental.analyst_snapshot", "analyst_snapshot"],
    ["fundamental.statements", "fundamentals"],
    ["earnings.next_date", "earnings_calendar"],        # S1 raw; days_to_earnings is S2
    ["earnings.report_raw", "earnings_reports"],        # S1 raw; earnings.analysis is S2
    ["insider.transactions_raw", "insider_transactions"],
    ["analyst.revisions_raw", "analyst_revisions"],
]


def _fresh_class(h):
    if h is None:
        return "miss"
    return "fresh" if h <= 24 else ("aging" if h <= 168 else "stale")


def _fresh_class_sla(sec, sla):
    """Color a cell by its signal's own freshness SLA: ≤green→fresh, ≤yellow→aging,
    else stale; None→missing. (Quote's SLA is 5min/30min, so a stale quote reddens
    within the hour, while a daily signal stays green for a day.)"""
    if sec is None:
        return "miss"
    green, yellow = sla
    return "fresh" if sec <= green else ("aging" if sec <= yellow else "stale")


def _dur(sec):
    if sec is None:
        return "—"
    if sec < 90:
        return f"{sec}s"
    if sec < 5400:
        return f"{round(sec/60)}m"
    if sec < 172800:
        return f"{round(sec/3600)}h"
    return f"{round(sec/86400)}d"


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
    bm = o.get("by_mode", {})
    mode_sub = " · ".join(f'{m} {p}%' for m, p in sorted(bm.items())) or "—"
    cards = [("Coverage vs expected", f'{o["pct"]}%',
              f'{o.get("kinds_full", 0)}/{o.get("kinds_total", 0)} signals fully covered · {mode_sub}'),
             ("Data points", f'{o["data_points"]:,}', "rows in the store"),
             ("Due now", f'{o["due_now"]:,}', "tasks ready to run"),
             ("Signals live", f'{len(report["signals"])}', "features with data")]
    card_html = "".join(f'<div class="card"><div class="eyebrow">{t}</div>'
                        f'<div class="metric">{v}</div><div class="sub">{s}</div></div>'
                        for t, v, s in cards)

    # mode → how to read the bar. history is a true depth %; snapshot/rolling bars
    # measure BREADTH (tickers covered), never "history complete" — the chip says so.
    MODE_CLS = {"history": "hist", "snapshot": "snap", "rolling": "roll"}
    qrows = ""
    for k in report["kinds"]:
        mode = k.get("mode", "snapshot")
        cls = "warn" if k["errors"] else MODE_CLS.get(mode, "run")
        last = f'{k["last_run_h"]}h ago' if k["last_run_h"] is not None else "—"
        err = f'<span class="chip crit">{k["errors"]} err</span>' if k["errors"] else ""
        due = f'<span class="chip">{k["due_now"]} due</span>' if k["due_now"] else ""
        freq = k.get("frequency", mode)
        chip = f'<span class="mchip {MODE_CLS.get(mode,"run")}" title="{k.get("expect","")}">{freq}</span>'
        qrows += (f'<tr><td class="mono kind">{k["kind"]} {chip}</td><td class="src">{k["source"]}</td>'
                  f'<td class="barcell">{_bar(k["pct"], cls)}</td>'
                  f'<td class="detail muted">{k.get("detail","")}</td>'
                  f'<td>{due}{err}</td><td class="mono muted">{last}</td></tr>')

    # ticker order for the selectors — tickers with data first (the per-ticker timeline
    # coverage replaced the old tickers×signals freshness heatmap)
    shown = [t for t in tickers if t in mat] + [t for t in tickers if t not in mat]

    # full queue schedule — per signal × ticker × time
    # hourly collection activity per source (last 72h)
    hy = report.get("hourly", {"labels": [], "sources": {}, "totals": {}})
    SRC_CLS = {"polygon": "#3b82c4", "yfinance": "#3fb27f", "sec": "#c9930b"}
    RED, OKC, IDLE = "#e5484d", "#3fb27f", "var(--line)"
    hlabels = hy["labels"]
    srcs = list(hy["sources"].keys())
    n_h = len(hlabels)
    fails = hy.get("fails", {s: [0] * n_h for s in srcs})
    attempts = hy.get("attempts", {s: [0] * n_h for s in srcs})
    totals_h = [sum(hy["sources"][s][i] for s in srcs) for i in range(n_h)]
    fail_h = [sum(fails.get(s, [0] * n_h)[i] for s in srcs) for i in range(n_h)]
    att_h = [sum(attempts.get(s, [0] * n_h)[i] for s in srcs) for i in range(n_h)]
    hmax = max(totals_h + [1])
    BW, GAP, PLOT_H, PAD_L, PAD_T = 12, 3, 190, 52, 12
    STRIP_Y, STRIP_H, PAD_B = PLOT_H + 8, 9, 62          # status strip sits below the bars
    plot_w = n_h * (BW + GAP)
    svg_w = PAD_L + plot_w + 12
    svg_h = PAD_T + PLOT_H + PAD_B

    def _fmt(n):
        return f"{n/1000:.0f}k" if n >= 1000 else str(n)
    grid = ""
    for f in (0, .25, .5, .75, 1):
        y = PAD_T + PLOT_H - f * PLOT_H
        grid += (f'<line x1="{PAD_L}" y1="{y:.1f}" x2="{PAD_L+plot_w}" y2="{y:.1f}" '
                 f'class="hgrid"/><text x="{PAD_L-8}" y="{y+3:.1f}" class="hyl">{_fmt(int(f*hmax))}</text>')
    grid += f'<text x="{PAD_L-8}" y="{PAD_T+STRIP_Y+STRIP_H}" class="hyl">ok?</text>'
    bars = ""
    for i in range(n_h):
        x = PAD_L + i * (BW + GAP)
        y0 = PAD_T + PLOT_H
        # green volume bars (rows written), stacked per source
        vol = " · ".join(f'{s} {hy["sources"][s][i]:,}' for s in srcs if hy["sources"][s][i])
        state = "FAILING" if fail_h[i] else ("ok" if (att_h[i] or totals_h[i]) else "idle (nothing due)")
        tip = (f'{hlabels[i]} — {state}'
               f'{" · rows: " + vol if vol else ""}'
               f'{f" · {fail_h[i]}/{att_h[i]} attempts FAILED" if fail_h[i] else ""}')
        seg = f'<title>{tip}</title>'
        for s in srcs:
            v = hy["sources"][s][i]
            if v <= 0:
                continue
            h = v / hmax * PLOT_H
            y0 -= h
            seg += f'<rect x="{x}" y="{y0:.1f}" width="{BW}" height="{h:.1f}" fill="{SRC_CLS[s]}"/>'
        # status strip: red = a failure this hour, green = attempts all ok, grey = idle
        sc = RED if fail_h[i] else (OKC if (att_h[i] or totals_h[i]) else IDLE)
        seg += (f'<rect x="{x}" y="{PAD_T+STRIP_Y}" width="{BW}" height="{STRIP_H}" rx="1.5" '
                f'fill="{sc}"/>')
        bars += f'<g class="hbar">{seg}</g>'
        if (n_h - 1 - i) % 6 == 0:
            bars += (f'<text x="{x+BW/2:.1f}" y="{PAD_T+STRIP_Y+STRIP_H+14}" class="hxl">{hlabels[i]}</text>')
    fail_total = sum(fail_h)
    legend = "".join(f'<span class="hleg"><i style="background:{SRC_CLS[s]}"></i>{s} '
                     f'<b>{hy["totals"].get(s,0):,}</b></span>' for s in srcs)
    legend += (f'<span class="hleg"><i style="background:{OKC}"></i>ok hr</span>'
               f'<span class="hleg"><i style="background:{RED}"></i>failing hr '
               f'<b>{fail_total}</b></span>'
               f'<span class="hleg"><i style="background:{IDLE}"></i>idle hr</span>')
    hchart_svg = (f'<svg viewBox="0 0 {svg_w} {svg_h}" width="{svg_w}" height="{svg_h}" '
                  f'class="hchart" role="img">{grid}{bars}</svg>')

    def _ts(iso):   # ISO -> 'YYYY-MM-DD HH:MM' (minute level)
        return iso[:16].replace("T", " ") if iso else "—"
    grows = ""
    for q in report["queue"]:
        lbl, dcls = _due_label(q)
        state = ("err" if q["error"] else ("done" if q["status"] == "done"
                 else ("duenow" if (q["due_in_min"] or 1) <= 0 else "sched")))
        collected = _ts(q.get("last_ok")) if q.get("last_ok") else "never"
        nxt = _ts(q.get("next_due"))
        rel = f'<span class="muted"> · {lbl}</span>' if q["status"] != "done" else ""
        tries = f'<span class="chip crit">×{q["attempts"]}</span>' if q["attempts"] else ""
        grows += (f'<tr><td class="mono tk">{q["scope"]}</td>'
                  f'<td class="mono kind">{q["kind"]}</td><td class="src">{q["source"]}</td>'
                  f'<td><span class="tag {state}">{state}</span></td>'
                  f'<td class="mono muted">{collected}</td>'
                  f'<td class="mono due {dcls}">{nxt}{rel}</td><td>{tries}</td></tr>')

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
    tbl_opts = "".join(f"<option>{t}</option>" for t in TYPED_SCHEMA)

    # ── universe-wide S1 coverage matrix (server-rendered; typed store; weekly) ──
    sc = report.get("s1_coverage", {"weeks": [], "signals": [], "universe": 0})
    U = sc["universe"]; wl = sc["weeks"]; nW = len(wl)
    whead = "".join(
        f'<i class="s1hc">{wl[i][5:] if (nW - 1 - i) % 3 == 0 else ""}</i>' for i in range(nW))
    s1rows = ""
    for s in sc["signals"]:
        cells = ""
        for i, f in enumerate(s["cells"]):
            h = int(f * 120)                                   # 0=red → 120=green
            lab = ("live" if s["denom"] == 1 else f'{round(f * U)}/{U}')
            cells += (f'<i class="s1c" style="background:hsl({h},55%,44%)" '
                      f'title="week of {wl[i]} — {lab} ({f:.0%})"></i>')
        cov = "market" if s["denom"] == 1 else f'{s["covered"]}/{U}'
        cov_cls = "ok" if (s["denom"] == 1 or s["covered"] >= 0.9 * U) else \
                  ("warn" if s["covered"] >= 0.5 * U else "bad")
        s1rows += (f'<div class="s1row"><div class="s1lab">'
                   f'<span class="s1feat">{s["signal"]}</span>'
                   f'<span class="s1cad">{s["cadence"]}·{s["source"]}</span></div>'
                   f'<div class="s1cov {cov_cls}">{cov}</div>'
                   f'<div class="s1strip">{cells}</div></div>')
    s1cov_html = (f'<div class="s1wrap"><div class="s1grid">'
                  f'<div class="s1row s1head"><div class="s1lab">signal</div>'
                  f'<div class="s1cov">covered</div><div class="s1strip">{whead}</div></div>'
                  f'{s1rows}</div></div>')

    return f"""{_CSS}
<main>
  <header>
    <div class="brand"><span class="live"></span>S1 Data Collector</div>
    <div class="gen mono">live · {report["generated_at"][:19].replace("T", " ")} UTC</div>
  </header>
  {_flow_nav("S1")}
  <section class="cards">{card_html}</section>
  <section class="quota"><span class="lbl">Rate-limit quota</span>{quota}</section>
  <section class="panel"><h2>Download rate &amp; failures per source <span class="muted">— bars = rows collected per hour (last 72h). Strip below each bar: green = attempts ok, <b style="color:#e5484d">red = a collection FAILED that hour</b>, grey = idle (nothing due). Hover for detail.</span></h2>
    <div class="hlegend">{legend}</div>
    <div class="hwrap">{hchart_svg}</div></section>

  <section class="panel"><h2>Coverage vs expectation
    <span class="muted">— <b class="hist">history</b> = % of the expected daily window backfilled ·
    <b class="snap">snapshot</b> = tickers with a current value (history accrues forward, not backfillable) ·
    <b class="roll">rolling</b> = tickers covered by the source's event history</span></h2>
    <table class="queue"><thead><tr><th>Kind</th><th>Source</th><th>Coverage</th>
    <th>What we hold vs. expected</th><th>State</th><th>Last run</th></tr></thead><tbody>{qrows}</tbody></table>
  </section>

  <section class="panel"><h2>Signal coverage across the universe
    <span class="muted">— <b>S1 raw collection only</b> (S2+ derived signals live on their own dashboards).
    Weekly buckets, measured from the typed store. <b>covered</b> = tickers we hold this signal for;
    each cell = share of the {U} tickers with a record that week (<span style="color:hsl(120,55%,44%)">green</span>=all →
    <span style="color:hsl(0,55%,44%)">red</span>=none). Event signals are sparse by nature — read their
    <b>covered</b> count, not weekly density. Hover a cell for counts.</span></h2>
    {s1cov_html}
  </section>

  <section class="panel"><h2>Queue schedule <span class="muted">— every task by ticker × signal × next-run ({len(report["queue"])} tasks)</span></h2>
    <input id="qf" class="filter" placeholder="filter by ticker, signal, or state…" oninput="filterQ()">
    <div class="qwrap"><table class="qsched"><thead><tr><th>Ticker</th><th>Signal</th><th>Src</th>
    <th>State</th><th>Last collected (UTC)</th><th>Next run (UTC)</th><th>Retries</th></tr></thead>
    <tbody id="qbody">{grows}</tbody></table></div>
  </section>

  <section class="panel"><h2>Raw data inspector <span class="muted">— exact typed rows per table: real columns + the source timestamp each value was logged at</span></h2>
    <div class="rawctrl">
      <select id="rtbl">{tbl_opts}</select>
      <input id="rticker" value="AAPL" placeholder="ticker (blank = all; macro ignores)" spellcheck="false">
      <button id="rgo" onclick="showTyped()">Show rows</button>
    </div>
    <div id="rawout"></div>
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
  function strip(arr,keyf,lab){{
    var mx=Math.max.apply(null,[1].concat(arr.map(function(x){{return x.count;}})));
    var bars=arr.map(function(x){{var h=Math.round(4+18*x.count/mx);
      return '<span class="dbar" style="height:'+h+'px" title="'+keyf(x)+': '+x.count+' point(s)"></span>';}}).join('');
    return '<div class="striplab mono">'+lab+'</div><div class="density">'+(bars||'<span class="muted">none</span>')+'</div>';
  }}
  var ts=d.stamps.slice(0,120).map(function(s){{
    return '<tr><td class="mono">'+s.event_time+'</td><td class="mono muted">'+String(s.ingested_at).replace('T',' ').slice(0,19)+'</td></tr>';}}).join('');
  body.innerHTML='<div class="dmeta mono">'+d.total+' points \\u00b7 events '+(d.first_event||'\\u2014')+' \\u2192 '+(d.last_event||'\\u2014')+'</div>'+
    strip(d.daily,function(x){{return x.date;}},'coverage by event date (1 bar / day)')+
    strip(d.by_5min,function(x){{return x.t;}},'collection cadence (1 bar / 5 min, ingested_at)')+
    (d.stamps.length?'<details><summary>exact timestamps to the second ('+d.stamps.length+')</summary>'+
    '<div class="tswrap"><table class="ts"><thead><tr><th>event time</th><th>collected at (ingested_at)</th></tr></thead><tbody>'+ts+'</tbody></table></div></details>':'');
}}
async function showTyped(){{
  var table=document.getElementById('rtbl').value;
  var ticker=document.getElementById('rticker').value.trim().toUpperCase();
  var out=document.getElementById('rawout'); out.innerHTML='<div class="dmeta muted">loading…</div>';
  var r=await fetch('/api/typed?table='+encodeURIComponent(table)+'&ticker='+encodeURIComponent(ticker));
  var d=await r.json();
  if(!d.rows||!d.rows.length){{out.innerHTML='<div class="dmeta muted">no rows in '+table+(ticker?' for '+ticker:'')+'</div>';return;}}
  function esc(x){{return (x===null||x===undefined)?'':String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;');}}
  var th=d.columns.map(function(c){{return '<th'+(c===d.ts_col?' class="tscol"':'')+'>'+esc(c)+'</th>';}}).join('');
  var tr=d.rows.map(function(row){{return '<tr>'+d.columns.map(function(c){{
    return '<td class="mono'+(c===d.ts_col?' tscol':'')+'">'+esc(row[c])+'</td>';}}).join('')+'</tr>';}}).join('');
  out.innerHTML='<div class="dmeta mono">'+esc(table)+(ticker?' \\u00b7 '+esc(ticker):'')+' \\u2014 '+d.rows.length+' rows \\u00b7 source-timestamp column: <b>'+esc(d.ts_col)+'</b></div>'+
    '<div class="tswrap"><table class="ts"><thead><tr>'+th+'</tr></thead><tbody>'+tr+'</tbody></table></div>';
}}
document.addEventListener('DOMContentLoaded',function(){{loadTicker();showTyped();}});
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
/* mode-colored fills: history=green depth, snapshot=amber breadth, rolling=blue breadth */
.fill.hist{background:var(--fresh)}.fill.snap{background:#c9930b}.fill.roll{background:#3b82c4}
b.hist,.mchip.hist{color:var(--fresh)}b.snap,.mchip.snap{color:#c9930b}b.roll,.mchip.roll{color:#3b82c4}
.mchip{font-family:ui-monospace,monospace;font-size:10px;padding:1px 6px;border-radius:5px;margin-left:6px;
  border:1px solid currentColor;opacity:.85;text-transform:uppercase;letter-spacing:.4px}
td.detail{font-size:12px;color:var(--mut);max-width:340px}
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
/* universe-wide S1 coverage matrix */
.s1wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;max-height:600px;overflow-y:auto}
.s1grid{min-width:620px}
.s1row{display:flex;align-items:stretch;height:22px;border-bottom:1px solid var(--line)}
.s1row:last-child{border-bottom:none}
.s1row.s1head{height:26px;position:sticky;top:0;z-index:2;background:var(--panel);
  font-size:11px;color:var(--mut);border-bottom:1px solid var(--line)}
.s1lab{flex:0 0 220px;width:220px;display:flex;justify-content:space-between;align-items:center;gap:8px;
  padding:0 10px;overflow:hidden;position:sticky;left:0;background:var(--panel);border-right:1px solid var(--line)}
.s1feat{font-family:ui-monospace,monospace;font-size:12px;color:var(--ink);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.s1cad{font-size:9px;color:var(--mut);white-space:nowrap;flex:0 0 auto}
.s1cov{flex:0 0 66px;width:66px;display:flex;align-items:center;justify-content:flex-end;
  padding:0 10px;font-size:11px;font-variant-numeric:tabular-nums;border-right:1px solid var(--line)}
.s1cov.ok{color:var(--fresh)}.s1cov.warn{color:var(--aging)}.s1cov.bad{color:var(--stale)}
.s1strip{flex:1 1 auto;display:flex;min-width:0}
.s1c{flex:1 1 0;min-width:0}
.s1hc{flex:1 1 0;min-width:0;font-size:9px;color:var(--mut);font-family:ui-monospace,monospace;
  white-space:nowrap;overflow:visible;line-height:26px;padding-left:2px}
.hwrap{overflow-x:auto;padding-bottom:4px}
.hchart{display:block}
.hgrid{stroke:var(--line);stroke-width:1;stroke-dasharray:2 3}
.hyl{fill:var(--mut);font-size:10px;text-anchor:end;font-variant-numeric:tabular-nums}
.hxl{fill:var(--mut);font-size:10px;text-anchor:middle}
.hbar rect{transition:opacity .1s}
.hbar:hover rect{opacity:.75}
.hlegend{display:flex;gap:16px;margin-bottom:10px;font-size:12px;color:var(--mut);flex-wrap:wrap}
.hleg{display:inline-flex;align-items:center;gap:6px}
.hleg i{width:11px;height:11px;border-radius:2px;display:inline-block}
.hleg b{color:var(--ink);font-variant-numeric:tabular-nums}
.filter:focus{outline:2px solid var(--accent);outline-offset:1px}
.rawctrl{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:12px}
.rawctrl select,.rawctrl input{padding:8px 12px;border-radius:8px;border:1px solid var(--line);background:var(--bg);color:var(--ink);font-size:13px;font-family:ui-monospace,monospace}
.rawctrl select{min-width:230px}.rawctrl input{width:180px}
.rawctrl select:focus,.rawctrl input:focus{outline:2px solid var(--accent);outline-offset:1px}
.rawctrl button{padding:8px 16px;border-radius:8px;border:1px solid var(--accent);background:var(--accent);color:#fff;font-size:13px;font-weight:600;cursor:pointer}
.rawctrl button:hover{filter:brightness(1.08)}
.rawval{max-width:640px;white-space:pre-wrap;word-break:break-word;color:var(--ink)}
.ts th.tscol,.ts td.tscol{color:var(--accent);font-weight:600}
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
.striplab{font-size:10.5px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);margin-top:4px}
.density{display:flex;align-items:flex-end;gap:2px;min-height:24px;padding:6px 0 12px;flex-wrap:wrap}
.dbar{width:5px;background:var(--fresh);border-radius:1px;display:inline-block}
details{margin:0 0 12px}summary{cursor:pointer;font-size:12px;color:var(--accent);padding:4px 0}
.tswrap{max-height:240px;overflow:auto;border:1px solid var(--line);border-radius:8px;margin-top:8px}
.ts{font-size:12px}.ts th{position:sticky;top:0;background:var(--panel);text-align:left;padding:8px 10px;
  font-size:11px;text-transform:uppercase;letter-spacing:.05em;color:var(--mut);border-bottom:1px solid var(--line)}
.ts td{padding:5px 10px;border-bottom:1px solid var(--line)}
</style>"""


_STEPS = [
    ("♥", "Collector Health", "/health", True,
     "Live heartbeat, per-signal freshness, stall detection + alert history — is collection actually running?"),
    ("S1", "Data Collection", "/data-collection", True,
     "Queue-driven collector: backfill progress, freshness heatmap, per-ticker×signal drill-down."),
    ("S2", "Signal Processing", "/signal-processing", True,
     "Feature lineage S1→S2→S3→S4, coverage, and the gaps: collected data not yet turned into features."),
    ("S3", "Predictors", "/predictors", True,
     "Multi-horizon predictor models, OOS skill (IC / hit-rate), confidence intervals, production status."),
    ("S4", "Alpha Report", "/alpha", True,
     "Per-stock alpha report: factors, catalysts, valuation, risk, positioning, predictor efficacy."),
    ("§5", "Backtest & P&L", None, False, "Cost-aware walk-forward returns."),
    ("★", "Top &amp; Bottom Picks", "/picks", True,
     "Whole universe ranked by predictor conviction — the day's long and short candidates."),
    ("↳", "Single Stock", "/single-stock", True,
     "One ticker across every step: all S1→S4 signals + the full raw collected rows."),
]


def _index_page():
    cards = ""
    for stage, name, href, live, desc in _STEPS:
        tag = '<span class="s-live">live</span>' if live else '<span class="s-soon">soon</span>'
        open_a = f'<a class="scard" href="{href}">' if href else '<div class="scard off">'
        close_a = "</a>" if href else "</div>"
        cards += (f'{open_a}<div class="s-top"><span class="s-stage">{stage}</span>{tag}</div>'
                  f'<div class="s-name">{name}</div><div class="s-desc">{desc}</div>{close_a}')
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Pipeline dashboards</title>{_CSS}<style>'
            '.wrap{max-width:900px;margin:0 auto;padding:48px 24px}'
            '.wrap h1{font-size:22px;font-weight:660;letter-spacing:-.02em;margin:0 0 6px}'
            '.wrap p.lede{color:var(--mut);margin:0 0 28px}'
            '.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px}'
            '.scard{display:block;text-decoration:none;color:inherit;background:var(--card);'
            'border:1px solid var(--line);border-radius:14px;padding:20px;transition:border-color .15s}'
            '.scard:hover{border-color:var(--accent)}.scard.off{opacity:.55}'
            '.s-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}'
            '.s-stage{font-family:ui-monospace,monospace;font-size:12px;color:var(--mut);font-weight:600}'
            '.s-live{font-family:ui-monospace,monospace;font-size:10.5px;text-transform:uppercase;'
            'letter-spacing:.05em;color:var(--fresh);background:color-mix(in srgb,var(--fresh) 15%,transparent);padding:2px 8px;border-radius:20px}'
            '.s-soon{font-family:ui-monospace,monospace;font-size:10.5px;text-transform:uppercase;'
            'letter-spacing:.05em;color:var(--mut);background:var(--miss);padding:2px 8px;border-radius:20px}'
            '.s-name{font-size:16px;font-weight:640;margin-bottom:4px}.s-desc{font-size:12.5px;color:var(--mut);line-height:1.5}'
            '</style></head><body><div class="wrap">'
            '<h1>stock-predictor · pipeline dashboards</h1>'
            '<p class="lede">One dashboard per stage. Data Collection is live; the rest arrive as each stage gets its own view.</p>'
            f'<div class="grid">{cards}</div></div></body></html>')


def _page(report, tickers, auto_refresh=0):
    meta = f'<meta http-equiv="refresh" content="{auto_refresh}">' if auto_refresh else ""
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'{meta}<title>S1 Data Collector</title></head><body>'
            + render(report, tickers) + '</body></html>')


def render_s2(rep: dict) -> str:
    UNIV = 109
    groups = rep["groups"]
    total_feats = rep["s2_feature_count"]
    produced = sum(1 for g in groups for f in g["features"] if f["scopes"] > 0)
    gaps = rep["gaps"]
    n_missing = sum(1 for g in gaps if g["severity"] == "missing")

    cards = [("S2 features", f'{produced}/{total_feats}', "produced (≥1 ticker)"),
             ("Feature groups", f'{len(groups)}', "lineage families"),
             ("Gaps", f'{len(gaps)}', f'{n_missing} missing · {len(gaps)-n_missing} unused'),
             ("Universe", f'{UNIV}', "target tickers/feature")]
    card_html = "".join(f'<div class="card"><div class="eyebrow">{t}</div>'
                        f'<div class="metric">{v}</div><div class="sub">{s}</div></div>'
                        for t, v, s in cards)

    # lineage groups — each with per-feature coverage
    grp = ""
    for g in groups:
        warn = "⚠" in g["consumer"]
        head = (f'<tr class="grphead"><td colspan="5"><span class="gname">{g["group"]}</span>'
                f'<span class="gflow"><b>from</b> {g["derived_from"]} &nbsp;<b>→</b> '
                f'<span class="{"gwarn" if warn else "gto"}">{g["consumer"]}</span></span></td></tr>')
        rows = ""
        for f in g["features"]:
            pct = round(100 * f["scopes"] / UNIV)
            cls = "ok" if pct >= 90 else ("run" if pct > 0 else "warn")
            span = (f'{(f["first"] or "?")[:10]}→{(f["latest"] or "?")[:10]}'
                    if f["n_dates"] else "—")
            rows += (f'<tr><td class="mono feat">{f["feature"]}</td>'
                     f'<td class="barcell">{_bar(pct, cls)}</td>'
                     f'<td class="mono num">{f["scopes"]}/{UNIV}</td>'
                     f'<td class="mono num">{f["n_dates"]}</td>'
                     f'<td class="mono muted">{span}</td></tr>')
        grp += head + rows

    # downstream contract
    con = ""
    for consumer, feats in rep["contract"].items():
        ok = sum(1 for f in feats if f["produced"])
        con += (f'<tr class="grphead"><td colspan="3"><span class="gname">{consumer}</span>'
                f'<span class="gflow">{ok}/{len(feats)} inputs produced</span></td></tr>')
        for f in feats:
            mark = ('<span class="tag done">produced</span>' if f["produced"]
                    else '<span class="tag err">absent</span>')
            con += (f'<tr><td class="mono feat">{f["feature"]}</td><td>{mark}</td>'
                    f'<td class="mono muted">{f["scopes"]} tickers</td></tr>')

    # gaps
    gp = ""
    for g in gaps:
        sev = ("crit" if g["severity"] == "missing" else "warn")
        gp += (f'<div class="gap"><div class="gaptop"><span class="mono gsig">{g["signal"]}</span>'
               f'<span class="chip {sev}">{g["severity"]}</span>'
               f'<span class="muted">· S1 has {g["s1_tickers"]} tickers of data</span></div>'
               f'<div class="gapissue">{g["issue"]}</div>'
               f'<div class="gapfix"><b>proposed:</b> {g["proposed"]}</div></div>')

    steps = ('<div class="flow"><span class="fstep done">S1 collect</span>→'
             '<span class="fstep cur">S2 process</span>→'
             '<span class="fstep">S3 predict</span>→<span class="fstep">S4 alpha</span></div>')
    return f"""{_CSS}
<style>
.flow{{display:flex;gap:8px;align-items:center;font-size:13px;margin:10px 0 4px;color:var(--mut)}}
.fstep{{padding:3px 10px;border:1px solid var(--line);border-radius:16px}}
.fstep.done{{color:var(--fresh);border-color:color-mix(in srgb,var(--fresh) 40%,transparent)}}
.fstep.cur{{color:var(--accent);border-color:var(--accent);font-weight:640}}
.grphead td{{padding-top:16px;border-bottom:1px solid var(--line)}}
.gname{{font-weight:660;font-size:13px;margin-right:12px}}
.gflow{{font-size:12px;color:var(--mut)}}.gflow b{{color:var(--fg);font-weight:600}}
.gto{{color:var(--fresh)}}.gwarn{{color:#c9930b;font-weight:600}}
.feat{{font-size:12.5px}}.gap{{border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin-bottom:10px}}
.gaptop{{display:flex;align-items:center;gap:10px;margin-bottom:6px}}
.gsig{{font-weight:640}}.gapissue{{font-size:13px;line-height:1.5;margin-bottom:6px}}
.gapfix{{font-size:12.5px;color:var(--mut)}}.gapfix b{{color:var(--accent)}}
.chip.warn{{background:color-mix(in srgb,#c9930b 18%,transparent);color:#c9930b}}
</style>
<main>
  <header>
    <div class="brand"><span class="live"></span>S2 Signal Processing</div>
    <div class="gen mono">live · {rep["generated_at"][:19].replace("T", " ")} UTC</div>
  </header>
  {steps}
  <section class="cards">{card_html}</section>

  <section class="panel"><h2>Feature lineage &amp; coverage
    <span class="muted">— what each S2 feature is derived from, who consumes it, and how much of the universe is produced</span></h2>
    <table class="queue"><thead><tr><th>Feature</th><th>Coverage</th><th>Tickers</th><th>Dates</th><th>Date span</th></tr></thead>
    <tbody>{grp}</tbody></table>
  </section>

  <section class="panel"><h2>Downstream contract
    <span class="muted">— every input S3/S4 declares, and whether S2 (or upstream) currently produces it</span></h2>
    <table class="queue"><thead><tr><th>Required input</th><th>Status</th><th>Coverage</th></tr></thead>
    <tbody>{con}</tbody></table>
  </section>

  <section class="panel"><h2>Gaps <span class="muted">— S1 data collected but not yet turned into a downstream-used S2 feature</span></h2>
    {gp}
  </section>
</main>"""


_SS_CSS = r"""
.picker{display:flex;gap:10px;align-items:center;margin:12px 0;flex-wrap:wrap}
.picker select{font-size:15px;padding:6px 10px;border-radius:8px;border:1px solid var(--line);
  background:var(--card);color:var(--fg);font-family:ui-monospace,monospace}
.legend2{display:flex;gap:10px;align-items:center;font-size:12px;color:var(--mut)}
.legend2 i{width:9px;height:9px;border-radius:50%;display:inline-block;margin-right:3px}
.sgwrap{position:relative;margin-top:14px;overflow-x:auto}
svg#edges{position:absolute;top:0;left:0;z-index:0;pointer-events:none;overflow:visible}
.cols{position:relative;z-index:1;display:flex;gap:72px;align-items:flex-start;min-width:760px}
.col{flex:1;min-width:150px}
.colhead{font-family:ui-monospace,monospace;font-size:11px;color:var(--mut);text-transform:uppercase;
  letter-spacing:.5px;margin-bottom:10px;border-bottom:1px solid var(--line);padding-bottom:6px}
.node{position:relative;display:flex;align-items:center;gap:7px;padding:6px 9px;margin-bottom:7px;
  border:1px solid var(--line);border-radius:8px;background:var(--card);cursor:pointer;
  font-size:12px;font-family:ui-monospace,monospace;transition:border-color .12s,box-shadow .12s}
.node:hover{border-color:var(--accent)}
.node.off{opacity:.4}
.node.sel{border-color:var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 30%,transparent)}
.node.up{border-color:#3b82c4}.node.down{border-color:var(--fresh)}
.node .nlbl{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.ndot{width:8px;height:8px;border-radius:50%;background:var(--mut);flex:none}
.k-line .ndot,i.k-line{background:var(--accent)}.k-raw .ndot,i.k-raw{background:#c9930b}
.k-json .ndot,i.k-json{background:#3b82c4}
.nmeta{margin-left:auto;color:var(--mut);font-size:10px}
.edge{fill:none;stroke:var(--line);stroke-width:1;opacity:.14}
.edge.hot{stroke:var(--accent);stroke-width:2;opacity:1}
.dhead{display:flex;gap:10px;align-items:center;margin-bottom:12px;font-size:14px}
.chart{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:12px;cursor:crosshair}
.chart .ln{fill:none;stroke:var(--accent);stroke-width:2}
.chart .area{fill:color-mix(in srgb,var(--accent) 10%,transparent);stroke:none}
.chart .ciband{fill:color-mix(in srgb,var(--accent) 16%,transparent);stroke:none}
.chart .pt{fill:var(--accent)}
.chart .grid{stroke:var(--line);stroke-width:1;opacity:.5}
.chart .ax{fill:var(--mut);font-size:11px;font-family:ui-monospace,monospace}
.chart .cx{stroke:var(--accent);stroke-width:1;opacity:.6;stroke-dasharray:3 3}
.chart .cxpt{fill:var(--accent);stroke:var(--card);stroke-width:1.5}
.chart .cxbox{fill:var(--fg);opacity:.92}
.chart .cxt{fill:var(--card);font-size:11px;font-family:ui-monospace,monospace}
.chart .cxv{fill:var(--card);font-size:12px;font-weight:700;font-family:ui-monospace,monospace}
.chart .cxci{stroke:var(--accent);stroke-width:1.5;opacity:.5}
.chart .trainzone{fill:var(--mut);opacity:.10}
.chart .trainbound{stroke:var(--stale);stroke-width:1.5;stroke-dasharray:4 3;opacity:.8}
.chart .tzl{fill:var(--stale);font-size:10px;opacity:.85}.chart .tzl2{fill:var(--fresh);font-size:10px;opacity:.85}
.tznote{color:var(--stale)}
.cmeta{margin-top:8px;font-size:12px;color:var(--mut)}.cmeta b{color:var(--fg)}
.json{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;
  overflow:auto;font-size:12px;line-height:1.5;max-height:460px}
.ts td.tscol{color:var(--accent)}
.predbox{margin:12px 0;padding:12px 14px;border:1px solid var(--line);border-radius:10px;display:flex;
  flex-wrap:wrap;gap:10px;align-items:center;font-size:13px}
.predbox input{padding:5px 8px;border-radius:7px;border:1px solid var(--line);background:var(--card);
  color:var(--fg);font-family:ui-monospace,monospace}
.predbox button{padding:6px 14px;border-radius:7px;border:1px solid var(--accent);background:var(--accent);
  color:#fff;cursor:pointer;font-weight:600}
.predbox #predout{flex-basis:100%}
.ptbl{border-collapse:collapse;margin-top:8px;font-size:13px}.ptbl th,.ptbl td{text-align:left;padding:5px 16px 5px 0}
.ptbl th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase}
.prej{margin-top:8px;color:var(--stale);font-size:13px}
"""

_SS_JS = r"""
function el(id){return document.querySelector('.node[data-id="'+id+'"]');}
function layout(){
  var cols=document.getElementById('cols'), svg=document.getElementById('edges');
  var br=cols.getBoundingClientRect();
  svg.setAttribute('width',cols.scrollWidth); svg.setAttribute('height',cols.scrollHeight);
  var p='';
  for(var i=0;i<EDGES.length;i++){
    var u=EDGES[i][0], v=EDGES[i][1], a=el(u), b=el(v); if(!a||!b) continue;
    var ra=a.getBoundingClientRect(), rb=b.getBoundingClientRect();
    var x1=ra.right-br.left, y1=ra.top-br.top+ra.height/2;
    var x2=rb.left-br.left, y2=rb.top-br.top+rb.height/2, mx=(x1+x2)/2;
    p+='<path d="M'+x1+' '+y1+' C '+mx+' '+y1+' '+mx+' '+y2+' '+x2+' '+y2+'" class="edge" data-u="'+u+'" data-v="'+v+'"></path>';
  }
  svg.innerHTML=p;
}
function pick(node){
  var id=node.getAttribute('data-id');
  var ns=document.querySelectorAll('.node'); for(var i=0;i<ns.length;i++) ns[i].classList.remove('sel','up','down');
  var es=document.querySelectorAll('.edge'); for(var j=0;j<es.length;j++) es[j].classList.remove('hot');
  node.classList.add('sel');
  for(var k=0;k<EDGES.length;k++){var u=EDGES[k][0],v=EDGES[k][1];
    if(u===id||v===id){
      var pth=document.querySelector('.edge[data-u="'+u+'"][data-v="'+v+'"]'); if(pth)pth.classList.add('hot');
      var o=el(u===id?v:u); if(o)o.classList.add(u===id?'down':'up');
    }}
  loadDetail(id);
}
function loadDetail(id){
  var d=document.getElementById('detail'); d.innerHTML='<div class="muted">loading '+id+'…</div>';
  fetch('/api/stock-signal?ticker='+encodeURIComponent(TK)+'&feature='+encodeURIComponent(id))
    .then(function(r){return r.json();}).then(function(s){d.innerHTML=view(s);});
}
function esc(x){return String(x).replace(/&/g,'&amp;').replace(/</g,'&lt;');}
function view(s){
  var t='<div class="dhead"><span class="mono">'+esc(s.feature)+'</span> <span class="chip">'+s.kind+'</span></div>';
  if(s.kind==='line') return t+lineChart(s.points, s.ciseries, s.train_end);
  if(s.kind==='raw') return t+rawTable(s);
  return t+'<pre class="json">'+esc(JSON.stringify(s.value,null,2))+'</pre>';
}
function fmtv(v){var a=Math.abs(v); return (a!==0&&a<0.01)?v.toPrecision(3):(a>=1000?v.toFixed(1):v.toPrecision(4));}
function lineChart(pts, ciArr, trainEnd){
  if(!pts||!pts.length) return '<div class="muted">no numeric series</div>';
  var W=940,H=320,pl=64,pr=66,pt=22,pb=42,n=pts.length;
  var mn=Infinity,mx=-Infinity;
  for(var i0=0;i0<n;i0++){var v=pts[i0][1],c=(ciArr&&ciArr[i0])||0; if(v-c<mn)mn=v-c; if(v+c>mx)mx=v+c;}
  if(mn===mx){mn-=1;mx+=1;} var pr2=(mx-mn)*0.06; mn-=pr2; mx+=pr2;
  function X(i){return pl+(W-pl-pr)*(n<2?0.5:i/(n-1));}
  function Y(v){return (H-pb)-((H-pb-pt))*((v-mn)/(mx-mn));}
  var ti=-1; if(trainEnd){for(var q=0;q<n;q++){if(String(pts[q][0])<=trainEnd)ti=q;}}
  window.CHART={n:n,mn:mn,mx:mx,pl:pl,pr:pr,pt:pt,pb:pb,W:W,H:H,pts:pts,ci:ciArr||null,trainEnd:trainEnd};
  var g='';
  for(var k=0;k<=4;k++){var yv=mn+(mx-mn)*k/4,y=Y(yv).toFixed(1);
    g+='<line class="grid" x1="'+pl+'" y1="'+y+'" x2="'+(W-pr)+'" y2="'+y+'"></line>';
    g+='<text class="ax" x="'+(pl-8)+'" y="'+(+y+4)+'" text-anchor="end">'+fmtv(yv)+'</text>';}
  var K=Math.min(6,n);
  for(var j=0;j<K;j++){var xi=Math.round(j*(n-1)/(K-1||1)),x=X(xi).toFixed(1);
    g+='<line class="grid" x1="'+x+'" y1="'+pt+'" x2="'+x+'" y2="'+(H-pb)+'" opacity="0.5"></line>';
    g+='<text class="ax" x="'+x+'" y="'+(H-pb+18)+'" text-anchor="middle">'+esc(String(pts[xi][0]).slice(5))+'</text>';}
  var tz='';
  if(ti>=0&&ti<n-1){var bx=((X(ti)+X(ti+1))/2).toFixed(1);
    tz='<rect class="trainzone" x="'+pl+'" y="'+pt+'" width="'+(bx-pl).toFixed(1)+'" height="'+(H-pb-pt)+'"></rect>'+
       '<line class="trainbound" x1="'+bx+'" y1="'+pt+'" x2="'+bx+'" y2="'+(H-pb)+'"></line>'+
       '<text class="ax tzl" x="'+(+bx-6)+'" y="'+(pt+11)+'" text-anchor="end">training · don\'t trust</text>'+
       '<text class="ax tzl2" x="'+(+bx+6)+'" y="'+(pt+11)+'" text-anchor="start">out-of-sample →</text>';}
  var band='';
  if(ciArr){var up='',lo='';
    for(var b=0;b<n;b++){var cu=ciArr[b]||0; up+=(b?'L':'M')+X(b).toFixed(1)+' '+Y(pts[b][1]+cu).toFixed(1)+' ';}
    for(var b2=n-1;b2>=0;b2--){var cl=ciArr[b2]||0; lo+='L'+X(b2).toFixed(1)+' '+Y(pts[b2][1]-cl).toFixed(1)+' ';}
    band='<path class="ciband" d="'+up+lo+'Z"></path>';}
  var d='',a='M'+X(0).toFixed(1)+' '+(H-pb);
  for(var i=0;i<n;i++){var xx=X(i).toFixed(1),yy=Y(pts[i][1]).toFixed(1); d+=(i?'L':'M')+xx+' '+yy+' '; a+=' L'+xx+' '+yy;}
  a+=' L'+X(n-1).toFixed(1)+' '+(H-pb)+' Z';
  var last=pts[n-1];
  return '<svg viewBox="0 0 '+W+' '+H+'" class="chart" onmousemove="chartHover(event)" onmouseleave="clearCross()">'+
    tz+g+band+'<path d="'+a+'" class="area"></path><path d="'+d+'" class="ln"></path>'+
    '<circle cx="'+X(n-1).toFixed(1)+'" cy="'+Y(last[1]).toFixed(1)+'" r="3.5" class="pt"></circle>'+
    '<g id="cross"></g></svg>'+
    '<div class="cmeta">'+pts[0][0]+' → '+last[0]+' · '+n+' pts · latest <b>'+fmtv(last[1])+'</b>'+
    (trainEnd?' · <span class="tznote">grey = training window (≤'+trainEnd+', not trusted); right of the line is out-of-sample</span>':'')+
    ' <span class="muted">(hover to read any point)</span></div>';
}
function chartHover(e){
  var C=window.CHART; if(!C) return;
  var svg=e.currentTarget, r=svg.getBoundingClientRect();
  function X(i){return C.pl+(C.W-C.pl-C.pr)*(C.n<2?0.5:i/(C.n-1));}
  function Y(v){return (C.H-C.pb)-((C.H-C.pb-C.pt))*((v-C.mn)/(C.mx-C.mn));}
  var fx=(e.clientX-r.left)/r.width*C.W, df=(fx-C.pl)/(C.W-C.pl-C.pr); df=Math.max(0,Math.min(1,df));
  var i=Math.round(df*(C.n-1)), p=C.pts[i], cx=X(i), cy=Y(p[1]);
  var ci=(C.ci&&C.ci[i]!=null)?C.ci[i]:null;
  var trusted=(!C.trainEnd)||(String(p[0])>C.trainEnd);
  var lx=Math.max(C.pl+2,Math.min(cx,C.W-C.pr-178));
  var l1=String(p[0])+(C.trainEnd?(trusted?'  · OOS (trust)':'  · training'):'');
  var l2=fmtv(p[1])+(ci!=null?'   95% CI ±'+fmtv(ci):'');
  document.getElementById('cross').innerHTML=
    '<line class="cx" x1="'+cx.toFixed(1)+'" y1="'+C.pt+'" x2="'+cx.toFixed(1)+'" y2="'+(C.H-C.pb)+'"></line>'+
    (ci!=null?'<line class="cxci" x1="'+cx.toFixed(1)+'" y1="'+Y(p[1]-ci).toFixed(1)+'" x2="'+cx.toFixed(1)+'" y2="'+Y(p[1]+ci).toFixed(1)+'"></line>':'')+
    '<circle class="cxpt" cx="'+cx.toFixed(1)+'" cy="'+cy.toFixed(1)+'" r="4"></circle>'+
    '<rect class="cxbox" x="'+lx.toFixed(1)+'" y="'+(C.pt+2)+'" width="182" height="34" rx="5"></rect>'+
    '<text class="cxt" x="'+(lx+8).toFixed(1)+'" y="'+(C.pt+16)+'">'+esc(l1)+'</text>'+
    '<text class="cxv" x="'+(lx+8).toFixed(1)+'" y="'+(C.pt+30)+'">'+esc(l2)+'</text>';
}
function clearCross(){var c=document.getElementById('cross'); if(c)c.innerHTML='';}
function rawTable(s){
  var h='<div class="tswrap"><table class="ts"><thead><tr>';
  for(var i=0;i<s.columns.length;i++) h+='<th>'+esc(s.columns[i])+'</th>';
  h+='</tr></thead><tbody>';
  for(var r=0;r<s.rows.length;r++){h+='<tr>';
    for(var c=0;c<s.columns.length;c++){var col=s.columns[c],val=s.rows[r][col];
      h+='<td class="mono'+(col===s.ts_col?' tscol':'')+'">'+(val==null?'—':esc(val))+'</td>';}
    h+='</tr>';}
  return h+'</tbody></table></div>';
}
function runPredict(){
  var out=document.getElementById('predout');
  out.innerHTML='<div class="muted">predicting '+TK+'…</div>';
  fetch('/api/predict?ticker='+encodeURIComponent(TK))
    .then(function(r){return r.json();}).then(function(s){
      if(!s.predictions){out.innerHTML='<div class="prej">no prediction available</div>';return;}
      function pc(p,r,side){if(p==null)return '<td class="mono muted">—</td>';
        var col=side==='down'?'var(--stale)':'var(--fresh)', hot=(r&&r<=5)?' style="font-weight:700;color:var(--accent)"':'';
        return '<td class="mono"><b style="color:'+col+'">'+(p*100).toFixed(0)+'%</b> <span'+hot+'>#'+r+'</span></td>';}
      var h='<table class="ptbl"><thead><tr><th>horizon</th><th>P(up&gt;3%)</th><th>P(down&gt;3%)</th></tr></thead><tbody>';
      s.predictions.forEach(function(p){
        h+='<tr><td>'+p.h+'</td>'+pc(p.p_up,p.rank_up,'up')+pc(p.p_down,p.rank_down,'down')+'</tr>';});
      h+='</tbody></table><div class="cmeta">→ <b>'+esc(s.suggestion||'')+'</b> · action '+esc(s.action||'')+' · calibrated big-move classifier (lower rank # = higher conviction)</div>';
      out.innerHTML=h;
    });
}
window.addEventListener('load',function(){layout(); var d=el('price.close')||document.querySelector('.node'); if(d)pick(d);});
window.addEventListener('resize',layout);
"""


def render_single_stock(graph: dict, tickers: list) -> str:
    tk = graph["ticker"]
    opts = "".join(f'<option{" selected" if t == tk else ""}>{t}</option>' for t in tickers)
    STAGE_NAMES = {"S1": "S1 · Raw collected", "S2": "S2 · Signals",
                   "S3": "S3 · Predictors", "S4": "S4 · Alpha"}
    cols_html = ""
    for stage in graph["stages"]:
        ns = [n for n in graph["nodes"] if n["stage"] == stage]
        chips = ""
        for n in ns:
            off = "" if n["produced"] else " off"
            meta = str(n["n"]) if n["n"] else ""
            chips += (f'<div class="node k-{n["kind"]}{off}" data-id="{n["id"]}" onclick="pick(this)">'
                      f'<span class="ndot"></span><span class="nlbl">{n["label"]}</span>'
                      f'<span class="nmeta">{meta}</span></div>')
        cols_html += (f'<div class="col"><div class="colhead">{STAGE_NAMES[stage]} '
                      f'<span class="muted">({len(ns)})</span></div>{chips}</div>')
    data = (f'<script>var TK={_json.dumps(tk)};'
            f'var EDGES={_json.dumps(graph["edges"])};</script>')
    body = (
        f'<main><header><div class="brand"><span class="live"></span>Single Stock · {tk}</div>'
        f'<div class="gen mono">dataflow · click a node to inspect</div></header>'
        f'<form class="picker" method="get" action="/single-stock"><label class="muted">Ticker</label>'
        f'<select name="ticker" onchange="this.form.submit()">{opts}</select>'
        f'<span class="legend2"><i class="k-line"></i>line <i class="k-raw"></i>raw '
        f'<i class="k-json"></i>json <span class="muted">· dim = not produced</span></span></form>'
        f'<div class="predbox"><b>Predict trigger</b> <span class="muted">deployed big-move classifier (calibrated) ·</span> '
        f'<button type="button" onclick="runPredict()">Predict {tk}</button>'
        f'<span class="muted">calibrated P(up)/P(down) per horizon + rank vs universe</span>'
        f'<div id="predout"></div></div>'
        f'<div class="sgwrap"><svg id="edges"></svg><div class="cols" id="cols">{cols_html}</div></div>'
        f'<div id="detail" class="panel"><div class="muted">Click a node to inspect its signal '
        f'(line chart, raw rows, or structured value).</div></div></main>')
    return _CSS + "<style>" + _SS_CSS + "</style>" + body + data + "<script>" + _SS_JS + "</script>"


def _flow_nav(current):
    steps = [("S1", "/data-collection"), ("S2", "/signal-processing"),
             ("S3 predictors", "/predictors"), ("S4 alpha", "/alpha")]
    out = ['<a class="fstep home" href="/">☰ dashboards</a>']
    for i, (name, href) in enumerate(steps):
        cls = "fstep cur" if name.startswith(current) else "fstep"
        out.append(f'<a class="{cls}" href="{href}">{name}</a>' if href
                   else f'<span class="{cls} off">{name}</span>')
        if i < len(steps) - 1:
            out.append("→")
    return '<div class="flow">' + "".join(out) + "</div>"


def render_predictors(rep: dict) -> str:
    def pct(x):
        return "—" if x is None else f"{x * 100:.0f}%"

    built = [c for c in rep["categories"] if c["built"]]
    cards = [("Prediction categories", str(len(rep["categories"])), "one calibrated prediction / ticker each"),
             ("Metric", "precision@k", "per-day, walk-forward vs base rate"),
             ("Built", str(len(built)), "have recorded model rosters"),
             ("Consumed by", "S4 alpha", "picks the strongest per stock")]
    ch = "".join(f'<div class="card"><div class="eyebrow">{t}</div><div class="metric">{v}</div>'
                 f'<div class="sub">{s}</div></div>' for t, v, s in cards)

    cats_html = ""
    for c in rep["categories"]:
        if not c["built"]:
            cats_html += (f'<details class="catbox"><summary><span class="cattag">{c["label"]}</span> '
                          f'{c["title"]} <span class="chip">not built yet</span></summary>'
                          f'<div class="note">No recorded model roster for this horizon.</div></details>')
            continue
        base = f'base ↑{pct(c["base_up"])} ↓{pct(c["base_down"])}'
        rows = ""
        for m in c["models"]:
            prod, champ = m["production"], m["champion"]
            cls = "prodrow" if prod else ""
            badge = ('<span class="chip prod">PRODUCTION</span>' if prod
                     else ('<span class="chip champ">champion ⭐</span>' if champ
                           else '<span class="chip">tried</span>'))
            rows += (f'<tr class="{cls}" onclick="modelDetail(this)" data-model="{m["model"]}" data-cat="{c["key"]}">'
                     f'<td class="mono feat">{m["model"]}</td>'
                     f'<td class="mono">{m["up1"]}</td><td class="mono">{m["up5"]}</td>'
                     f'<td class="mono"><b>{m["down1"]}</b></td><td class="mono">{m["down5"]}</td>'
                     f'<td class="mono muted">{m["ece"]}</td><td>{badge}</td></tr>')
        cats_html += (
            f'<details class="catbox" open><summary><span class="cattag">{c["label"]}</span> {c["title"]} '
            f'<span class="muted">· {base} · {c.get("n_rows",0):,} rows · <b>prod: {c["production"]}</b></span></summary>'
            f'<table class="queue"><thead><tr><th>Model</th><th>up@1</th><th>up@5</th>'
            f'<th>down@1</th><th>down@5</th><th>ECE</th><th>Status</th></tr></thead><tbody>{rows}</tbody></table>'
            f'<div id="md-{c["key"]}" class="mdslot"></div></details>')

    return f"""{_CSS}
<style>
.flow{{display:flex;gap:8px;align-items:center;font-size:13px;margin:12px 0;flex-wrap:wrap}}
.fstep{{padding:4px 11px;border:1px solid var(--line);border-radius:16px;text-decoration:none;color:var(--mut)}}
.fstep:hover{{border-color:var(--accent);color:var(--fg)}}
.fstep.cur{{color:var(--accent);border-color:var(--accent);font-weight:640}}
.fstep.off{{opacity:.5}}.fstep.home{{color:var(--fg)}}
.note{{font-size:12.5px;color:var(--mut);line-height:1.6;margin-top:8px}}
.catbox{{border:1px solid var(--line);border-radius:12px;padding:6px 16px 14px;margin-bottom:12px}}
.catbox summary{{cursor:pointer;font-size:14px;padding:8px 0;font-weight:600}}
.cattag{{display:inline-block;background:var(--accent);color:#fff;font-family:ui-monospace,monospace;
  font-size:12px;font-weight:700;padding:2px 9px;border-radius:6px;margin-right:8px}}
tr.prodrow{{background:color-mix(in srgb,var(--fresh) 10%,transparent)}}
tr[data-model]{{cursor:pointer}}tr[data-model]:hover{{background:color-mix(in srgb,var(--accent) 7%,transparent)}}
.chip.prod{{background:color-mix(in srgb,var(--fresh) 22%,transparent);color:var(--fresh);font-weight:700}}
.chip.champ{{background:color-mix(in srgb,var(--accent) 16%,transparent);color:var(--accent)}}
.mdslot{{margin-top:10px}}.mdcurve{{display:flex;gap:16px;align-items:flex-end;height:110px;margin:8px 0}}
.mdbar{{display:flex;flex-direction:column;align-items:center;gap:4px;font-size:11px;font-family:ui-monospace,monospace;color:var(--mut)}}
.mdbar .bar2{{width:24px;background:var(--fresh);border-radius:3px 3px 0 0}}
.mdbar .base{{width:24px;background:var(--miss);border-radius:3px 3px 0 0}}
.json{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;font-size:11.5px;max-height:320px}}
</style>
<main>
  <header><div class="brand"><span class="live"></span>Predictors</div>
    <div class="gen mono">recorded rosters · {rep["generated_at"][:19].replace("T", " ")} UTC</div></header>
  {_flow_nav("S3")}
  <section class="cards">{ch}</section>
  <section class="panel"><h2>Prediction categories <span class="muted">— {rep["metric"]}. One category = one model-creation target; each serves a calibrated prediction per ticker. Expand a category, click a model for its precision@k curve + error cases.</span></h2>
    {cats_html}
    <div class="note"><b>{rep["headline"]}</b><br>
    Numbers are the recorded per-horizon rosters ({rep["source"]}). up@k/down@k = precision of the
    day's top-k conviction picks (× = lift over base rate); <b>ECE</b> = calibration error (lower =
    probabilities more trustworthy). <span class="lift">PRODUCTION</span> = deployed model for that
    category; ⭐ = recorded champion. Planned: {rep["planned"]}.</div>
  </section>
</main>
<script>
function modelDetail(row){{
  var model=row.getAttribute('data-model'), cat=row.getAttribute('data-cat');
  var out=document.getElementById('md-'+cat);
  out.innerHTML='<div class="muted">loading '+model+'…</div>';
  fetch('/api/model-detail?model='+encodeURIComponent(model)+'&category='+encodeURIComponent(cat))
    .then(function(r){{return r.json();}}).then(function(s){{
    var h='<div class="panel" style="margin:0"><h2>'+model+' · '+s.category+' <span class="muted">— precision@k curve (prediction vs accuracy) · ECE '+(s.ece||'—')+'</span></h2>';
    if(s.curve&&s.curve.length){{
      var bd=s.base_rate_down||0, bu=s.base_rate_up||0, mx=Math.max(bd,bu,0.01);
      s.curve.forEach(function(c){{mx=Math.max(mx,c.up||0,c.dn||0);}});
      function bars(side,base,lbl){{var b='<div class="muted mono">'+lbl+'</div><div class="mdcurve">';
        s.curve.forEach(function(c){{var v=c[side]||0; b+='<div class="mdbar"><div>'+(v*100).toFixed(0)+'%</div><div class="bar2" style="height:'+(v/mx*88)+'px"></div><div>@'+c.k+'</div></div>';}});
        b+='<div class="mdbar"><div>'+(base*100).toFixed(0)+'%</div><div class="base" style="height:'+(base/mx*88)+'px"></div><div>base</div></div></div>';return b;}}
      h+=bars('dn',bd,'DOWN precision@k (vs base rate)')+bars('up',bu,'UP precision@k');
    }}
    if(s.top_errors&&s.top_errors.length){{
      h+='<h2 style="margin-top:12px">Recorded confident-wrong cases <span class="muted">('+s.top_errors.length+')</span></h2><pre class="json">'+JSON.stringify(s.top_errors,null,1)+'</pre>';
    }}
    out.innerHTML=h+'</div>';
  }});
}}
</script>"""


def render_health(h: dict, alerts: list) -> str:
    V = {"OK": ("fresh", "✓ HEALTHY"), "DEGRADED": ("amber", "▲ DEGRADED"),
         "STALLED": ("stale", "✕ STALLED")}
    cls, label = V.get(h["verdict"], ("mut", h["verdict"]))
    hb = h["heartbeat_age_s"]
    krows = ""
    for k in h["kinds"]:
        st = ('<span class="chip crit">STALE</span>' if k["stale"] and k["critical"]
              else ('<span class="chip warn">stale</span>' if k["stale"] else '<span class="chip ok2">fresh</span>'))
        err = f'<span class="chip crit">{k["errors"]} err</span>' if k["errors"] else ""
        star = "★" if k["critical"] else ""
        krows += (f'<tr><td class="mono">{star} {k["kind"]}</td><td class="mono muted">{k["source"]}</td>'
                  f'<td class="mono">{(k["last_ok"] or "NEVER")[:19]}</td>'
                  f'<td class="mono num">{k["age_h"] if k["age_h"] is not None else "—"}h</td>'
                  f'<td class="mono muted">{k["interval_h"]}h</td><td>{st}{err}</td></tr>')
    al = "".join(f'<li class="mono">{a}</li>' for a in h["alerts"]) or '<li class="muted">none</li>'
    log = "".join(f'<tr><td class="mono muted">{a["ts"][:19]}</td>'
                  f'<td><span class="chip {"crit" if a["level"]=="critical" else ("warn" if a["level"]=="warn" else "ok2")}">{a["verdict"]}</span></td>'
                  f'<td class="mono">{a["message"]}</td></tr>' for a in alerts) or \
          '<tr><td colspan="3" class="muted">no state changes recorded</td></tr>'
    return f"""{_CSS}
<style>
.flow{{display:flex;gap:8px;align-items:center;font-size:13px;margin:12px 0;flex-wrap:wrap}}
.fstep{{padding:4px 11px;border:1px solid var(--line);border-radius:16px;text-decoration:none;color:var(--mut)}}
.fstep.home{{color:var(--fg)}}
.hbanner{{padding:18px 22px;border-radius:14px;margin:12px 0;display:flex;align-items:center;gap:20px;border:1px solid var(--line)}}
.hv{{font-size:26px;font-weight:760}}.hv.fresh{{color:var(--fresh)}}.hv.amber{{color:#c9930b}}.hv.stale{{color:var(--stale)}}
.hsub{{color:var(--mut);font-size:13px}}
.chip.ok2{{background:color-mix(in srgb,var(--fresh) 16%,transparent);color:var(--fresh)}}
.chip.warn{{background:color-mix(in srgb,#c9930b 18%,transparent);color:#c9930b}}
ul.al{{font-size:13px;line-height:1.7;color:var(--stale)}}ul.al li.muted{{color:var(--fresh)}}
</style>
<main>
  <header><div class="brand"><span class="live"></span>Collector Health</div>
    <div class="gen mono">auto-refresh 30s · {h["generated_at"][:19].replace("T"," ")}Z</div></header>
  <meta http-equiv="refresh" content="30">
  <div class="flow"><a class="fstep home" href="/">☰ dashboards</a><a class="fstep" href="/data-collection">S1 collection →</a></div>
  <div class="hbanner"><div><div class="hv {cls}">{label}</div>
    <div class="hsub">heartbeat: last API call {("%d s" % hb) if hb is not None else "—"} ago
    {"(ALIVE)" if h["alive"] else "(NO HEARTBEAT)"} · latest close {h["latest_close"]} · bars {h["bars_latest"]}</div></div>
    <div style="margin-left:auto"><ul class="al">{al}</ul></div></div>
  <section class="panel"><h2>Per-signal freshness <span class="muted">★ = critical (must stay fresh)</span></h2>
    <table class="queue"><thead><tr><th>Kind</th><th>Src</th><th>Last OK (UTC)</th><th>Age</th><th>Cadence</th><th>State</th></tr></thead>
    <tbody>{krows}</tbody></table></section>
  <section class="panel"><h2>Alert history <span class="muted">— recorded state changes (STALL → recover)</span></h2>
    <table class="queue"><thead><tr><th>When (UTC)</th><th>Verdict</th><th>Message</th></tr></thead><tbody>{log}</tbody></table></section>
</main>"""


def _page_health(h, alerts):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Collector Health</title></head><body>' + render_health(h, alerts) + '</body></html>')


def render_picks(s: dict) -> str:
    reg = s.get("regime") or {}

    def side_tbl(rows, side):
        pk = "up_p" if side == "long" else "down_p"
        hk = "up_h" if side == "long" else "down_h"
        col = "var(--fresh)" if side == "long" else "var(--stale)"
        body = ""
        for x in rows:
            de = x["days_to_earn"]
            ev = f'<span class="evchip">⚠ {de}d</span>' if (de is not None and de <= 3) else (f'{de}d' if de is not None else "—")
            body += (f'<tr onclick="location.href=\'/alpha?ticker={x["ticker"]}\'">'
                     f'<td class="mono">{x["rank"]}</td><td class="mono feat"><b>{x["ticker"]}</b></td>'
                     f'<td class="mono">${x["price"]:.2f}</td>'
                     f'<td class="mono"><b style="color:{col}">{x[pk]*100:.0f}%</b> <span class="muted">@{x[hk]}</span></td>'
                     f'<td class="mono">{x["net"]:+.2f}</td><td class="mono muted">{ev}</td></tr>')
        head = "P(up)" if side == "long" else "P(down)"
        return (f'<table class="queue"><thead><tr><th>#</th><th>Ticker</th><th>Price</th>'
                f'<th>{head} (horizon)</th><th>net</th><th>earnings</th></tr></thead><tbody>{body}</tbody></table>')

    return f"""{_CSS}
<style>
.flow{{display:flex;gap:8px;align-items:center;font-size:13px;margin:12px 0;flex-wrap:wrap}}
.fstep{{padding:4px 11px;border:1px solid var(--line);border-radius:16px;text-decoration:none;color:var(--mut)}}
.fstep:hover{{border-color:var(--accent);color:var(--fg)}}.fstep.cur{{color:var(--accent);border-color:var(--accent);font-weight:640}}
.fstep.home{{color:var(--fg)}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}@media(max-width:900px){{.grid2{{grid-template-columns:1fr}}}}
tr[onclick]{{cursor:pointer}}tr[onclick]:hover{{background:color-mix(in srgb,var(--accent) 7%,transparent)}}
.evchip{{color:var(--stale);font-weight:700;font-size:11px}}
.note{{font-size:12.5px;color:var(--mut);line-height:1.6;margin-top:8px}}
h2 .side{{font-weight:740}}h2 .lg{{color:var(--fresh)}}h2 .sh{{color:var(--stale)}}
</style>
<main>
  <header><div class="brand"><span class="live"></span>Alpha Screen · Top &amp; Bottom Picks</div>
    <div class="gen mono">as-of {s["as_of"]} · {s["universe_n"]} stocks · {s["generated_at"][:16].replace("T"," ")}Z</div></header>
  {_flow_nav("S4")}
  <div class="grid2">
    <section class="panel"><h2><span class="side lg">▲ TOP PICKS</span> <span class="muted">— longs, highest up-conviction</span></h2>
      {side_tbl(s["longs"], "long")}</section>
    <section class="panel"><h2><span class="side sh">▼ BOTTOM PICKS</span> <span class="muted">— shorts, highest down-conviction</span></h2>
      {side_tbl(s["shorts"], "short")}</section>
  </div>
  <div class="note">{s["note"]} Regime: {reg.get("decision","—")} (score {reg.get("score","—")}). Click a row for the full <a href="/alpha">alpha report</a>. ⚠ = earnings ≤3 days (event risk).</div>
</main>"""


def _page_picks(s):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Alpha Screen — Top &amp; Bottom Picks</title></head><body>'
            + render_picks(s) + '</body></html>')


def render_alpha(rep: dict, tickers: list) -> str:
    tk = rep["ticker"]
    opts = "".join(f'<option{" selected" if t == tk else ""}>{t}</option>' for t in tickers)
    ACT_CLS = {"LONG CANDIDATE": "fresh", "AVOID": "stale", "AVOID / SHORT-LEAN": "stale",
               "CRASH-WATCH": "amber", "STAND DOWN": "amber", "NEUTRAL": "mut"}

    def pct(x, d=1):
        return "—" if x is None else f"{x * 100:.{d}f}%"

    def sig(x, d=3):
        return "—" if x is None else f"{x:.{d}f}"

    def bar(p):
        p = p or 0
        col = "var(--fresh)" if p >= 0.66 else ("#c9930b" if p >= 0.33 else "var(--stale)")
        return (f'<div class="fbar"><span style="width:{p*100:.0f}%;background:{col}"></span></div>'
                f'<span class="fp">{p*100:.0f}</span>')

    fac = "".join(f'<tr><td class="mono">{f["name"]}</td><td class="barcell">{bar(f["pct"])}</td>'
                  f'<td class="mono muted">{f["note"]}</td></tr>' for f in rep["factors"])

    def rkcell(p, r, side):
        if p is None:
            return '<td class="mono muted">—</td><td class="mono muted">—</td>'
        hot = "hotrank" if (r and r <= 5) else ""
        col = "var(--stale)" if side == "down" else "var(--fresh)"
        return (f'<td class="mono"><b style="color:{col}">{p*100:.0f}%</b></td>'
                f'<td class="mono {hot}">#{r}</td>')
    predrows = "".join(
        f'<tr><td class="mono">{p["h"]}</td>{rkcell(p["p_up"], p["rank_up"], "up")}'
        f'{rkcell(p["p_down"], p["rank_down"], "down")}</tr>' for p in rep["predictions"])
    cat = rep["catalysts"]; risk = rep["risk"]; pos = rep["positioning"]; val = rep["valuation"]
    vr = rep["valuation_ranks"]; reg = rep["regime"]; eff = rep["efficacy"]

    def money(x):
        if x is None:
            return "—"
        a = abs(x)
        return f'{"−" if x<0 else ""}${a/1e9:.1f}B' if a >= 1e9 else f'{"−" if x<0 else ""}${a/1e6:.1f}M'

    catrows = [("Days to earnings", cat["days_to_earnings"], "event proximity"),
               ("Options implied move", pct(cat["implied_move"]), "expected earnings move"),
               ("Last EPS surprise", pct((cat["last_surprise_pct"] or 0) / 100, 1) if cat["last_surprise_pct"] else "—", "beat/miss"),
               ("Analyst revisions 90d", f'{cat["analyst_rev_net_90d"]:+d}', "net up−down"),
               ("Insider net 90d", money(cat["insider_net_90d_usd"]), "buy(+) / sell(−)")]
    riskrows = [("Realized vol (20d)", pct(risk["hvol20"], 2), ""),
                ("Event risk", (risk["event_risk"] or {}).get("level", "—"), "earnings proximity"),
                ("Short % float", pct(risk["short_pct_float"]), "crowding"),
                ("Days to cover", sig(risk["days_to_cover"], 1), "squeeze fuel"),
                ("Near 52w high", "yes" if risk["near_52w_high"] else "no", "extension"),
                ("Predicted beta", "— (gap)", "collectible from bars")]
    posrows = [("Analyst target", f'${pos["target_price"]:.0f}' if pos["target_price"] else "—",
                f'upside {pct(pos["upside_to_target"])}'),
               ("Recommendation", sig(pos["recommendation_mean"], 2), f'{pos["n_analysts"] or "—"} analysts'),
               ("Insider net 90d", money(pos["insider_net_90d_usd"]), ""),
               ("Short % float", pct(pos["short_pct_float"]), "")]
    valrows = [("Earnings yield", pct(val["earnings_yield"], 2), pct(vr["earnings_yield"]) + " pctile"),
               ("FCF yield", pct(val["fcf_yield"], 2), pct(vr["fcf_yield"]) + " pctile"),
               ("ROE", pct(val["roe"], 1), pct(vr["roe"]) + " pctile"),
               ("Gross profitability", pct(val["gross_profitability"], 1), pct(vr["gross_profitability"]) + " pctile"),
               ("Net margin", pct(val["net_margin"], 1), ""),
               ("Market cap", money(val["market_cap"]), "")]

    def tbl(rows):
        return "".join(f'<tr><td class="mono lbl">{a}</td><td class="mono val"><b>{b}</b></td>'
                       f'<td class="mono muted">{d}</td></tr>' for a, b, d in rows)

    gaps = "".join(f'<li>{g}</li>' for g in rep["gaps"])
    act_cls = ACT_CLS.get(rep["action"], "mut")

    return f"""{_CSS}
<style>
.flow{{display:flex;gap:8px;align-items:center;font-size:13px;margin:12px 0;flex-wrap:wrap}}
.fstep{{padding:4px 11px;border:1px solid var(--line);border-radius:16px;text-decoration:none;color:var(--mut)}}
.fstep:hover{{border-color:var(--accent);color:var(--fg)}}.fstep.cur{{color:var(--accent);border-color:var(--accent);font-weight:640}}
.fstep.home{{color:var(--fg)}}
.picker{{display:flex;gap:10px;align-items:center;margin:12px 0}}
.picker select{{font-size:16px;padding:6px 10px;border-radius:8px;border:1px solid var(--line);background:var(--card);color:var(--fg);font-family:ui-monospace,monospace}}
.actbanner{{display:flex;align-items:center;gap:16px;padding:16px 20px;border:1px solid var(--line);border-radius:14px;margin:12px 0}}
.act{{font-size:22px;font-weight:740;letter-spacing:-.02em}}
.act.fresh{{color:var(--fresh)}}.act.stale{{color:var(--stale)}}.act.amber{{color:#c9930b}}.act.mut{{color:var(--mut)}}
.acsub{{color:var(--mut);font-size:13px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:16px}}@media(max-width:820px){{.grid2{{grid-template-columns:1fr}}}}
.fbar{{display:inline-block;width:calc(100% - 34px);height:8px;border-radius:4px;background:var(--miss);vertical-align:middle;overflow:hidden}}
.fbar span{{display:block;height:100%;border-radius:4px}}.fp{{display:inline-block;width:28px;text-align:right;font-family:ui-monospace,monospace;font-size:12px;color:var(--mut)}}
td.lbl{{color:var(--mut)}}td.val{{min-width:90px}}
.gaps{{font-size:12.5px;color:var(--mut);line-height:1.7}}.gaps li{{margin-left:4px}}
.eff{{font-size:12.5px;color:var(--mut)}}.eff b{{color:var(--fresh)}}
td.hotrank{{color:var(--accent);font-weight:700}}
</style>
<main>
  <header><div class="brand"><span class="live"></span>Alpha Report</div>
    <div class="gen mono">as-of {rep["as_of"]} · {rep["generated_at"][:16].replace("T"," ")}Z</div></header>
  {_flow_nav("S4")}
  <form class="picker" method="get" action="/alpha"><label class="muted">Ticker</label>
    <select name="ticker" onchange="this.form.submit()">{opts}</select>
    <span class="muted mono">${sig(rep["price"],2)} · regime {reg["decision"]} (VIX {sig(reg["vix"],1)}, breadth {pct(reg["breadth"],0)})</span></form>
  <div class="actbanner"><div><div class="act {act_cls}">{rep["action"]}</div>
    <div class="acsub">{rep["why"]}</div></div>
    <div style="margin-left:auto;text-align:right"><div class="metric">{pct(rep["factor_score"],0)}</div>
    <div class="acsub">factor screen score</div></div></div>
  <div class="grid2">
    <section class="panel"><h2>Factor decomposition <span class="muted">— percentile vs universe</span></h2>
      <table class="queue"><tbody>{fac}</tbody></table></section>
    <section class="panel"><h2>Catalysts &amp; events</h2><table class="queue"><tbody>{tbl(catrows)}</tbody></table></section>
    <section class="panel"><h2>Valuation <span class="muted">— vs peers</span></h2><table class="queue"><tbody>{tbl(valrows)}</tbody></table></section>
    <section class="panel"><h2>Risk &amp; exposures</h2><table class="queue"><tbody>{tbl(riskrows)}</tbody></table></section>
    <section class="panel"><h2>Positioning</h2><table class="queue"><tbody>{tbl(posrows)}</tbody></table></section>
    <section class="panel"><h2>Predictions <span class="muted">— deployed big-move classifier: calibrated P(move&gt;±3%) &amp; rank vs universe</span></h2>
      <table class="queue"><thead><tr><th>Horizon</th><th>P(up)</th><th>rank</th><th>P(down)</th><th>rank</th></tr></thead><tbody>{predrows}</tbody></table>
      <div class="eff">→ <b>Strongest: {rep["suggestion"]}</b><br>
      Production model: <b>{(eff or {}).get("production","—")}</b> · recorded down@1 {(eff or {}).get("down@1","—")}.
      <span class="muted">Lower rank # = higher conviction; the top picks per day carry the precision@k edge. Calibrated (isotonic).</span></div></section>
  </div>
  <section class="panel"><h2>Missing inputs <span class="muted">— to make this report complete (see docs/alpha-report)</span></h2>
    <ul class="gaps">{gaps}</ul></section>
</main>"""


def _page_alpha(rep, tickers):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Alpha · {rep["ticker"]}</title></head><body>'
            + render_alpha(rep, tickers) + '</body></html>')


def _page_predictors(rep):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>Predictors</title></head><body>' + render_predictors(rep) + '</body></html>')


def _page_single(graph, tickers):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>Single Stock · {graph["ticker"]}</title></head><body>'
            + render_single_stock(graph, tickers) + '</body></html>')


def _page_s2(rep):
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>S2 Signal Processing</title></head><body>'
            + render_s2(rep) + '</body></html>')


def serve(port=8787):
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from urllib.parse import urlparse, parse_qs
    from collector import default_collector
    from universe import UNIVERSE

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            col = default_collector()
            parsed = urlparse(self.path); path = parsed.path.rstrip("/") or "/"
            if path == "/api/detail":                 # drill-down: one ticker × signal
                q = parse_qs(parsed.query)
                payload = _json.dumps(col.signal_detail(
                    q.get("scope", [""])[0], q.get("feature", [""])[0])).encode()
                ctype = "application/json"
            elif path == "/api/typed":                 # typed table rows (real columns)
                q = parse_qs(parsed.query)
                payload = _json.dumps(col.typed_rows(
                    q.get("table", [""])[0], q.get("ticker", [""])[0] or None)).encode()
                ctype = "application/json"
            elif path == "/data-collection":          # THIS pipeline step's dashboard
                payload = _page(col.coverage_report(), UNIVERSE, auto_refresh=0).encode()
                ctype = "text/html; charset=utf-8"
            elif path == "/signal-processing":        # S2 feature lineage + gaps
                import pipeline_map
                payload = _page_s2(pipeline_map.report()).encode()
                ctype = "text/html; charset=utf-8"
            elif path == "/predictors":               # S3 predictor models (recorded results)
                import pipeline_map
                payload = _page_predictors(pipeline_map.predictor_report()).encode()
                ctype = "text/html; charset=utf-8"
            elif path == "/alpha":                     # S4 per-stock alpha report
                import pipeline_map
                q = parse_qs(parsed.query)
                tk = (q.get("ticker", [""])[0] or UNIVERSE[0]).upper()
                payload = _page_alpha(pipeline_map.alpha_report(tk), list(UNIVERSE)).encode()
                ctype = "text/html; charset=utf-8"
            elif path == "/picks":                     # universe screen: top & bottom picks
                import pipeline_map
                payload = _page_picks(pipeline_map.alpha_screen(n=15)).encode()
                ctype = "text/html; charset=utf-8"
            elif path == "/health":                    # collector heartbeat + freshness + alerts
                import health
                payload = _page_health(health.health(), health.recent_alerts()).encode()
                ctype = "text/html; charset=utf-8"
            elif path == "/api/model-detail":          # a model's precision@k curve + errors
                import pipeline_map
                q = parse_qs(parsed.query)
                payload = _json.dumps(pipeline_map.model_detail(
                    q.get("model", [""])[0], q.get("category", [""])[0]), default=str).encode()
                ctype = "application/json"
            elif path == "/single-stock":             # per-ticker dataflow DAG
                import pipeline_map
                q = parse_qs(parsed.query)
                tk = (q.get("ticker", [""])[0] or UNIVERSE[0]).upper()
                payload = _page_single(pipeline_map.stock_graph(tk), list(UNIVERSE)).encode()
                ctype = "text/html; charset=utf-8"
            elif path == "/api/stock-signal":         # one signal's best view (line/raw/json)
                import pipeline_map
                q = parse_qs(parsed.query)
                payload = _json.dumps(pipeline_map.stock_signal(
                    q.get("ticker", [""])[0], q.get("feature", [""])[0]), default=str).encode()
                ctype = "application/json"
            elif path == "/api/predict":              # prediction TRIGGER — deployed classifier
                import pipeline_map
                q = parse_qs(parsed.query)
                payload = _json.dumps(pipeline_map.ticker_prediction(
                    q.get("ticker", [""])[0]), default=str).encode()
                ctype = "application/json"
            else:                                     # "/" index of all step dashboards
                payload = _index_page().encode()
                ctype = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers(); self.wfile.write(payload)

        def log_message(self, *a):
            pass

    print(f"Dashboards → http://localhost:{port}/  ·  data collection → "
          f"http://localhost:{port}/data-collection  (Ctrl-C to stop)")
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
