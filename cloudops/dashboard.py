"""Local web dashboard: stat tiles (running instances, burn rate, month-to-date
spend, Vast balance) plus an instance table, served on localhost with zero extra
dependencies (stdlib http.server). Data refreshes in the browser; usage is cached
server-side because each AWS Cost Explorer query costs ~$0.01.
"""
from __future__ import annotations

import argparse
import json
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .providers import resolve_providers

DEFAULT_PORT = 8787
USAGE_TTL_SECONDS = 600  # Cost Explorer queries are billed — don't hammer them
INSTANCES_TTL_SECONDS = 20

_cache_lock = threading.Lock()
_cache: dict = {}


def _cached(key: str, ttl: float, fn):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() - entry[0] < ttl:
            return entry[1]
    value = fn()
    with _cache_lock:
        _cache[key] = (time.time(), value)
    return value


def _fetch_instances() -> dict:
    instances, errors = [], []
    for name, provider, err in resolve_providers("all"):
        if err:
            errors.append({"provider": name, "error": err})
            continue
        try:
            instances.extend(i.to_dict() for i in provider.list_instances())
        except Exception as exc:
            errors.append({"provider": name, "error": str(exc)})
    return {"instances": instances, "errors": errors}


def _fetch_usage() -> dict:
    usages = []
    for name, provider, err in resolve_providers("all"):
        if err:
            usages.append({"provider": name, "error": err})
            continue
        try:
            usages.append(provider.usage())
        except Exception as exc:
            usages.append({"provider": name, "error": str(exc)})
    return {"usage": usages}


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>cloudops dashboard</title>
<style>
  :root {
    color-scheme: light;
    --page:           #f9f9f7;
    --surface-1:      #fcfcfb;
    --text-primary:   #0b0b0b;
    --text-secondary: #52514e;
    --text-muted:     #898781;
    --grid:           #e1e0d9;
    --border:         rgba(11,11,11,0.10);
    --status-good:    #0ca30c;
    --status-warning: #fab219;
    --status-critical:#d03b3b;
    --delta-good:     #006300;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      color-scheme: dark;
      --page:           #0d0d0d;
      --surface-1:      #1a1a19;
      --text-primary:   #ffffff;
      --text-secondary: #c3c2b7;
      --text-muted:     #898781;
      --grid:           #2c2c2a;
      --border:         rgba(255,255,255,0.10);
      --delta-good:     #0ca30c;
    }
  }
  * { box-sizing: border-box; margin: 0; }
  body {
    font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
    background: var(--page); color: var(--text-primary);
    padding: 24px; max-width: 1100px; margin: 0 auto;
  }
  h1 { font-size: 18px; font-weight: 650; }
  .sub { color: var(--text-secondary); font-size: 13px; margin: 4px 0 20px; }
  .tiles { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 12px; margin-bottom: 24px; }
  .tile {
    background: var(--surface-1); border: 1px solid var(--border);
    border-radius: 10px; padding: 14px 16px;
  }
  .tile .label { font-size: 12px; color: var(--text-secondary); margin-bottom: 6px; }
  .tile .value { font-size: 26px; font-weight: 650; }
  .tile .detail { font-size: 12px; color: var(--text-muted); margin-top: 4px; }
  h2 { font-size: 14px; font-weight: 650; margin: 20px 0 8px; }
  .card { background: var(--surface-1); border: 1px solid var(--border); border-radius: 10px; overflow-x: auto; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th { text-align: left; color: var(--text-secondary); font-weight: 600; padding: 10px 12px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  td { padding: 9px 12px; border-bottom: 1px solid var(--grid); white-space: nowrap; }
  tr:last-child td { border-bottom: none; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
  .status { display: inline-flex; align-items: center; gap: 6px; }
  .status .dot { width: 8px; height: 8px; border-radius: 50%; flex: none; }
  .muted { color: var(--text-muted); }
  .err { color: var(--status-critical); font-size: 13px; margin: 8px 0; }
  footer { color: var(--text-muted); font-size: 12px; margin-top: 16px; }
</style>
</head>
<body>
  <h1>cloudops dashboard</h1>
  <p class="sub">AWS + Vast.ai — instances and spend. Instances refresh every 30&nbsp;s; usage every 10&nbsp;min (Cost Explorer queries are billed).</p>

  <div class="tiles" id="tiles"></div>

  <h2>Instances</h2>
  <div class="card"><table id="instances">
    <thead><tr>
      <th>Provider</th><th>ID</th><th>Name</th><th>Status</th><th>Type</th>
      <th>Region</th><th>IP / SSH</th><th class="num">$/hr</th><th>Managed</th>
    </tr></thead>
    <tbody></tbody>
  </table></div>
  <div id="errors"></div>

  <h2>AWS month-to-date by service</h2>
  <div class="card"><table id="services">
    <thead><tr><th>Service</th><th class="num">USD</th></tr></thead>
    <tbody></tbody>
  </table></div>

  <footer>Local only (127.0.0.1). Spawn/terminate stay in the scripts &amp; CLI — this view is read-only.</footer>

<script>
const money = (v, d = 2) => v == null ? "—" : "$" + Number(v).toLocaleString(undefined, {minimumFractionDigits: d, maximumFractionDigits: d});
// Status is never color-alone: dot + text label together.
const STATUS = {
  running: "var(--status-good)", pending: "var(--status-warning)", loading: "var(--status-warning)",
  created: "var(--status-warning)", stopping: "var(--status-warning)",
  stopped: "var(--status-critical)", exited: "var(--status-critical)",
};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));

function tile(label, value, detail) {
  return `<div class="tile"><div class="label">${esc(label)}</div><div class="value">${value}</div><div class="detail">${esc(detail || "")}</div></div>`;
}

async function refreshUsage() {
  const data = await (await fetch("/api/usage")).json();
  let running = 0, burn = 0, mtd = null, balance = null, notes = [];
  for (const u of data.usage) {
    if (u.error) { notes.push(`${u.provider}: ${u.error}`); continue; }
    running += u.running_instances || 0;
    burn += u.burn_usd_per_hour || 0;
    if (u.month_to_date_usd != null) mtd = (mtd || 0) + u.month_to_date_usd;
    if (u.balance_usd != null) balance = u.balance_usd;
  }
  document.getElementById("tiles").innerHTML =
    tile("Running instances", running, "across all providers") +
    tile("Burn rate", money(burn, 3) + "<span class='muted' style='font-size:14px'>/hr</span>", "running instances only") +
    tile("AWS month-to-date", money(mtd), "Cost Explorer, all services") +
    tile("Vast.ai balance", money(balance), "prepaid credit");
  const aws = data.usage.find(u => u.by_service);
  document.querySelector("#services tbody").innerHTML = aws
    ? aws.by_service.map(r => `<tr><td>${esc(r.service)}</td><td class="num">${money(r.usd)}</td></tr>`).join("")
    : `<tr><td colspan="2" class="muted">unavailable</td></tr>`;
  renderErrors(notes.map(n => ({error: n})));
}

function renderErrors(list) {
  document.getElementById("errors").innerHTML =
    (list || []).map(e => `<p class="err">⚠ ${esc(e.provider ? e.provider + ": " : "")}${esc(e.error)}</p>`).join("");
}

async function refreshInstances() {
  const data = await (await fetch("/api/instances")).json();
  const rows = data.instances.map(i => {
    const color = STATUS[i.status] || "var(--text-muted)";
    return `<tr>
      <td>${esc(i.provider)}</td><td>${esc(i.id)}</td><td>${esc(i.name) || "—"}</td>
      <td><span class="status"><span class="dot" style="background:${color}"></span>${esc(i.status)}</span></td>
      <td>${esc(i.instance_type)}</td><td>${esc(i.region) || "—"}</td><td>${esc(i.ip) || "—"}</td>
      <td class="num">${money(i.hourly_usd, 3)}</td><td>${i.managed ? "yes" : "no"}</td>
    </tr>`;
  });
  document.querySelector("#instances tbody").innerHTML =
    rows.join("") || `<tr><td colspan="9" class="muted">no instances</td></tr>`;
  if (data.errors && data.errors.length) renderErrors(data.errors);
}

refreshUsage(); refreshInstances();
setInterval(refreshInstances, 30000);
setInterval(refreshUsage, 600000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    def _send(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802 (http.server API)
        try:
            if self.path in ("/", "/index.html"):
                self._send(PAGE.encode(), "text/html; charset=utf-8")
            elif self.path == "/api/instances":
                data = _cached("instances", INSTANCES_TTL_SECONDS, _fetch_instances)
                self._send(json.dumps(data).encode(), "application/json")
            elif self.path == "/api/usage":
                data = _cached("usage", USAGE_TTL_SECONDS, _fetch_usage)
                self._send(json.dumps(data).encode(), "application/json")
            else:
                self._send(b"not found", "text/plain", 404)
        except Exception as exc:
            self._send(json.dumps({"error": str(exc)}).encode(), "application/json", 500)

    def log_message(self, *args):  # keep the terminal quiet
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Local cloudops dashboard")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--no-browser", action="store_true", help="don't auto-open the browser")
    args = parser.parse_args()

    server = ThreadingHTTPServer(("127.0.0.1", args.port), _Handler)
    url = f"http://127.0.0.1:{args.port}"
    print(f"cloudops dashboard: {url}  (Ctrl-C to stop)")
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
