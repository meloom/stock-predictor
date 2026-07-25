"""collector_dashboard.py — render the collector's coverage_report() into a
self-contained HTML dashboard: overall backfill %, per-source quota, per-kind
queue progress, and a ticker x signal freshness heatmap.

  python -m collector_dashboard [out.html]
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SIGNAL_LABELS = {
    "price.close": "Bars", "price.current": "Quote", "short.pct_float": "Short%",
    "opt.implied_move": "ImplMove", "fundamental.analyst_snapshot": "Analyst",
    "fundamental.statements": "Fundmls", "calendar.days_to_earnings": "Earn",
}


def _fresh_class(h):
    if h is None:
        return "miss"
    if h <= 24:
        return "fresh"
    if h <= 168:
        return "aging"
    return "stale"


def _bar(pct, cls="ok"):
    return (f'<div class="bar"><span class="fill {cls}" style="width:{pct}%"></span>'
            f'</div><span class="pct">{pct}%</span>')


def render(report: dict, tickers: list[str]) -> str:
    o = report["overall"]
    cols = report["matrix_cols"]
    mat = report["matrix"]
    # quota pills
    quota = "".join(f'<span class="pill"><b>{s}</b> {v}</span>'
                    for s, v in report["quota"].items())
    # metric cards
    cards = [
        ("Backfill", f'{o["pct"]}%', f'{o["collected"]:,}/{o["tasks"]:,} tasks collected'),
        ("Data points", f'{o["data_points"]:,}', "rows in the store"),
        ("Due now", f'{o["due_now"]:,}', "tasks ready to run"),
        ("Signals live", f'{len(report["signals"])}', "features with data"),
    ]
    card_html = "".join(
        f'<div class="card"><div class="eyebrow">{t}</div>'
        f'<div class="metric">{v}</div><div class="sub">{s}</div></div>'
        for t, v, s in cards)

    # queue rows
    qrows = ""
    for k in report["kinds"]:
        cls = "warn" if k["errors"] else ("ok" if k["pct"] == 100 else "run")
        last = f'{k["last_run_h"]}h ago' if k["last_run_h"] is not None else "—"
        err = f'<span class="chip crit">{k["errors"]} err</span>' if k["errors"] else ""
        due = f'<span class="chip">{k["due_now"]} due</span>' if k["due_now"] else ""
        qrows += (f'<tr><td class="mono kind">{k["kind"]}</td>'
                  f'<td class="src">{k["source"]}</td>'
                  f'<td class="barcell">{_bar(k["pct"], cls)}</td>'
                  f'<td class="mono num">{k["collected"]}/{k["total"]}</td>'
                  f'<td>{due}{err}</td><td class="mono muted">{last}</td></tr>')

    # heatmap
    head = "".join(f'<th class="rot"><span>{SIGNAL_LABELS.get(c, c)}</span></th>' for c in cols)
    body = ""
    shown = [t for t in tickers if t in mat] + [t for t in tickers if t not in mat]
    for t in shown:
        cells = ""
        row = mat.get(t, {})
        for c in cols:
            cell = row.get(c)
            h = cell["fresh_h"] if cell else None
            cls = _fresh_class(h)
            title = f'{t} · {c}: {("ingested " + str(h) + "h ago, event " + str(cell["event"])) if cell else "no data"}'
            cells += f'<td class="cell {cls}" title="{title}"></td>'
        body += f'<tr><th class="tick mono">{t}</th>{cells}</tr>'

    # signals table
    srows = ""
    for s in report["signals"]:
        cls = _fresh_class(s["fresh_h"])
        fresh = f'{s["fresh_h"]}h' if s["fresh_h"] is not None else "—"
        srows += (f'<tr><td class="mono">{s["feature"]}</td>'
                  f'<td class="mono num">{s["rows"]:,}</td>'
                  f'<td class="mono num">{s["scopes"]}</td>'
                  f'<td class="mono muted">{s["latest_event"] or "—"}</td>'
                  f'<td><span class="dot {cls}"></span><span class="mono">{fresh}</span></td></tr>')

    return f"""{_CSS}
<main>
  <header>
    <div class="brand"><span class="live"></span>S1 Data Collector</div>
    <div class="gen mono">snapshot · {report["generated_at"][:19].replace("T", " ")} UTC</div>
  </header>

  <section class="cards">{card_html}</section>

  <section class="quota"><span class="lbl">Rate-limit quota</span>{quota}</section>

  <section class="panel">
    <h2>Queue &amp; backfill progress</h2>
    <table class="queue">
      <thead><tr><th>Kind</th><th>Source</th><th>Progress</th><th>Collected</th>
      <th>State</th><th>Last run</th></tr></thead>
      <tbody>{qrows}</tbody>
    </table>
  </section>

  <section class="panel">
    <h2>Coverage &amp; freshness <span class="muted">— {len(shown)} tickers × {len(cols)} signals</span></h2>
    <div class="legend">
      <span><i class="cell fresh"></i>≤24h</span><span><i class="cell aging"></i>≤7d</span>
      <span><i class="cell stale"></i>&gt;7d</span><span><i class="cell miss"></i>missing</span>
    </div>
    <div class="heatwrap">
      <table class="heat"><thead><tr><th class="corner"></th>{head}</tr></thead>
      <tbody>{body}</tbody></table>
    </div>
  </section>

  <section class="panel">
    <h2>Signals in the store</h2>
    <table class="signals">
      <thead><tr><th>Feature</th><th>Rows</th><th>Tickers</th><th>Latest event</th><th>Freshness</th></tr></thead>
      <tbody>{srows}</tbody>
    </table>
  </section>
</main>"""


_CSS = """<style>
:root{
  --bg:#f6f8fa; --panel:#ffffff; --line:#d8dee4; --ink:#1c2128; --mut:#656d76;
  --accent:#0d9488; --accent-soft:#99f6e4;
  --fresh:#1a7f37; --aging:#9a6700; --stale:#cf222e; --miss:#eaeef2;
  --card:#ffffff;
}
@media (prefers-color-scheme:dark){:root{
  --bg:#0e1116; --panel:#161b22; --line:#242b34; --ink:#cdd5de; --mut:#7d8792;
  --accent:#2dd4bf; --accent-soft:#134e4a;
  --fresh:#3fb950; --aging:#d29922; --stale:#f85149; --miss:#21262d; --card:#12171e;
}}
:root[data-theme="light"]{--bg:#f6f8fa;--panel:#fff;--line:#d8dee4;--ink:#1c2128;--mut:#656d76;--accent:#0d9488;--fresh:#1a7f37;--aging:#9a6700;--stale:#cf222e;--miss:#eaeef2;--card:#fff;}
:root[data-theme="dark"]{--bg:#0e1116;--panel:#161b22;--line:#242b34;--ink:#cdd5de;--mut:#7d8792;--accent:#2dd4bf;--fresh:#3fb950;--aging:#d29922;--stale:#f85149;--miss:#21262d;--card:#12171e;}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;line-height:1.5}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-variant-numeric:tabular-nums}
main{max-width:1120px;margin:0 auto;padding:32px 24px 64px}
header{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px;
  padding-bottom:20px;border-bottom:1px solid var(--line);margin-bottom:24px}
.brand{font-size:20px;font-weight:650;letter-spacing:-.01em;display:flex;align-items:center;gap:10px}
.live{width:9px;height:9px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 0 0 var(--accent);animation:pulse 2.4s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 color-mix(in srgb,var(--accent) 60%,transparent)}
  70%{box-shadow:0 0 0 7px transparent}100%{box-shadow:0 0 0 0 transparent}}
@media (prefers-reduced-motion:reduce){.live{animation:none}}
.gen{font-size:12px;color:var(--mut)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.eyebrow{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);font-weight:600}
.metric{font-size:30px;font-weight:680;letter-spacing:-.02em;margin:4px 0 2px;
  font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-variant-numeric:tabular-nums}
.sub{font-size:12.5px;color:var(--mut)}
.quota{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:26px}
.quota .lbl{font-size:11px;text-transform:uppercase;letter-spacing:.09em;color:var(--mut);font-weight:600}
.pill{font-family:ui-monospace,monospace;font-size:12px;background:var(--panel);
  border:1px solid var(--line);border-radius:20px;padding:5px 12px}
.pill b{color:var(--accent)}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:20px 22px;margin-bottom:22px}
h2{font-size:15px;font-weight:640;margin:0 0 16px;letter-spacing:-.01em}
h2 .muted{font-weight:400;font-size:13px}
.muted{color:var(--mut)}
table{width:100%;border-collapse:collapse;font-size:13px}
.queue th,.signals th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
  color:var(--mut);font-weight:600;padding:0 10px 10px;border-bottom:1px solid var(--line)}
.queue td,.signals td{padding:9px 10px;border-bottom:1px solid var(--line)}
.queue tr:last-child td,.signals tr:last-child td{border-bottom:none}
.kind{font-weight:600;color:var(--ink)}.src{color:var(--mut);font-size:12px}
.num{text-align:right}.barcell{width:42%}
.bar{display:inline-block;width:calc(100% - 46px);height:7px;border-radius:4px;
  background:var(--miss);vertical-align:middle;overflow:hidden}
.fill{display:block;height:100%;border-radius:4px;background:var(--accent)}
.fill.ok{background:var(--fresh)}.fill.warn{background:var(--stale)}.fill.run{background:var(--accent)}
.pct{display:inline-block;width:40px;text-align:right;font-family:ui-monospace,monospace;
  font-size:12px;font-variant-numeric:tabular-nums;color:var(--mut)}
.chip{font-family:ui-monospace,monospace;font-size:11px;padding:2px 7px;border-radius:6px;
  background:var(--miss);color:var(--mut);margin-right:5px}
.chip.crit{background:color-mix(in srgb,var(--stale) 18%,transparent);color:var(--stale)}
.legend{display:flex;gap:16px;font-size:12px;color:var(--mut);margin-bottom:14px}
.legend i{width:12px;height:12px;border-radius:3px;display:inline-block;vertical-align:-2px;margin-right:5px}
.heatwrap{overflow-x:auto}
.heat{border-collapse:separate;border-spacing:2px}
.heat .corner{width:56px}
.heat th.rot{height:70px;width:26px;padding:0;vertical-align:bottom}
.heat th.rot span{display:inline-block;writing-mode:vertical-rl;transform:rotate(180deg);
  font-size:11px;color:var(--mut);font-weight:600;letter-spacing:.03em;padding-bottom:6px}
.heat .tick{text-align:right;font-size:11px;color:var(--mut);padding-right:8px;font-weight:500;
  position:sticky;left:0;background:var(--panel)}
.cell{width:26px;height:16px;border-radius:3px;background:var(--miss);padding:0}
.cell.fresh{background:var(--fresh)}.cell.aging{background:var(--aging)}.cell.stale{background:var(--stale)}
.cell.miss{background:var(--miss);border:1px solid var(--line)}
.dot{width:8px;height:8px;border-radius:50%;display:inline-block;margin-right:7px;background:var(--miss)}
.dot.fresh{background:var(--fresh)}.dot.aging{background:var(--aging)}.dot.stale{background:var(--stale)}
</style>"""


def main():
    from collector import default_collector
    from universe import UNIVERSE
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("runtime/collector_dashboard.html")
    col = default_collector()
    report = col.coverage_report()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(report, UNIVERSE))
    print(f"wrote {out}  (backfill {report['overall']['pct']}%, "
          f"{report['overall']['data_points']:,} data points)")


if __name__ == "__main__":
    main()
