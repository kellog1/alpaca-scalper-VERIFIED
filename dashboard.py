"""Live P&L dashboard for the scalper bot.

Zero extra dependencies: Python stdlib HTTP server + the bot's SQLite log.
Charts render in the browser via Chart.js (CDN).

Usage:
    python dashboard.py                 # http://localhost:8080
    python dashboard.py --port 9000 --db scalper_log.db

Auto-refreshes every 15 seconds. Read-only: safe to run alongside the live bot.
"""
import argparse
import json
import sqlite3
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler

DB_PATH = "scalper_log.db"


# ---------------------------------------------------------------- queries --
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def q_summary():
    with _conn() as c:
        row = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl, "
            "COALESCE(AVG(hold_secs),0) hold, "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins, "
            "COALESCE(SUM(CASE WHEN pnl>0 THEN pnl ELSE 0 END),0) gw, "
            "COALESCE(SUM(CASE WHEN pnl<0 THEN -pnl ELSE 0 END),0) gl "
            "FROM trades").fetchone()
        today = c.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(pnl),0) pnl FROM trades "
            "WHERE date(close_ts)=date('now')").fetchone()
        sigs = c.execute(
            "SELECT COUNT(*) total, SUM(acted) acted FROM signals").fetchone()
    n = row["n"] or 0
    return {
        "trades": n,
        "pnl": round(row["pnl"], 2),
        "win_rate": round(row["wins"] / n * 100, 1) if n else 0,
        "profit_factor": round(row["gw"] / row["gl"], 2) if row["gl"] else None,
        "avg_hold_secs": round(row["hold"], 0),
        "today_trades": today["n"] or 0,
        "today_pnl": round(today["pnl"], 2),
        "signals_total": sigs["total"] or 0,
        "signals_acted": sigs["acted"] or 0,
    }


def q_equity():
    """Cumulative net P&L per closed trade, plus running max drawdown."""
    with _conn() as c:
        rows = c.execute(
            "SELECT close_ts, pnl FROM trades ORDER BY close_ts").fetchall()
    labels, cum_series, dd_series = [], [], []
    cum = peak = 0.0
    for r in rows:
        cum += r["pnl"]
        peak = max(peak, cum)
        labels.append(r["close_ts"][:16].replace("T", " "))
        cum_series.append(round(cum, 2))
        dd_series.append(round(cum - peak, 2))  # <= 0
    return {"labels": labels, "cum_pnl": cum_series, "drawdown": dd_series}


def q_symbols():
    with _conn() as c:
        rows = c.execute(
            "SELECT symbol, COUNT(*) n, SUM(pnl) pnl, "
            "SUM(CASE WHEN pnl>0 THEN 1 ELSE 0 END) wins "
            "FROM trades GROUP BY symbol ORDER BY pnl DESC").fetchall()
    return [{"symbol": r["symbol"], "trades": r["n"],
             "pnl": round(r["pnl"], 2),
             "win_rate": round(r["wins"] / r["n"] * 100, 1)} for r in rows]


def q_exits():
    with _conn() as c:
        rows = c.execute(
            "SELECT exit_reason, COUNT(*) n, SUM(pnl) pnl FROM trades "
            "GROUP BY exit_reason").fetchall()
    return [{"reason": r["exit_reason"], "count": r["n"],
             "pnl": round(r["pnl"], 2)} for r in rows]


def q_trades(limit=50):
    with _conn() as c:
        rows = c.execute(
            "SELECT close_ts, symbol, side, qty, entry, exit, pnl, "
            "hold_secs, exit_reason FROM trades "
            "ORDER BY close_ts DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def q_hourly():
    """Today's P&L bucketed by hour — shows when the strategy makes/loses."""
    with _conn() as c:
        rows = c.execute(
            "SELECT strftime('%H', close_ts) h, SUM(pnl) pnl, COUNT(*) n "
            "FROM trades GROUP BY h ORDER BY h").fetchall()
    return [{"hour": r["h"], "pnl": round(r["pnl"], 2), "n": r["n"]}
            for r in rows]


ROUTES = {
    "/api/summary": q_summary,
    "/api/equity": q_equity,
    "/api/symbols": q_symbols,
    "/api/exits": q_exits,
    "/api/trades": q_trades,
    "/api/hourly": q_hourly,
}


# ------------------------------------------------------------------- page --
PAGE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Scalper Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root { color-scheme: dark; }
* { box-sizing: border-box; margin: 0; }
body { background:#101418; color:#d7dce1; font:14px/1.5 -apple-system,system-ui,sans-serif; padding:20px; }
h1 { font-size:18px; margin-bottom:4px; }
.sub { color:#7a838d; font-size:12px; margin-bottom:20px; }
.cards { display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:20px; }
.card { background:#181e25; border:1px solid #232b34; border-radius:10px; padding:14px; }
.card .k { color:#7a838d; font-size:11px; text-transform:uppercase; letter-spacing:.05em; }
.card .v { font-size:22px; font-weight:600; margin-top:4px; }
.pos { color:#3ecf8e; } .neg { color:#ff5c5c; }
.grid { display:grid; grid-template-columns:2fr 1fr; gap:12px; margin-bottom:20px; }
.panel { background:#181e25; border:1px solid #232b34; border-radius:10px; padding:14px; }
.panel h2 { font-size:13px; color:#7a838d; margin-bottom:10px; }
table { width:100%; border-collapse:collapse; font-size:12px; }
th { text-align:left; color:#7a838d; font-weight:500; padding:6px 8px; border-bottom:1px solid #232b34; }
td { padding:6px 8px; border-bottom:1px solid #1c232b; }
@media (max-width:900px){ .grid { grid-template-columns:1fr; } }
</style></head><body>
<h1>Scalper Dashboard</h1>
<div class="sub">Paper trading &middot; auto-refresh 15s &middot; <span id="ts"></span></div>
<div class="cards" id="cards"></div>
<div class="grid">
  <div class="panel"><h2>Cumulative net P&amp;L ($)</h2><canvas id="equity" height="110"></canvas></div>
  <div class="panel"><h2>Drawdown ($)</h2><canvas id="dd" height="110"></canvas></div>
</div>
<div class="grid">
  <div class="panel"><h2>P&amp;L by symbol</h2><canvas id="sym" height="110"></canvas></div>
  <div class="panel"><h2>Exit reasons</h2><canvas id="exits" height="110"></canvas></div>
</div>
<div class="panel"><h2>Recent trades</h2>
<table><thead><tr><th>Closed</th><th>Symbol</th><th>Side</th><th>Qty</th>
<th>Entry</th><th>Exit</th><th>P&amp;L</th><th>Hold</th><th>Reason</th></tr></thead>
<tbody id="trades"></tbody></table></div>
<script>
const charts = {};
function mk(id, cfg) {
  if (charts[id]) { charts[id].destroy(); }
  charts[id] = new Chart(document.getElementById(id), cfg);
}
const gridCol = "#232b34", tickCol = "#7a838d";
const axis = { grid: { color: gridCol }, ticks: { color: tickCol, maxTicksLimit: 8 } };
async function j(u) { const r = await fetch(u); return r.json(); }
function money(v) { return (v >= 0 ? "+" : "") + v.toLocaleString(undefined, {maximumFractionDigits: 0}); }

async function refresh() {
  const [s, eq, sym, ex, tr] = await Promise.all([
    j("/api/summary"), j("/api/equity"), j("/api/symbols"),
    j("/api/exits"), j("/api/trades")]);

  const cls = v => v >= 0 ? "pos" : "neg";
  document.getElementById("cards").innerHTML = `
    <div class="card"><div class="k">Total P&L</div><div class="v ${cls(s.pnl)}">$${money(s.pnl)}</div></div>
    <div class="card"><div class="k">Today P&L</div><div class="v ${cls(s.today_pnl)}">$${money(s.today_pnl)}</div></div>
    <div class="card"><div class="k">Trades</div><div class="v">${s.trades}</div></div>
    <div class="card"><div class="k">Win rate</div><div class="v">${s.win_rate}%</div></div>
    <div class="card"><div class="k">Profit factor</div><div class="v">${s.profit_factor ?? "–"}</div></div>
    <div class="card"><div class="k">Avg hold</div><div class="v">${s.avg_hold_secs}s</div></div>
    <div class="card"><div class="k">Signals acted</div><div class="v">${s.signals_acted}/${s.signals_total}</div></div>`;

  mk("equity", { type: "line", data: { labels: eq.labels, datasets: [{
      data: eq.cum_pnl, borderColor: "#3ecf8e", backgroundColor: "rgba(62,207,142,.08)",
      fill: true, pointRadius: 0, borderWidth: 1.5, tension: .15 }]},
    options: { plugins: { legend: { display: false } }, scales: { x: axis, y: axis },
               animation: false, maintainAspectRatio: true }});

  mk("dd", { type: "line", data: { labels: eq.labels, datasets: [{
      data: eq.drawdown, borderColor: "#ff5c5c", backgroundColor: "rgba(255,92,92,.10)",
      fill: true, pointRadius: 0, borderWidth: 1.5, tension: .15 }]},
    options: { plugins: { legend: { display: false } }, scales: { x: axis, y: axis },
               animation: false, maintainAspectRatio: true }});

  mk("sym", { type: "bar", data: { labels: sym.map(r => r.symbol), datasets: [{
      data: sym.map(r => r.pnl),
      backgroundColor: sym.map(r => r.pnl >= 0 ? "rgba(62,207,142,.7)" : "rgba(255,92,92,.7)") }]},
    options: { plugins: { legend: { display: false } }, scales: { x: axis, y: axis },
               animation: false, maintainAspectRatio: true }});

  mk("exits", { type: "doughnut", data: { labels: ex.map(r => `${r.reason} (${r.count})`),
      datasets: [{ data: ex.map(r => r.count),
        backgroundColor: ["#3ecf8e", "#ff5c5c", "#e8b93e", "#5c9dff", "#b57edc", "#7a838d"] }]},
    options: { plugins: { legend: { position: "right", labels: { color: tickCol } } },
               animation: false, maintainAspectRatio: true }});

  document.getElementById("trades").innerHTML = tr.map(t => `<tr>
    <td>${t.close_ts.slice(0, 16).replace("T", " ")}</td><td>${t.symbol}</td>
    <td>${t.side}</td><td>${t.qty}</td>
    <td>${t.entry.toFixed(2)}</td><td>${t.exit.toFixed(2)}</td>
    <td class="${cls(t.pnl)}">$${money(t.pnl)}</td>
    <td>${Math.round(t.hold_secs)}s</td><td>${t.exit_reason}</td></tr>`).join("");

  document.getElementById("ts").textContent = "updated " + new Date().toLocaleTimeString();
}
refresh(); setInterval(refresh, 15000);
</script></body></html>"""


# ------------------------------------------------------------------ server --
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            body = PAGE.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        elif self.path in ROUTES:
            try:
                body = json.dumps(ROUTES[self.path]()).encode()
                self.send_response(200)
            except Exception as e:  # e.g. db not created yet
                body = json.dumps({"error": str(e)}).encode()
                self.send_response(500)
            self.send_header("Content-Type", "application/json")
        else:
            body = b"not found"
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # quiet
        pass


def main():
    global DB_PATH
    p = argparse.ArgumentParser(description="Scalper bot dashboard")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument("--db", default="scalper_log.db")
    args = p.parse_args()
    DB_PATH = args.db
    print(f"Dashboard: http://localhost:{args.port}  (db: {DB_PATH})")
    HTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
