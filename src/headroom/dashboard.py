"""Local dashboard. It listens on 127.0.0.1 and requires a token.

Approving a review item posts the word MOVE. The move uses the same archive or
Recycle Bin path as ``headroom review``. Charts are drawn with the page's own
script. Nothing is loaded from another host.
"""

from __future__ import annotations

import html
import json
import os
import secrets
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from headroom.explain import OllamaClient, explain_items
from headroom.history import HistoryStore, build_report, default_history_path
from headroom.render import format_report
from headroom.review import (
    ReviewError,
    archive_directory,
    collect_suggestions,
    format_move_stats,
    move_records,
    recycle_file,
)
from headroom.rules import state_dir

TOKEN_BYTES = 24


class DashboardError(Exception):
    """The dashboard was asked to listen somewhere other than this machine."""


def new_token() -> str:
    return secrets.token_urlsafe(TOKEN_BYTES)


def first_chart_minutes(interval_seconds: int) -> int:
    """How long the page says to wait before the first chart point."""
    seconds = max(1, int(interval_seconds))
    return max(1, (seconds + 59) // 60)


def collecting_message(minutes: int) -> str:
    unit = "minute" if minutes == 1 else "minutes"
    return f"Collecting data, first chart in {minutes} {unit}."


def bind_dashboard(
    host: str,
    port: int,
    token: str,
    *,
    directory: Path | None = None,
    history: Path | None = None,
    recycle=None,
    now=None,
    today: date | None = None,
    explainer=None,
    interval_seconds: int = 30,
    live_probe=None,
    widget_lite: bool = False,
    widget_default: bool = False,
    pet_start=None,
    pet_stop=None,
) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise DashboardError("the dashboard only binds to 127.0.0.1")
    folder = state_dir() if directory is None else directory
    history_path = default_history_path() if history is None else history
    minutes = first_chart_minutes(interval_seconds)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if not _authorized(self, token):
                _send(self, 403, "Forbidden. Open the URL printed by headroom dashboard.")
                return
            path = urlparse(self.path).path
            if path in {"", "/"}:
                _send(self, 200, _page(token, minutes), content_type="text/html; charset=utf-8")
                return
            if path == "/api/history":
                payload = _history_payload(
                    history_path,
                    directory=folder,
                    minutes=minutes,
                    probe=live_probe,
                )
                _send(self, 200, json.dumps(payload), content_type="application/json")
                return
            if path == "/api/report":
                _send(
                    self,
                    200,
                    _report_text(history_path, directory=folder, minutes=minutes, probe=live_probe),
                    content_type="text/plain; charset=utf-8",
                )
                return
            if path == "/api/review":
                _send(self, 200, json.dumps(_review_payload(folder, explainer, now, today)), content_type="application/json")
                return
            if path == "/api/widget":
                _send(self, 200, json.dumps(_widget_state(folder, widget_default)), content_type="application/json")
                return
            _send(self, 404, "Not found")

        def do_POST(self) -> None:  # noqa: N802
            if not _authorized(self, token):
                _send(self, 403, "Forbidden")
                return
            path = urlparse(self.path).path
            if path == "/api/widget":
                fields = _form(self)
                raw = fields.get("enabled") or fields.get("on")
                value = (raw[0] if raw else "").strip().casefold()
                if value in {"1", "true", "on", "yes"}:
                    from headroom.pet import set_pet_enabled

                    set_pet_enabled(True, folder)
                    (pet_start or _default_pet_start)(folder, widget_lite)
                elif value in {"0", "false", "off", "no"}:
                    from headroom.pet import set_pet_enabled

                    set_pet_enabled(False, folder)
                    (pet_stop or _default_pet_stop)(folder)
                else:
                    _send(self, 400, "enabled must be true or false", content_type="application/json")
                    return
                _send(self, 200, json.dumps(_widget_state(folder, widget_default)), content_type="application/json")
                return
            if path != "/move":
                _send(self, 404, "Not found")
                return
            fields = _form(self)
            posted = fields.get("token", [""])[0]
            if not secrets.compare_digest(posted, token):
                _send(self, 403, "Forbidden")
                return
            status, body = _move(fields, folder, recycle, now, today)
            _send(self, status, body, content_type="text/plain; charset=utf-8")

        def log_message(self, fmt: str, *args) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    return server


def serve_dashboard(host: str, port: int, token: str, *, opener=None, **kwargs) -> None:
    """Print and open the URL. A second launch opens the dashboard that is already up."""
    directory = kwargs.get("directory")
    folder = state_dir() if directory is None else directory
    existing = existing_dashboard_url(folder)
    if existing is not None:
        print("Dashboard is already running.", flush=True)
        print(existing, flush=True)
        _open_url(existing, opener)
        return
    try:
        server = bind_dashboard(host, port, token, **kwargs)
    except OSError as exc:
        existing = existing_dashboard_url(folder)
        if existing is not None:
            print("Dashboard is already running.", flush=True)
            print(existing, flush=True)
            _open_url(existing, opener)
            return
        raise DashboardError(f"port {port} is already in use") from exc
    bound_port = int(server.server_address[1])
    url = f"http://127.0.0.1:{bound_port}/?token={token}"
    remember_dashboard(folder, url)
    print(f"Headroom dashboard  {url}", flush=True)
    print("Local only. Leave this running and open that URL in a browser on this PC.", flush=True)
    _open_url(url, opener)
    try:
        server.serve_forever()
    finally:
        forget_dashboard(folder, url)
        server.server_close()


def dashboard_url_path(directory: Path) -> Path:
    return directory / "dashboard.url"


def remember_dashboard(directory: Path, url: str) -> None:
    path = dashboard_url_path(directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(url + "\n", encoding="utf-8")


def forget_dashboard(directory: Path, url: str) -> None:
    path = dashboard_url_path(directory)
    try:
        current = path.read_text(encoding="utf-8").strip()
    except OSError:
        return
    if current == url.strip():
        path.unlink(missing_ok=True)


def existing_dashboard_url(directory: Path) -> str | None:
    """The saved URL when that port is still accepting connections."""
    path = dashboard_url_path(directory)
    try:
        url = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    port = _port_of(url)
    if port is None or not _port_open(port):
        path.unlink(missing_ok=True)
        return None
    return url


def _port_of(url: str) -> int | None:
    parsed = urlparse(url)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        return None
    if parsed.port is None:
        return None
    return int(parsed.port)


def _port_open(port: int) -> bool:
    import socket

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def _open_url(url: str, opener) -> None:
    if opener is None:
        import webbrowser

        opener = webbrowser.open
    try:
        opener(url)
    except Exception:
        return


def _authorized(handler: BaseHTTPRequestHandler, token: str) -> bool:
    host = (handler.headers.get("Host") or "").split(":")[0].casefold()
    if host not in {"127.0.0.1", "localhost"}:
        return False
    header = handler.headers.get("X-Headroom-Token") or ""
    query = parse_qs(urlparse(handler.path).query)
    provided = header or (query.get("token") or [""])[0]
    return bool(provided) and secrets.compare_digest(provided, token)


def _form(handler: BaseHTTPRequestHandler) -> dict[str, list[str]]:
    length = int(handler.headers.get("Content-Length") or "0")
    raw = handler.rfile.read(length) if length else b""
    content_type = handler.headers.get("Content-Type") or ""
    if "application/json" in content_type:
        try:
            data = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            return {}
        if not isinstance(data, dict):
            return {}
        fields: dict[str, list[str]] = {}
        for key, value in data.items():
            if isinstance(value, list):
                fields[str(key)] = [str(item) for item in value]
            else:
                fields[str(key)] = [str(value)]
        return fields
    parsed = parse_qs(raw.decode("utf-8"), keep_blank_values=True)
    return {key: list(values) for key, values in parsed.items()}


def _move(fields, folder: Path, recycle, now, today: date | None) -> tuple[int, str]:
    confirm = (fields.get("confirm") or [""])[0]
    paths = fields.get("path") or []
    if confirm.strip() != "MOVE":
        return 400, "Type MOVE to move the selected files. Nothing was moved.\n"
    if not paths:
        return 400, "Nothing selected. Nothing was moved.\n"
    moment = _clock(now)
    clock_day = date.today() if today is None else today
    items, roots = collect_suggestions(folder, now=moment, today=clock_day)
    allowed = {item.record.path: item.record for item in items}
    selected = []
    for path in paths:
        record = allowed.get(path)
        if record is None:
            return 400, f"Refusing {path}. It is not on the review list. Nothing was moved.\n"
        selected.append(record)
    recycler = recycle
    if recycler is None:
        recycler = recycle_file if os.name == "nt" else (lambda _path: False)
    try:
        stats = move_records(
            selected,
            roots,
            archive_directory(folder),
            recycle=recycler,
            state=folder,
        )
    except ReviewError as exc:
        return 400, f"{exc}. Nothing was moved.\n"
    return 200, format_move_stats(stats)


def _history_payload(
    path: Path,
    *,
    directory: Path,
    minutes: int,
    probe=None,
) -> dict[str, object]:
    ram: list[dict[str, float]] = []
    disks: dict[str, list[dict[str, float]]] = {}
    if path.is_file():
        store = HistoryStore(path)
        try:
            rows = store.samples_between(0, 10**18)
            ram = [{"t": float(row["ts"]), "percent": float(row["ram_percent"])} for row in rows]
            for drive in store.drives():
                disks[drive] = [{"t": ts, "free": float(free)} for ts, free in store.disk_points(drive)]
        finally:
            store.close()
    live = read_live_snapshot(directory, probe=probe)
    collecting = len(ram) == 0
    return {
        "ram": _thin(ram, "percent"),
        "disks": {key: _thin(points, "free") for key, points in disks.items()},
        "live": live,
        "collecting": collecting,
        "first_chart_minutes": minutes,
        "message": collecting_message(minutes) if collecting else "",
    }


def read_live_snapshot(directory: Path, *, probe=None, max_age: float = 300.0) -> dict[str, object] | None:
    """Current RAM and disks from the watch snapshot, or one direct sample."""
    import time

    from headroom.pet import read_watch_sample

    snap = read_watch_sample(directory, now=time.time(), max_age=max_age)
    if snap is not None:
        return {
            "ram_percent": snap.ram_percent,
            "ram_total": snap.ram_total,
            "disks": [
                {"key": key, "free": free, "total": total} for key, free, total in snap.disks
            ],
            "source": "watch",
            "captured_at": snap.captured_at,
        }
    reader = probe
    if reader is None:
        from headroom.probe import sample as reader
    try:
        current = reader()
    except Exception:
        return None
    return {
        "ram_percent": float(current.memory.ram_percent),
        "ram_total": int(current.memory.ram_total),
        "disks": [
            {"key": disk.key, "free": int(disk.free), "total": int(disk.total)} for disk in current.disks
        ],
        "source": "probe",
        "captured_at": float(current.captured_at),
    }


def format_live(snapshot: dict[str, object] | None) -> str:
    from headroom.formatutil import format_bytes

    if not snapshot:
        return "No live reading yet."
    parts: list[str] = []
    ram = snapshot.get("ram_percent")
    if isinstance(ram, (int, float)):
        parts.append(f"RAM {float(ram):.1f}%")
    disks = snapshot.get("disks")
    if isinstance(disks, list):
        for disk in disks:
            if not isinstance(disk, dict):
                continue
            parts.append(f"{disk.get('key')} {format_bytes(int(disk.get('free') or 0))} free")
    body = " · ".join(parts) if parts else "No live reading yet."
    source = "the watch snapshot" if snapshot.get("source") == "watch" else "a direct reading"
    return f"Live {body} ({source})."


def _report_text(path: Path, *, directory: Path, minutes: int, probe=None) -> str:
    live = read_live_snapshot(directory, probe=probe)
    if not path.is_file():
        return _empty_report(live, minutes)
    from headroom.config import Config

    store = HistoryStore(path)
    try:
        import time

        if store.count_samples() == 0:
            return _empty_report(live, minutes)
        text = format_report(build_report(store, Config(), now=time.time()))
    finally:
        store.close()
    extra = format_live(live)
    if extra and not text.endswith("\n"):
        text += "\n"
    return text + extra + "\n"


def _empty_report(live: dict[str, object] | None, minutes: int) -> str:
    lines = [
        collecting_message(minutes),
        format_live(live),
        "No chart yet. The first sample is written within about 10 seconds of headroom watch starting,",
        "then on the normal interval.",
    ]
    return "\n".join(lines) + "\n"


def _review_payload(folder: Path, explainer, now, today: date | None) -> dict[str, object]:
    moment = _clock(now)
    clock_day = date.today() if today is None else today
    items, _roots = collect_suggestions(folder, now=moment, today=clock_day)
    reasons = explain_items(items, now=moment, client=explainer if explainer is not None else _quiet_client())
    rows = []
    for item, reason in zip(items, reasons):
        rows.append(
            {
                "path": item.record.path,
                "size": item.record.size,
                "category": item.record.category,
                "rule": item.rule_text,
                "why": reason,
            }
        )
    return {"items": rows}


def _quiet_client() -> OllamaClient:
    def opener(request, timeout):  # type: ignore[no-untyped-def]
        raise OSError("ollama is not used unless a client is passed")

    return OllamaClient(opener=opener, timeout=0.2)


def _widget_state(folder: Path, widget_default: bool) -> dict[str, object]:
    """The Roomy toggle as the page sees it: on/off plus whether it is running."""
    from headroom.pet import pet_enabled, pet_running

    enabled = pet_enabled(folder)
    running = pet_running(folder)
    if enabled is None:
        enabled = running or bool(widget_default)
    return {"on": bool(enabled), "running": bool(running)}


def _default_pet_start(folder: Path, lite: bool) -> None:
    from headroom.pet import launch_pet_detached

    launch_pet_detached(lite=lite, directory=folder)


def _default_pet_stop(folder: Path) -> None:
    from headroom.pet import send_control

    send_control("quit", folder)


def _page(token: str, minutes: int = 1) -> str:
    safe = html.escape(token, quote=True)
    waiting = html.escape(collecting_message(minutes))
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Headroom</title>
<style>
  :root {{
    color-scheme: dark;
    --bg: #1f1f1f;
    --panel: #282828;
    --border: #3a3a3a;
    --text: #f2f2f2;
    --muted: #9a9a9a;
    --accent: #4cc2ff;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font: 14px/1.5 "Segoe UI", system-ui, sans-serif; background: var(--bg); color: var(--text); }}
  main {{ max-width: 1040px; margin: 0 auto; padding: 20px 24px 48px; }}
  h1 {{ font-size: 1.25rem; font-weight: 600; margin: 0 0 2px; }}
  h2 {{ font-size: 0.9rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted); margin: 0 0 10px; }}
  .subtitle {{ color: var(--muted); margin: 0 0 22px; }}
  section {{ border-top: 1px solid var(--border); padding: 18px 0; }}
  .note {{ color: var(--muted); margin: 4px 0 8px; }}
  .live {{ font-size: 1.05rem; margin: 0; }}
  canvas {{ display: block; width: 100%; height: 180px; background: var(--panel); }}
  pre {{ white-space: pre-wrap; background: var(--panel); border: 1px solid var(--border); padding: 12px; margin: 0; min-height: 4.5em; font: 13px/1.5 "Consolas", monospace; }}
  article {{ border: 1px solid var(--border); padding: 10px 12px; margin-bottom: 8px; }}
  article p {{ margin: 4px 0 0; color: var(--muted); }}
  label {{ display: block; margin-top: 8px; }}
  input[type="text"] {{ margin-top: 4px; background: var(--panel); color: var(--text); border: 1px solid var(--border); padding: 6px 8px; }}
  button {{ margin-top: 10px; background: var(--panel); color: var(--text); border: 1px solid var(--border); padding: 6px 14px; cursor: pointer; }}
  button:hover {{ border-color: var(--accent); }}
  button:disabled {{ opacity: 0.5; cursor: default; }}
  .switch {{ display: flex; align-items: center; gap: 10px; }}
  .switch input {{ width: 16px; height: 16px; margin: 0; }}
  .switch span {{ color: var(--text); }}
</style>
</head>
<body>
<main>
  <h1>Headroom</h1>
  <p class="subtitle">This page stays on this computer. Approving a review moves files to the Recycle Bin when Windows accepts them, otherwise into the archive. Nothing here permanently deletes; cache cleanup still uses Type YES in a terminal.</p>
  <section>
    <h2>Roomy</h2>
    <label class="switch">
      <input type="checkbox" id="roomy-toggle">
      <span id="roomy-label">Checking Roomy...</span>
    </label>
  </section>
  <section>
    <h2>Now</h2>
    <p id="live" class="live">Loading the live reading...</p>
  </section>
  <section>
    <h2>RAM</h2>
    <p id="ram-note" class="note">{waiting}</p>
    <canvas id="ram"></canvas>
  </section>
  <section>
    <h2>Free space</h2>
    <p id="disk-note" class="note">{waiting}</p>
    <canvas id="disk"></canvas>
  </section>
  <section>
    <h2>Latest report</h2>
    <pre id="report">{waiting}</pre>
  </section>
  <section>
    <h2>Review</h2>
    <form method="post" action="/move">
      <input type="hidden" name="token" value="{safe}">
      <div id="review">Loading review...</div>
      <p id="move-note" class="note">Type MOVE to move the checked files. The button stays off until a file is listed.</p>
      <label>Type MOVE to move the checked files
        <input id="confirm" name="confirm" autocomplete="off">
      </label>
      <button id="move" type="submit" disabled>Move checked files</button>
    </form>
  </section>
</main>
<script>
const token = new URLSearchParams(location.search).get("token") || "";
const waiting = "{waiting}";
function formatBytes(n) {{
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = Number(n) || 0;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) {{
    value /= 1024;
    index += 1;
  }}
  return (index === 0 ? String(Math.round(value)) : value.toFixed(1)) + " " + units[index];
}}
function liveLine(live) {{
  if (!live) return "No live reading yet.";
  const parts = [];
  if (typeof live.ram_percent === "number") parts.push("RAM " + live.ram_percent.toFixed(1) + "%");
  (live.disks || []).forEach(disk => {{
    parts.push(disk.key + " " + formatBytes(disk.free) + " free");
  }});
  const source = live.source === "watch" ? "the watch snapshot" : "a direct reading";
  return "Live " + (parts.length ? parts.join(" · ") : "No live reading yet.") + " (" + source + ").";
}}
function draw(canvas, values, emptyText) {{
  const dpr = window.devicePixelRatio || 1;
  const rect = canvas.getBoundingClientRect();
  const cssW = Math.max(1, rect.width || 900);
  const cssH = 180;
  const w = Math.round(cssW * dpr);
  const h = Math.round(cssH * dpr);
  if (canvas.width !== w || canvas.height !== h) {{
    canvas.width = w;
    canvas.height = h;
  }}
  const ctx = canvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, cssW, cssH);
  ctx.font = "13px 'Segoe UI', sans-serif";
  ctx.strokeStyle = "#4cc2ff";
  ctx.lineWidth = 1.5;
  if (!values.length) {{
    ctx.fillStyle = "#9a9a9a";
    ctx.fillText(emptyText || waiting, 12, 28);
    return;
  }}
  let min = Math.min.apply(null, values);
  let max = Math.max.apply(null, values);
  if (min === max) {{ max = min + 1; }}
  ctx.beginPath();
  values.forEach((value, index) => {{
    const x = values.length === 1 ? 0 : (index / (values.length - 1)) * (cssW - 16) + 8;
    const y = cssH - 14 - ((value - min) / (max - min)) * (cssH - 28);
    if (index === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
  }});
  ctx.stroke();
}}
function setMove(enabled, boxText, noteText) {{
  const button = document.getElementById("move");
  const note = document.getElementById("move-note");
  button.disabled = !enabled;
  button.textContent = enabled ? "Move checked files" : "Nothing to move yet";
  note.textContent = noteText;
  if (boxText !== null) document.getElementById("review").textContent = boxText;
}}
async function load() {{
  const headers = {{"X-Headroom-Token": token}};
  try {{
    const response = await fetch("/api/history?token=" + encodeURIComponent(token), {{headers}});
    if (!response.ok) throw new Error("history");
    const history = await response.json();
    const message = history.collecting ? (history.message || waiting) : "Recorded samples.";
    document.getElementById("live").textContent = liveLine(history.live);
    document.getElementById("ram-note").textContent = message;
    const drives = history.disks || {{}};
    const first = Object.keys(drives)[0];
    document.getElementById("disk-note").textContent = history.collecting
      ? message
      : (first ? first + " free space from recorded samples." : "No drives recorded yet.");
    draw(document.getElementById("ram"), (history.ram || []).map(p => p.percent), message);
    draw(document.getElementById("disk"), first ? drives[first].map(p => p.free) : [], message);
  }} catch (err) {{
    document.getElementById("live").textContent = "Could not load the chart.";
    document.getElementById("ram-note").textContent = "Could not load the chart.";
    document.getElementById("disk-note").textContent = "Could not load the chart.";
    draw(document.getElementById("ram"), [], "Could not load the chart.");
    draw(document.getElementById("disk"), [], "Could not load the chart.");
  }}
  try {{
    const response = await fetch("/api/report?token=" + encodeURIComponent(token), {{headers}});
    if (!response.ok) throw new Error("report");
    document.getElementById("report").textContent = await response.text();
  }} catch (err) {{
    document.getElementById("report").textContent = "Could not load the report.";
  }}
  try {{
    const response = await fetch("/api/review?token=" + encodeURIComponent(token), {{headers}});
    if (!response.ok) throw new Error("review");
    const review = await response.json();
    const box = document.getElementById("review");
    const items = review.items || [];
    if (!items.length) {{
      setMove(
        false,
        "Nothing to move yet. Run headroom analyze, or headroom dupes, first.",
        "Type MOVE to move the checked files. Nothing is listed, so the button stays off."
      );
      return;
    }}
    box.innerHTML = items.map(item => {{
      const path = item.path.replace(/[&<>"]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}}[ch]));
      return "<article><label><input type=\\"checkbox\\" name=\\"path\\" value=\\"" + path + "\\"> " + path + "</label><p>" +
        (item.why || "").replace(/[&<>]/g, ch => ({{"&":"&amp;","<":"&lt;",">":"&gt;"}}[ch])) + "</p></article>";
    }}).join("");
    setMove(true, null, "Type MOVE to move the checked files, then press the button.");
  }} catch (err) {{
    setMove(
      false,
      "Could not load the review list.",
      "Type MOVE to move the checked files. The button stays off until the list loads."
    );
  }}
}}
const roomyToggle = document.getElementById("roomy-toggle");
const roomyLabel = document.getElementById("roomy-label");
function setRoomy(on, running) {{
  roomyToggle.checked = !!on;
  roomyLabel.textContent = on ? (running ? "Roomy is running" : "Roomy is on") : "Roomy is off";
}}
async function loadRoomy() {{
  const headers = {{"X-Headroom-Token": token}};
  try {{
    const r = await fetch("/api/widget?token=" + encodeURIComponent(token), {{headers}});
    if (!r.ok) throw new Error("widget");
    const w = await r.json();
    setRoomy(w.on, w.running);
  }} catch (err) {{
    roomyLabel.textContent = "Roomy state unavailable";
    roomyToggle.disabled = true;
  }}
}}
roomyToggle.addEventListener("change", async () => {{
  const enabled = roomyToggle.checked;
  roomyLabel.textContent = enabled ? "Starting Roomy..." : "Stopping Roomy...";
  roomyToggle.disabled = true;
  try {{
    const r = await fetch("/api/widget?token=" + encodeURIComponent(token), {{
      method: "POST",
      headers: {{"X-Headroom-Token": token, "Content-Type": "application/json"}},
      body: JSON.stringify({{enabled: enabled}}),
    }});
    if (!r.ok) throw new Error("widget");
    const w = await r.json();
    setRoomy(w.on, w.running);
  }} catch (err) {{
    roomyLabel.textContent = "Could not change Roomy";
  }} finally {{
    roomyToggle.disabled = false;
  }}
}});
load();
loadRoomy();
</script>
</body>
</html>
"""


def _thin(points: list[dict], key: str, limit: int = 240) -> list[dict]:
    if len(points) <= limit:
        return points
    step = len(points) / limit
    return [points[int(index * step)] for index in range(limit)]


def _send(handler: BaseHTTPRequestHandler, status: int, body: str, *, content_type: str = "text/plain; charset=utf-8") -> None:
    data = body.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("X-Content-Type-Options", "nosniff")
    handler.end_headers()
    handler.wfile.write(data)


def _clock(now) -> float:
    import time

    return time.time() if now is None else float(now)
