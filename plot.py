#!/usr/bin/env python3
"""Live plot server for mmapbench. Reads CSV from stdin or --file, serves a dashboard with SSE updates."""

import argparse
import json
import sys
import threading
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn

COLUMNS = ["dev", "seq", "hint", "threads", "time", "workGB", "tlb", "readGB", "CPUwork"]

data_rows = []
data_lock = threading.Lock()
reader_done = False

cache_rows = []
cache_lock = threading.Lock()

cpu_usage = []  # latest snapshot: list of per-core usage percentages
cpu_lock = threading.Lock()


def read_cpu_ticks():
    """Read per-core CPU ticks from /proc/stat. Returns list of (total, idle) per core."""
    cores = []
    with open("/proc/stat") as f:
        for line in f:
            if line.startswith("cpu") and not line.startswith("cpu "):
                parts = line.split()
                # user nice system idle iowait irq softirq steal
                vals = [int(x) for x in parts[1:]]
                idle = vals[3] + vals[4]  # idle + iowait
                total = sum(vals)
                cores.append((total, idle))
    return cores


def read_page_cache():
    """Read /proc/meminfo and return (cache_pct, cache_gb, total_gb)."""
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            parts = line.split()
            if len(parts) >= 2:
                info[parts[0].rstrip(":")] = int(parts[1])  # kB
    total = info.get("MemTotal", 1)
    cached = info.get("Cached", 0) + info.get("SReclaimable", 0) + info.get("Buffers", 0)
    return (cached / total * 100, cached / (1024 * 1024), total / (1024 * 1024))

HTML_PAGE = r"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<title>mmapbench live</title>
<script src="https://cdn.jsdelivr.net/npm/uplot@1.6.30/dist/uPlot.iife.min.js"></script>
<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/uplot@1.6.30/dist/uPlot.min.css">
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body { background: #1a1a2e; color: #e0e0e0; font-family: system-ui, sans-serif; padding: 16px; }
  h1 { text-align: center; margin-bottom: 12px; font-size: 1.4em; color: #8be9fd; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
  .chart-box { background: #16213e; border-radius: 8px; padding: 12px; }
  .chart-box h2 { font-size: 0.95em; margin-bottom: 6px; color: #bd93f9; }
  .status { text-align: center; margin-top: 8px; font-size: 0.85em; color: #6272a4; }
  .u-legend { font-size: 0.8em !important; }
  .u-legend th, .u-legend td { padding: 2px 6px !important; }
  .cpu-panel { background: #16213e; border-radius: 8px; padding: 12px; margin-bottom: 12px; }
  .cpu-panel h2 { font-size: 0.95em; margin-bottom: 8px; color: #bd93f9; }
  .cpu-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(60px, 1fr)); gap: 4px; }
  .cpu-cell { text-align: center; padding: 4px 2px; border-radius: 4px; font-size: 0.85em;
              font-weight: 600; font-variant-numeric: tabular-nums; }
</style>
</head><body>
<h1>mmapbench live</h1>
<div class="cpu-panel">
  <h2>CPU Cores</h2>
  <div class="cpu-grid" id="cpu-grid"></div>
</div>
<div class="grid">
  <div class="chart-box"><h2>mmap Throughput (GB)</h2><div id="c-workGB"></div></div>
  <div class="chart-box"><h2>I/O Bandwidth (GB)</h2><div id="c-readGB"></div></div>
  <div class="chart-box"><h2>TLB Shootdowns</h2><div id="c-tlb"></div></div>
  <div class="chart-box"><h2>CPU Work</h2><div id="c-CPUwork"></div></div>
  <div class="chart-box" style="grid-column: span 2;"><h2>Page Cache Usage (%)</h2><div id="c-cache"></div></div>
</div>
<div class="status" id="status">connecting...</div>
<script>
const CHARTS = [
  { el: "c-workGB", col: "workGB", label: "workGB", color: "#50fa7b" },
  { el: "c-readGB", col: "readGB", label: "readGB", color: "#ff79c6" },
  { el: "c-tlb",    col: "tlb",    label: "tlb",    color: "#ffb86c" },
  { el: "c-CPUwork",col: "CPUwork",label: "CPUwork", color: "#8be9fd" },
];

function chartSize() {
  const w = Math.max(300, (window.innerWidth - 60) / 2);
  const h = Math.max(200, (window.innerHeight - 160) / 2);
  return { width: w, height: h };
}

function makeOpts(label, color) {
  const sz = chartSize();
  return {
    width: sz.width, height: sz.height,
    cursor: { drag: { x: true, y: true } },
    scales: { x: { time: false }, y: { auto: true } },
    axes: [
      { stroke: "#6272a4", grid: { stroke: "rgba(98,114,164,0.3)" }, ticks: { stroke: "rgba(98,114,164,0.3)" }, font: "11px system-ui", labelFont: "11px system-ui" },
      { stroke: "#6272a4", grid: { stroke: "rgba(98,114,164,0.3)" }, ticks: { stroke: "rgba(98,114,164,0.3)" }, font: "11px system-ui", labelFont: "11px system-ui" },
    ],
    series: [
      { label: "time" },
      { label: label, stroke: color, width: 2, fill: color + "18" },
    ],
  };
}

const plots = {};
let allRows = [];
let cacheRows = [];
let cachePlot = null;

function initPlots() {
  CHARTS.forEach(c => {
    const el = document.getElementById(c.el);
    el.innerHTML = "";
    const opts = makeOpts(c.label, c.color);
    const times = allRows.map(r => r.time);
    const vals = allRows.map(r => r[c.col]);
    plots[c.col] = new uPlot(opts, [times, vals], el);
  });
  // cache chart: full width
  const cel = document.getElementById("c-cache");
  cel.innerHTML = "";
  const csz = chartSize();
  const cacheOpts = makeOpts("cache %", "#f1fa8c");
  cacheOpts.width = csz.width * 2 + 12;
  cacheOpts.scales.y = { auto: false, range: [0, 100] };
  const ct = cacheRows.map(r => r.time);
  const cv = cacheRows.map(r => r.cachePct);
  cachePlot = new uPlot(cacheOpts, [ct, cv], cel);
}

function updatePlots() {
  CHARTS.forEach(c => {
    const p = plots[c.col];
    if (!p) return;
    const times = allRows.map(r => r.time);
    const vals = allRows.map(r => r[c.col]);
    p.setData([times, vals]);
  });
  if (cachePlot) {
    cachePlot.setData([cacheRows.map(r => r.time), cacheRows.map(r => r.cachePct)]);
  }
}

function parseRow(r) {
  return { time: +r.time, workGB: +r.workGB, tlb: +r.tlb, readGB: +r.readGB, CPUwork: +r.CPUwork };
}

// Initial data load
Promise.all([
  fetch("/data").then(r => r.json()),
  fetch("/cache").then(r => r.json()),
]).then(([rows, cache]) => {
  allRows = rows.map(parseRow);
  cacheRows = cache;
  initPlots();
  document.getElementById("status").textContent = "connected — " + allRows.length + " points";
  startSSE();
});

function startSSE() {
  const es = new EventSource("/events");
  es.onmessage = function(e) {
    const msg = JSON.parse(e.data);
    if (msg.bench) { msg.bench.forEach(r => allRows.push(parseRow(r))); }
    if (msg.cache) { msg.cache.forEach(r => cacheRows.push(r)); }
    updatePlots();
    document.getElementById("status").textContent = "live — " + allRows.length + " points";
  };
  es.addEventListener("done", function() {
    document.getElementById("status").textContent = "complete — " + allRows.length + " points";
    es.close();
  });
  es.onerror = function() {
    document.getElementById("status").textContent = "disconnected — " + allRows.length + " points";
  };
}

window.addEventListener("resize", () => {
  const sz = chartSize();
  CHARTS.forEach(c => { if (plots[c.col]) plots[c.col].setSize(sz); });
  if (cachePlot) cachePlot.setSize({ width: sz.width * 2 + 12, height: sz.height });
});

// CPU grid - poll /cpu every second
function cpuColor(pct) {
  if (pct < 25) return "rgba(80,250,123,0.25)";
  if (pct < 50) return "rgba(241,250,140,0.35)";
  if (pct < 75) return "rgba(255,184,108,0.45)";
  return "rgba(255,85,85,0.55)";
}

function updateCPU(cores) {
  const grid = document.getElementById("cpu-grid");
  while (grid.children.length < cores.length) {
    const d = document.createElement("div");
    d.className = "cpu-cell";
    grid.appendChild(d);
  }
  cores.forEach((pct, i) => {
    const cell = grid.children[i];
    cell.textContent = pct.toFixed(0) + "%";
    cell.style.background = cpuColor(pct);
  });
}

setInterval(() => {
  fetch("/cpu").then(r => r.json()).then(updateCPU).catch(() => {});
}, 1000);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # suppress per-request logging

    def do_GET(self):
        if self.path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML_PAGE.encode())
        elif self.path == "/data":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with data_lock:
                snapshot = list(data_rows)
            self.wfile.write(json.dumps(snapshot).encode())
        elif self.path == "/cache":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with cache_lock:
                snapshot = list(cache_rows)
            self.wfile.write(json.dumps(snapshot).encode())
        elif self.path == "/cpu":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with cpu_lock:
                snapshot = list(cpu_usage)
            self.wfile.write(json.dumps(snapshot).encode())
        elif self.path == "/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            bench_cursor = 0
            cache_cursor = 0
            with data_lock:
                bench_cursor = len(data_rows)
            with cache_lock:
                cache_cursor = len(cache_rows)
            try:
                while True:
                    new_bench = None
                    new_cache = None
                    done = False
                    with data_lock:
                        if len(data_rows) > bench_cursor:
                            new_bench = data_rows[bench_cursor:]
                            bench_cursor = len(data_rows)
                        done = reader_done and bench_cursor >= len(data_rows)
                    with cache_lock:
                        if len(cache_rows) > cache_cursor:
                            new_cache = cache_rows[cache_cursor:]
                            cache_cursor = len(cache_rows)
                    if new_bench or new_cache:
                        msg = {}
                        if new_bench:
                            msg["bench"] = new_bench
                        if new_cache:
                            msg["cache"] = new_cache
                        self.wfile.write(f"data: {json.dumps(msg)}\n\n".encode())
                        self.wfile.flush()
                    if done:
                        self.wfile.write(b"event: done\ndata: {}\n\n")
                        self.wfile.flush()
                        break
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            self.send_response(404)
            self.end_headers()


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def parse_csv_line(line):
    """Parse a CSV line into a dict. Returns None for header/invalid lines."""
    line = line.strip()
    if not line:
        return None
    parts = line.split(",")
    if len(parts) < 9:
        return None
    # skip header line
    if parts[0].strip() == "dev":
        return None
    try:
        return {
            "dev": parts[0].strip(),
            "seq": int(parts[1]),
            "hint": int(parts[2]),
            "threads": int(parts[3]),
            "time": float(parts[4]),
            "workGB": float(parts[5]),
            "tlb": int(parts[6]),
            "readGB": float(parts[7]),
            "CPUwork": int(parts[8]),
        }
    except (ValueError, IndexError):
        return None


def cpu_monitor_thread():
    """Poll /proc/stat every second and compute per-core CPU usage."""
    prev = read_cpu_ticks()
    while True:
        time.sleep(1.0)
        cur = read_cpu_ticks()
        usage = []
        for (pt, pi), (ct, ci) in zip(prev, cur):
            dt = ct - pt
            di = ci - pi
            usage.append(round((1 - di / max(dt, 1)) * 100, 1))
        with cpu_lock:
            cpu_usage.clear()
            cpu_usage.extend(usage)
        prev = cur


def cache_monitor_thread(start_time):
    """Poll /proc/meminfo every second and record page cache usage."""
    while not reader_done:
        try:
            pct, cache_gb, total_gb = read_page_cache()
            row = {"time": time.monotonic() - start_time, "cachePct": round(pct, 1),
                   "cacheGB": round(cache_gb, 2), "totalGB": round(total_gb, 2)}
            with cache_lock:
                cache_rows.append(row)
        except Exception:
            pass
        time.sleep(1.0)
    # one final sample after reader ends
    try:
        pct, cache_gb, total_gb = read_page_cache()
        row = {"time": time.monotonic() - start_time, "cachePct": round(pct, 1),
               "cacheGB": round(cache_gb, 2), "totalGB": round(total_gb, 2)}
        with cache_lock:
            cache_rows.append(row)
    except Exception:
        pass


def reader_thread(source):
    """Read CSV lines from source (file object), parse and append to data_rows."""
    global reader_done
    try:
        for line in source:
            row = parse_csv_line(line)
            if row:
                with data_lock:
                    data_rows.append(row)
    except Exception as e:
        print(f"reader error: {e}", file=sys.stderr)
    finally:
        reader_done = True
        print("reader: input ended, server still running", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Live plot server for mmapbench")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (default: 8080)")
    parser.add_argument("--file", type=str, default=None, help="Read CSV from file instead of stdin")
    args = parser.parse_args()

    if args.file:
        source = open(args.file, "r")
    else:
        source = sys.stdin

    start_time = time.monotonic()

    t = threading.Thread(target=reader_thread, args=(source,), daemon=True)
    t.start()

    ct = threading.Thread(target=cache_monitor_thread, args=(start_time,), daemon=True)
    ct.start()

    cput = threading.Thread(target=cpu_monitor_thread, daemon=True)
    cput.start()

    server = ThreadedHTTPServer(("0.0.0.0", args.port), Handler)
    print(f"serving on http://0.0.0.0:{args.port}", file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nshutting down", file=sys.stderr)
        server.shutdown()


if __name__ == "__main__":
    main()
