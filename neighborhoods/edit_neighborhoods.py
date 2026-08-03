"""
edit_neighborhoods.py — draw and edit neighborhood boundaries in a browser.

The shapes in neighborhood_shapes.py decide which neighborhood each listing is
filed under, everywhere in the app. Hand-editing coordinate lists is miserable,
so this serves a small map editor instead:

    python neighborhoods/edit_neighborhoods.py

That opens http://127.0.0.1:8765 with every current shape loaded and editable.
Drag vertices, draw new polygons, rename, delete — then hit Save, which rewrites
neighborhood_shapes.py in place (keeping a .bak). Nothing is written until you
press Save, so it is safe to open and poke around.

Proposed shapes in draft_shapes.geojson load alongside the live ones in a dashed
grey outline. They are NOT live: a draft only becomes real if you approve it and
save. That is the review step — look at the boundary on the map first, adjust it
if it is wrong, and discard it if you don't want it at all.

Sharing shapes with someone else:
    Export GeoJSON  → hands you a file (or clipboard blob) to send.
    Import GeoJSON  → paste what they send back; it lands as drafts to review.

Other modes:
    --export FILE   write the standalone HTML and exit (no server, no saving)
    --port N        serve on a different port
    --no-browser    don't auto-open a browser tab
"""

from __future__ import annotations

import argparse
import http.server
import json
import shutil
import socketserver
import sys
import threading
import webbrowser
from datetime import datetime
from pathlib import Path

HERE          = Path(__file__).parent.resolve()
SHAPES_MODULE = HERE / "neighborhood_shapes.py"
DRAFTS_FILE   = HERE / "draft_shapes.geojson"

# Same palette the dashboard uses, so a shape looks here like it looks there.
_PALETTE = [
    "#0077BB", "#CC3311", "#009988", "#EE7733", "#AA3377",
    "#33BBEE", "#228833", "#EE3377", "#CCBB44",
]
_CATCHALL       = "Way Out There"
_CATCHALL_COLOR = "#AAAAAA"


# ── Reading current state ─────────────────────────────────────────────────────

def load_live_shapes() -> dict[str, list[list[float]]]:
    """Current shapes as {name: [[lon, lat], ...]}, straight from the module."""
    sys.path.insert(0, str(HERE.parent))
    from neighborhoods.neighborhood_shapes import neighborhood_shapes

    return {
        name: [[float(x), float(y)] for x, y in poly.exterior.coords]
        for name, poly in neighborhood_shapes.items()
    }


def load_draft_shapes() -> list[dict]:
    """Proposed shapes awaiting review, or [] if there are none."""
    if not DRAFTS_FILE.exists():
        return []
    try:
        raw = json.loads(DRAFTS_FILE.read_text())
    except ValueError as e:
        print(f"warning: {DRAFTS_FILE.name} is not valid JSON ({e}) — ignoring it")
        return []

    drafts = []
    for feature in raw.get("features", []):
        geom = feature.get("geometry") or {}
        if geom.get("type") != "Polygon" or not geom.get("coordinates"):
            continue
        props = feature.get("properties") or {}
        drafts.append({
            "name":   str(props.get("name", "Unnamed draft")),
            "note":   str(props.get("note", "")),
            "coords": [[float(x), float(y)] for x, y in geom["coordinates"][0]],
        })
    return drafts


def _color_for(name: str, index: int) -> str:
    return _CATCHALL_COLOR if name == _CATCHALL else _PALETTE[index % len(_PALETTE)]


# ── Writing back ──────────────────────────────────────────────────────────────

_MODULE_HEADER = '''\
from shapely.geometry import Polygon, mapping

# Edited with: python neighborhoods/edit_neighborhoods.py
# Last saved: {stamp}

neighborhood_shapes = {{
'''

_MODULE_FOOTER = '''}


if __name__ == '__main__':
    # Run directly to generate a reference map: python neighborhood_shapes.py
    import folium
    m = folium.Map(location=[37.76, -122.44], zoom_start=13)
    for name, polygon in neighborhood_shapes.items():
        folium.GeoJson(
            mapping(polygon),
            name=name,
            tooltip=name,
            popup=folium.Popup(name)
        ).add_to(m)
    out = "sf_neighborhoods_map.html"
    m.save(out)
    print(f"Saved: {out}")
'''


def render_module(shapes: dict[str, list[list[float]]]) -> str:
    """Rebuild neighborhood_shapes.py from a {name: ring} mapping.

    The file is pure data plus a __main__ block, so regenerating it wholesale is
    simpler and less fragile than trying to patch coordinate lists in place.
    """
    out = [_MODULE_HEADER.format(stamp=datetime.now().strftime("%Y-%m-%d %H:%M"))]

    for name, ring in shapes.items():
        # Close the ring if the editor handed back an open one — shapely accepts
        # both, but every existing entry in this file is explicitly closed.
        ring = list(ring)
        if ring and ring[0] != ring[-1]:
            ring = ring + [ring[0]]

        out.append(f'    "{name}": Polygon([\n')
        for i, (lon, lat) in enumerate(ring):
            comma = "" if i == len(ring) - 1 else ","
            out.append(f"        [{lon}, {lat}]{comma}\n")
        out.append("    ]),\n\n")

    out.append(_MODULE_FOOTER)
    return "".join(out)


def save_shapes(shapes: dict[str, list[list[float]]]) -> str:
    """Validate, back up, and rewrite the shapes module. Returns a status line."""
    if not shapes:
        raise ValueError("Refusing to save an empty shape set.")

    for name, ring in shapes.items():
        if len(ring) < 3:
            raise ValueError(f"{name!r} has only {len(ring)} point(s); a polygon needs 3+.")
        for lon, lat in ring:
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError(
                    f"{name!r} has an out-of-range point [{lon}, {lat}]. "
                    f"Coordinates are [longitude, latitude] — longitude first."
                )

    rendered = render_module(shapes)

    # Parse what we're about to write before touching the real file, so a bug in
    # here can never leave the app with an unimportable shapes module.
    try:
        compile(rendered, str(SHAPES_MODULE), "exec")
    except SyntaxError as e:
        raise ValueError(f"Generated module is invalid Python ({e}) — nothing written.")

    if SHAPES_MODULE.exists():
        shutil.copy2(SHAPES_MODULE, SHAPES_MODULE.with_suffix(".py.bak"))
    SHAPES_MODULE.write_text(rendered, encoding="utf-8")

    return f"Saved {len(shapes)} shape(s) to {SHAPES_MODULE.name} (backup: neighborhood_shapes.py.bak)"


# ── The page ──────────────────────────────────────────────────────────────────

_PAGE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Neighborhood Editor</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<link rel="stylesheet" href="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-draw@1.0.4/dist/leaflet.draw.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    display: flex; height: 100vh; overflow: hidden; color: #1a1a2e;
  }
  #side {
    width: 320px; flex: none; background: #f7f8fa; border-right: 1px solid #e2e5ea;
    display: flex; flex-direction: column;
  }
  #side header { padding: 16px 16px 12px; border-bottom: 1px solid #e2e5ea; }
  #side header h1 { font-size: 1rem; font-weight: 700; letter-spacing: -0.01em; }
  #side header p { font-size: 0.75rem; color: #6b7280; margin-top: 4px; line-height: 1.45; }
  #list { flex: 1; overflow-y: auto; padding: 8px; }
  .row {
    background: #fff; border: 1px solid #e2e5ea; border-radius: 8px;
    padding: 8px 10px; margin-bottom: 6px; display: flex; align-items: center; gap: 8px;
  }
  .row.draft { border-style: dashed; border-color: #b9bec7; background: #fcfcfd; }
  .row .swatch { width: 12px; height: 12px; border-radius: 3px; flex: none; }
  .row .name {
    flex: 1; font-size: 0.82rem; font-weight: 600; border: none; background: transparent;
    color: #1a1a2e; min-width: 0; padding: 2px 3px; border-radius: 4px;
  }
  .row .name:focus { outline: 2px solid #0077BB44; background: #fff; }
  .row.draft .name { font-weight: 500; color: #4b5563; }
  .badge {
    font-size: 0.6rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
    color: #92400e; background: #fef3c7; padding: 2px 5px; border-radius: 4px; flex: none;
  }
  .row button {
    border: none; background: transparent; cursor: pointer; font-size: 0.75rem;
    color: #6b7280; padding: 3px 5px; border-radius: 4px; flex: none;
  }
  .row button:hover { background: #eef0f3; color: #1a1a2e; }
  .row button.approve { color: #047857; font-weight: 700; }
  .note { font-size: 0.7rem; color: #9ca3af; line-height: 1.4; padding: 0 10px 8px; margin-top: -4px; }
  #actions { padding: 12px; border-top: 1px solid #e2e5ea; display: grid; gap: 6px; }
  #actions button {
    padding: 8px 10px; border-radius: 7px; border: 1px solid #d1d5db; background: #fff;
    font-size: 0.8rem; font-weight: 600; cursor: pointer; color: #1a1a2e;
  }
  #actions button:hover { background: #f0f2f5; }
  #actions button.primary { background: #0077BB; border-color: #0077BB; color: #fff; }
  #actions button.primary:hover { background: #01639d; }
  #actions button.primary:disabled { background: #9ca3af; border-color: #9ca3af; cursor: default; }
  .btn-pair { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  #status { font-size: 0.72rem; color: #6b7280; min-height: 1.1em; line-height: 1.4; }
  #status.ok  { color: #047857; }
  #status.err { color: #b91c1c; }
  #map { flex: 1; }
  .leaflet-tooltip.hood-label {
    background: transparent; border: none; box-shadow: none;
    font-size: 11px; font-weight: 700; text-shadow: 0 0 3px #fff, 0 0 3px #fff, 0 0 3px #fff;
  }
  dialog {
    border: none; border-radius: 12px; padding: 18px; width: min(520px, 90vw);
    box-shadow: 0 10px 40px rgba(0,0,0,.2);
  }
  dialog::backdrop { background: rgba(0,0,0,.35); }
  dialog h2 { font-size: 0.95rem; margin-bottom: 8px; }
  dialog p { font-size: 0.78rem; color: #6b7280; margin-bottom: 10px; line-height: 1.5; }
  dialog textarea {
    width: 100%; height: 200px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 0.72rem; border: 1px solid #d1d5db; border-radius: 8px; padding: 8px; resize: vertical;
  }
  dialog .dlg-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 10px; }
  dialog .dlg-actions button {
    padding: 7px 14px; border-radius: 7px; border: 1px solid #d1d5db;
    background: #fff; font-size: 0.8rem; font-weight: 600; cursor: pointer;
  }
  dialog .dlg-actions button.primary { background: #0077BB; border-color: #0077BB; color: #fff; }
</style>
</head>
<body>

<div id="side">
  <header>
    <h1>Neighborhood Editor</h1>
    <p>Click a shape to select it, then drag its vertices. Use the toolbar on the
       map to draw a new one. Dashed rows are proposals &mdash; approve or discard
       them. Nothing changes on disk until you press Save.</p>
  </header>
  <div id="list"></div>
  <div id="actions">
    <div class="btn-pair">
      <button id="btn-export">Export GeoJSON</button>
      <button id="btn-import">Import GeoJSON</button>
    </div>
    <button id="btn-revert">Discard my changes</button>
    <button id="btn-save" class="primary">Save to neighborhood_shapes.py</button>
    <div id="status"></div>
  </div>
</div>
<div id="map"></div>

<dialog id="dlg">
  <h2 id="dlg-title"></h2>
  <p id="dlg-help"></p>
  <textarea id="dlg-text" spellcheck="false"></textarea>
  <div class="dlg-actions">
    <button id="dlg-cancel">Close</button>
    <button id="dlg-ok" class="primary">Import</button>
  </div>
</dialog>

<script>
const BOOT      = __BOOT__;
const READ_ONLY = __READ_ONLY__;

const map = L.map('map', { scrollWheelZoom: true }).setView([37.762, -122.437], 13);
L.tileLayer('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png', {
  attribution: '&copy; OpenStreetMap, &copy; CARTO', maxZoom: 19,
}).addTo(map);

const drawn = new L.FeatureGroup().addTo(map);

map.addControl(new L.Control.Draw({
  edit: { featureGroup: drawn, remove: false },
  draw: {
    polygon: { allowIntersection: false, showArea: false,
               shapeOptions: { color: '#0077BB', weight: 2, fillOpacity: 0.22 } },
    polyline: false, rectangle: false, circle: false, marker: false, circlemarker: false,
  },
}));

// ── Model ───────────────────────────────────────────────────────────────────
// One entry per shape. `draft` means "proposed, not yet live"; it only becomes
// live when approved, which is the review gate.
let shapes = [];

function ringFromLayer(layer) {
  // Leaflet gives [lat, lng]; everything downstream wants [lon, lat].
  return layer.getLatLngs()[0].map(p => [p.lng, p.lat]);
}

function layerFromRing(ring, color, isDraft) {
  const latlngs = ring.map(([lon, lat]) => [lat, lon]);
  return L.polygon(latlngs, {
    color: isDraft ? '#6b7280' : color,
    weight: isDraft ? 2 : 2,
    dashArray: isDraft ? '7 5' : null,
    fillColor: color,
    fillOpacity: isDraft ? 0.10 : 0.22,
    opacity: 0.85,
  });
}

function addShape(spec) {
  const layer = layerFromRing(spec.coords, spec.color, spec.draft);
  layer.addTo(drawn);
  const entry = { ...spec, layer };
  layer.bindTooltip(spec.name, {
    permanent: true, direction: 'center', className: 'hood-label',
    opacity: 1,
  });
  layer.on('click', () => {
    map.fitBounds(layer.getBounds(), { padding: [40, 40] });
    const row = document.querySelector(`[data-id="${entry.id}"]`);
    if (row) { row.scrollIntoView({ block: 'nearest' }); row.querySelector('.name').focus(); }
  });
  shapes.push(entry);
  return entry;
}

let nextId = 1;
function boot() {
  drawn.clearLayers();
  shapes = [];
  nextId = 1;
  BOOT.live.forEach(s   => addShape({ ...s, id: nextId++, draft: false }));
  BOOT.drafts.forEach(s => addShape({ ...s, id: nextId++, draft: true  }));
  render();
}

// ── Sidebar ─────────────────────────────────────────────────────────────────
const listEl   = document.getElementById('list');
const statusEl = document.getElementById('status');

function setStatus(msg, kind) {
  statusEl.textContent = msg || '';
  statusEl.className = kind || '';
}

function render() {
  listEl.innerHTML = '';
  shapes.forEach(s => {
    const row = document.createElement('div');
    row.className = 'row' + (s.draft ? ' draft' : '');
    row.dataset.id = s.id;

    const sw = document.createElement('div');
    sw.className = 'swatch';
    sw.style.background = s.color;
    row.appendChild(sw);

    const name = document.createElement('input');
    name.className = 'name';
    name.value = s.name;
    name.addEventListener('input', () => {
      s.name = name.value;
      s.layer.setTooltipContent(name.value);
    });
    row.appendChild(name);

    if (s.draft) {
      const badge = document.createElement('span');
      badge.className = 'badge';
      badge.textContent = 'draft';
      row.appendChild(badge);

      const ok = document.createElement('button');
      ok.className = 'approve';
      ok.textContent = '✓';
      ok.title = 'Approve — make this a real neighborhood on save';
      ok.addEventListener('click', () => {
        s.draft = false;
        s.layer.setStyle({ color: s.color, dashArray: null, fillOpacity: 0.22 });
        render();
        setStatus(`Approved "${s.name}" — press Save to apply.`, '');
      });
      row.appendChild(ok);
    }

    const zoom = document.createElement('button');
    zoom.textContent = '⤢';
    zoom.title = 'Zoom to this shape';
    zoom.addEventListener('click', () =>
      map.fitBounds(s.layer.getBounds(), { padding: [40, 40] }));
    row.appendChild(zoom);

    const del = document.createElement('button');
    del.textContent = '✕';
    del.title = s.draft ? 'Discard this proposal' : 'Delete this neighborhood';
    del.addEventListener('click', () => {
      if (!s.draft && !confirm(
            `Delete "${s.name}"?\\n\\nListings already filed under it keep the ` +
            `name in their CSV row, but nothing new will match it.`)) return;
      drawn.removeLayer(s.layer);
      shapes = shapes.filter(x => x.id !== s.id);
      render();
    });
    row.appendChild(del);

    listEl.appendChild(row);

    if (s.note) {
      const note = document.createElement('div');
      note.className = 'note';
      note.textContent = s.note;
      listEl.appendChild(note);
    }
  });
}

// ── Draw / edit events ──────────────────────────────────────────────────────
map.on(L.Draw.Event.CREATED, e => {
  const ring  = ringFromLayer(e.layer);
  const color = BOOT.palette[shapes.length % BOOT.palette.length];
  const entry = addShape({
    id: nextId++, name: 'New neighborhood', coords: ring, color, draft: false, note: '',
  });
  render();
  const row = document.querySelector(`[data-id="${entry.id}"] .name`);
  if (row) { row.focus(); row.select(); }
  setStatus('Drew a new shape — give it a name, then Save.', '');
});

map.on(L.Draw.Event.EDITED, () => setStatus('Edited — press Save to apply.', ''));

// ── Save / export / import ──────────────────────────────────────────────────
function currentPayload() {
  // Draft shapes are deliberately excluded: an unapproved proposal must never
  // reach the live module just because someone hit Save.
  const out = {};
  for (const s of shapes) {
    if (s.draft) continue;
    const name = s.name.trim();
    if (!name) throw new Error('Every neighborhood needs a name.');
    if (name in out) throw new Error(`Two shapes are both named "${name}".`);
    out[name] = ringFromLayer(s.layer);
  }
  return out;
}

document.getElementById('btn-save').addEventListener('click', async () => {
  let payload;
  try {
    payload = currentPayload();
  } catch (err) {
    setStatus(err.message, 'err');
    return;
  }
  if (READ_ONLY) {
    setStatus('This is a static export — use Export GeoJSON instead.', 'err');
    return;
  }
  setStatus('Saving…', '');
  try {
    const res  = await fetch('/save', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ shapes: payload }),
    });
    const body = await res.json();
    if (!res.ok) throw new Error(body.error || res.statusText);
    setStatus(body.message, 'ok');
    // Anything left dashed is still a proposal; keep it that way on disk so the
    // review state survives a reload.
    BOOT.live = Object.entries(payload).map(([name, coords], i) => ({
      name, coords, color: BOOT.palette[i % BOOT.palette.length], note: '',
    }));
    BOOT.drafts = shapes.filter(s => s.draft)
      .map(s => ({ name: s.name, coords: ringFromLayer(s.layer), color: s.color, note: s.note }));
  } catch (err) {
    setStatus('Save failed: ' + err.message, 'err');
  }
});

document.getElementById('btn-revert').addEventListener('click', () => {
  if (!confirm('Discard every change since this page loaded?')) return;
  boot();
  setStatus('Reverted to the last saved shapes.', '');
});

const dlg = document.getElementById('dlg');
document.getElementById('dlg-cancel').addEventListener('click', () => dlg.close());

document.getElementById('btn-export').addEventListener('click', () => {
  const fc = {
    type: 'FeatureCollection',
    features: shapes.map(s => ({
      type: 'Feature',
      properties: { name: s.name, note: s.note || '' },
      geometry: { type: 'Polygon', coordinates: [ringFromLayer(s.layer)] },
    })),
  };
  const text = JSON.stringify(fc, null, 2);
  document.getElementById('dlg-title').textContent = 'Export GeoJSON';
  document.getElementById('dlg-help').textContent =
    'Send this to whoever is sharing neighborhoods with you, or drop it into ' +
    'geojson.io. They can paste it straight back into Import.';
  document.getElementById('dlg-text').value = text;
  document.getElementById('dlg-ok').textContent = 'Copy';
  document.getElementById('dlg-ok').onclick = async () => {
    try { await navigator.clipboard.writeText(text); setStatus('Copied to clipboard.', 'ok'); }
    catch { setStatus('Copy failed — select the text and copy manually.', 'err'); }
    dlg.close();
  };
  dlg.showModal();
});

document.getElementById('btn-import').addEventListener('click', () => {
  document.getElementById('dlg-title').textContent = 'Import GeoJSON';
  document.getElementById('dlg-help').textContent =
    'Paste a FeatureCollection of polygons — from geojson.io, or from someone ' +
    'who exported theirs. They come in as drafts so you can review each one ' +
    'before it goes live.';
  document.getElementById('dlg-text').value = '';
  document.getElementById('dlg-ok').textContent = 'Import';
  document.getElementById('dlg-ok').onclick = () => {
    let fc;
    try {
      fc = JSON.parse(document.getElementById('dlg-text').value);
    } catch (err) {
      setStatus('That is not valid JSON.', 'err');
      return;
    }
    const feats = (fc.features || []).filter(
      f => f.geometry && f.geometry.type === 'Polygon' && f.geometry.coordinates);
    if (!feats.length) {
      setStatus('No polygons found in that GeoJSON.', 'err');
      return;
    }
    feats.forEach((f, i) => {
      const props = f.properties || {};
      addShape({
        id: nextId++,
        name: props.name || props.Name || `Imported ${i + 1}`,
        note: props.note || '',
        coords: f.geometry.coordinates[0].map(([lon, lat]) => [lon, lat]),
        color: BOOT.palette[shapes.length % BOOT.palette.length],
        draft: true,
      });
    });
    render();
    dlg.close();
    setStatus(`Imported ${feats.length} shape(s) as drafts — review, then approve.`, 'ok');
  };
  dlg.showModal();
});

boot();
if (BOOT.drafts.length) {
  setStatus(`${BOOT.drafts.length} proposal(s) awaiting review.`, '');
}
</script>
</body>
</html>
"""


def build_page(read_only: bool = False) -> str:
    live = load_live_shapes()
    boot = {
        "palette": _PALETTE,
        "live": [
            {"name": name, "coords": ring, "color": _color_for(name, i), "note": ""}
            for i, (name, ring) in enumerate(live.items())
        ],
        "drafts": [
            {"name": d["name"], "coords": d["coords"], "note": d["note"],
             "color": _PALETTE[(len(live) + i) % len(_PALETTE)]}
            for i, d in enumerate(load_draft_shapes())
        ],
    }
    return (
        _PAGE
        .replace("__BOOT__", json.dumps(boot))
        .replace("__READ_ONLY__", "true" if read_only else "false")
    )


# ── Server ────────────────────────────────────────────────────────────────────

class _Handler(http.server.BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] not in ("/", "/index.html"):
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, build_page().encode("utf-8"), "text/html; charset=utf-8")

    def do_POST(self):
        if self.path != "/save":
            self._send(404, b'{"error":"not found"}', "application/json")
            return
        try:
            length  = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length) or b"{}")
            message = save_shapes(payload.get("shapes") or {})
        except Exception as e:
            self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
            return
        print(f"  {message}")
        self._send(200, json.dumps({"message": message}).encode(), "application/json")

    def log_message(self, *args):
        pass  # the default logger spams a line per request


def serve(port: int, open_browser: bool) -> None:
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), _Handler) as httpd:
        url = f"http://127.0.0.1:{port}"
        print(f"Neighborhood editor → {url}")
        print("Draw, edit, then press Save. Ctrl-C to stop.")
        if open_browser:
            threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nStopped.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Edit neighborhood shapes on a map")
    parser.add_argument("--export", metavar="FILE",
                        help="Write a standalone HTML copy and exit (view/export only, no saving)")
    parser.add_argument("--port", type=int, default=8765, help="Port to serve on (default 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Don't open a browser tab")
    args = parser.parse_args()

    if args.export:
        out = Path(args.export)
        out.write_text(build_page(read_only=True), encoding="utf-8")
        print(f"Saved: {out}")
        print("This copy can't write back to neighborhood_shapes.py — use Export GeoJSON.")
        return 0

    serve(args.port, not args.no_browser)
    return 0


if __name__ == "__main__":
    sys.exit(main())
