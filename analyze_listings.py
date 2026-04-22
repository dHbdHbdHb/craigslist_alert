"""
Listings Analysis Dashboard — HTML Edition
--------------------------------------------
Loads all historical listing data (active + archive CSVs) and generates a
self-contained interactive HTML dashboard (analysis_dashboard.html).

Usage:
    python analyze_listings.py
    python analyze_listings.py --open     # open in browser after generating
    python analyze_listings.py --no-html  # terminal summary only
"""

import argparse
import base64
import json
import sys
import time
from collections import deque
from pathlib import Path

import numpy as np
import pandas as pd

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR         = Path(__file__).parent
ACTIVE_CSV       = BASE_DIR / "craigslist_data" / "listings_active.csv"
ARCHIVE_CSV      = BASE_DIR / "craigslist_data" / "listings_archive.csv"
OUTPUT_HTML      = BASE_DIR / "analysis_dashboard.html"
BIKE_ROUTES_FILE      = BASE_DIR / "craigslist_data" / "bike_routes.json"
BART_BIKE_ROUTES_FILE = BASE_DIR / "craigslist_data" / "bart_bike_routes.json"

PRICE_FLOOR = 2_100
PRICE_CEIL  = 15_000

# Listings with no neighborhood match are bucketed here
CATCHALL_HOOD = "Way Out There"

# ── Load & Clean ──────────────────────────────────────────────────────────────

def load_data() -> pd.DataFrame:
    dfs = []
    for path in [ACTIVE_CSV, ARCHIVE_CSV]:
        if path.exists():
            df = pd.read_csv(path)
            if not df.empty:
                dfs.append(df)
    if not dfs:
        sys.exit("No CSV data found. Run the scraper first.")

    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset="url", keep="first")

    df["time_posted"]       = pd.to_datetime(df["time_posted"], utc=True, errors="coerce")
    df["date"]              = df["time_posted"].dt.date
    df["price"]             = pd.to_numeric(df["price"],             errors="coerce")
    df["num_bedrooms"]      = pd.to_numeric(df["num_bedrooms"],      errors="coerce").astype("Int64")
    df["num_bathrooms"]     = pd.to_numeric(df["num_bathrooms"],     errors="coerce").astype("Int64")
    df["bike_time_minutes"]      = pd.to_numeric(df.get("bike_time_minutes"),      errors="coerce")
    df["bart_bike_time_minutes"] = pd.to_numeric(df.get("bart_bike_time_minutes"), errors="coerce")
    df["lat"]                    = pd.to_numeric(df.get("lat"),                    errors="coerce")
    df["lon"]                    = pd.to_numeric(df.get("lon"),                    errors="coerce")
    if "bike_station" in df.columns:
        df["bike_station"] = df["bike_station"].fillna("")
    else:
        df["bike_station"] = ""
    if "bart_station" in df.columns:
        df["bart_station"] = df["bart_station"].fillna("")
    else:
        df["bart_station"] = ""

    df = df.dropna(subset=["price", "num_bedrooms"])
    df = df[(df["price"] >= PRICE_FLOOR) & (df["price"] <= PRICE_CEIL)]

    # Listings with no polygon match become "Way Out There"
    df["neighborhoods"] = df["neighborhoods"].fillna("").str.strip()
    df["neighborhood"]  = df["neighborhoods"].apply(
        lambda s: [n.strip() for n in s.split(",") if n.strip()] or [CATCHALL_HOOD]
    )
    df = df.explode("neighborhood").reset_index(drop=True)

    df["br_bath"] = (
        df["num_bedrooms"].astype(str) + "BR / "
        + df["num_bathrooms"].astype(str).str.replace("<NA>", "?", regex=False) + "BA"
    )
    return df


# ── Terminal Summary ──────────────────────────────────────────────────────────

def fmt_usd(v):
    return f"${v:,.0f}" if pd.notna(v) else "—"

def print_terminal_summary(df: pd.DataFrame):
    unique = df["url"].nunique()
    d_min, d_max = df["date"].min(), df["date"].max()
    print(f"\n{'═'*70}")
    print(f"  SF CRAIGSLIST — PRICE ANALYSIS")
    print(f"  {unique} unique listings  |  {d_min} → {d_max}")
    print(f"{'═'*70}")

    print("\n── By Neighborhood ──")
    g = df.groupby("neighborhood")["price"]
    tbl = pd.DataFrame({
        "n":      g.count(),
        "median": g.median().map(fmt_usd),
        "mean":   g.mean().map(fmt_usd),
        "min":    g.min().map(fmt_usd),
        "max":    g.max().map(fmt_usd),
    }).sort_values("n", ascending=False)
    print(tbl.to_string())

    print("\n── By BR/Bath ──")
    g2 = df.groupby("br_bath")["price"]
    tbl2 = pd.DataFrame({
        "n":      g2.count(),
        "median": g2.median().map(fmt_usd),
        "mean":   g2.mean().map(fmt_usd),
        "min":    g2.min().map(fmt_usd),
        "max":    g2.max().map(fmt_usd),
    }).sort_index()
    print(tbl2.to_string())
    print()


# ── Color / Order Helpers ─────────────────────────────────────────────────────

# Paul Tol "vibrant" high-contrast qualitative palette
# Maximally distinct, works on white backgrounds, accessible
_PALETTE = [
    "#0077BB",  # blue
    "#CC3311",  # vermillion red
    "#009988",  # teal
    "#EE7733",  # orange
    "#AA3377",  # purple
    "#33BBEE",  # sky blue
    "#228833",  # forest green
    "#EE3377",  # magenta
    "#CCBB44",  # gold
]
_WOT_COLOR = "#AAAAAA"  # medium grey — clearly deprioritized


def _hood_order(df: pd.DataFrame) -> list[str]:
    """Neighborhoods sorted by count, with Way Out There always last."""
    counts = df.groupby("neighborhood").size().sort_values(ascending=False)
    hoods  = [h for h in counts.index if h != CATCHALL_HOOD]
    if CATCHALL_HOOD in counts.index:
        hoods.append(CATCHALL_HOOD)
    return hoods


def _hood_colors(hoods: list[str]) -> dict[str, str]:
    colors, idx = {}, 0
    for h in hoods:
        if h == CATCHALL_HOOD:
            colors[h] = _WOT_COLOR
        else:
            colors[h] = _PALETTE[idx % len(_PALETTE)]
            idx += 1
    return colors


def _hex_fill(hex_color: str, alpha: str = "30") -> str:
    """Append 2-digit hex alpha to a 6-digit hex color."""
    return hex_color + alpha


# ── Plotly Chart Specs ────────────────────────────────────────────────────────

def chart_boxplots(df: pd.DataFrame) -> dict:
    hoods  = _hood_order(df)
    colors = _hood_colors(hoods)
    traces = []
    for hood in hoods:
        sub = df[df["neighborhood"] == hood]["price"].tolist()
        traces.append({
            "type": "box",
            "y": sub,
            "name": hood,
            "marker": {"color": colors[hood]},
            "line":   {"color": colors[hood]},
            "fillcolor": _hex_fill(colors[hood], "55"),
            "boxpoints": "outliers",
            "hovertemplate": "$%{y:,.0f}<extra>" + hood + "</extra>",
        })
    return {
        "data": traces,
        "layout": {
            "title":      {"text": "Price Distribution by Neighborhood", "font": {"size": 15}},
            "yaxis":      {"title": "Monthly Rent", "tickprefix": "$", "tickformat": ",.0f", "automargin": True},
            "xaxis":      {"automargin": True},
            "showlegend": False,
            "margin":     {"t": 50, "b": 20, "l": 80, "r": 20},
            "hovermode":  "closest",
        },
    }


def chart_count_bar(df: pd.DataFrame) -> dict:
    hoods = [h for h in _hood_order(df) if h != CATCHALL_HOOD]

    # One trace per bedroom count, sorted ascending
    br_counts = sorted(df["num_bedrooms"].dropna().unique())
    br_palette = ["#4e79a7", "#f28e2b", "#e15759", "#76b7b2", "#59a14f", "#edc948"]

    traces = []
    for i, br in enumerate(br_counts):
        br_label = f"{int(br)}BR"
        color = br_palette[i % len(br_palette)]
        counts = []
        custom = []
        for hood in hoods:
            n = int(((df["neighborhood"] == hood) & (df["num_bedrooms"] == br)).sum())
            counts.append(n)
            custom.append(f"{br_label} listings / {hood}: {n}")
        traces.append({
            "type": "bar",
            "name": br_label,
            "x": hoods,
            "y": counts,
            "marker": {"color": color},
            "customdata": custom,
            "hovertemplate": "%{customdata}<extra></extra>",
        })

    return {
        "data": traces,
        "layout": {
            "title":      {"text": "Listings per Neighborhood", "font": {"size": 15}},
            "barmode":    "stack",
            "yaxis":      {"title": "# Listings", "automargin": True},
            "xaxis":      {"automargin": True, "tickangle": -30},
            "legend":     {"orientation": "h", "y": 1.1},
            "margin":     {"t": 60, "b": 20, "l": 60, "r": 20},
        },
    }


def chart_heatmap(df: pd.DataFrame) -> dict:
    pivot = df.pivot_table(
        values="price", index="neighborhood", columns="num_bedrooms",
        aggfunc="median", observed=True,
    )
    hoods = _hood_order(df)
    pivot = pivot.reindex(hoods)
    br_labels = [f"{int(c)}BR" for c in pivot.columns]

    z     = pivot.values.tolist()
    z_clean = [
        [None if (isinstance(v, float) and np.isnan(v)) else round(v) for v in row]
        for row in z
    ]
    text = [["—" if v is None else f"${v:,}" for v in row] for row in z_clean]

    return {
        "data": [{
            "type": "heatmap",
            "z": z_clean,
            "x": br_labels,
            "y": hoods,
            "text": text,
            "texttemplate": "%{text}",
            "textfont": {"size": 11},
            "colorscale": [[0.0, "#d4f1d4"], [0.5, "#f7c948"], [1.0, "#e84040"]],
            "colorbar": {"title": "Median $/mo", "tickprefix": "$", "tickformat": ",.0f"},
            "hovertemplate": "<b>%{y}</b> — %{x}<br>Median: %{text}<extra></extra>",
        }],
        "layout": {
            "title":  {"text": "Median Rent — Neighborhood × Bedrooms", "font": {"size": 15}},
            "xaxis":  {"title": "Bedrooms", "automargin": True},
            "yaxis":  {"title": "", "autorange": "reversed", "automargin": True},
            "margin": {"t": 50, "l": 20, "b": 40, "r": 20},
        },
    }


def chart_brbath_bar(df: pd.DataFrame) -> dict:
    g = df.groupby("br_bath")["price"].agg(["median", "mean", "count"]).sort_index()
    return {
        "data": [
            {
                "type": "bar", "name": "Median",
                "x": g.index.tolist(), "y": g["median"].round().tolist(),
                "marker": {"color": "#4e79a7"},
                "hovertemplate": "<b>%{x}</b><br>Median: $%{y:,.0f}<extra></extra>",
            },
            {
                "type": "bar", "name": "Mean",
                "x": g.index.tolist(), "y": g["mean"].round().tolist(),
                "marker": {"color": "#f28e2b"},
                "hovertemplate": "<b>%{x}</b><br>Mean: $%{y:,.0f}<extra></extra>",
            },
        ],
        "layout": {
            "title":   {"text": "Price by Bedroom / Bath Type", "font": {"size": 15}},
            "barmode": "group",
            "yaxis":   {"title": "Monthly Rent", "tickprefix": "$", "tickformat": ",.0f", "automargin": True},
            "xaxis":   {"tickangle": -20, "automargin": True},
            "legend":  {"orientation": "h", "y": 1.12},
            "margin":  {"t": 60, "b": 20, "l": 80, "r": 20},
        },
    }


def chart_histogram(df: pd.DataFrame) -> dict:
    med = float(df["price"].median())
    return {
        "data": [{
            "type": "histogram",
            "x": df["price"].tolist(),
            "nbinsx": 35,
            "marker": {"color": "#4e79a7", "line": {"color": "white", "width": 0.5}},
            "hovertemplate": "$%{x:,.0f}<br>Count: %{y}<extra></extra>",
            "name": "Listings",
        }],
        "layout": {
            "title": {"text": "Overall Price Distribution", "font": {"size": 15}},
            "xaxis": {"title": "Monthly Rent", "tickprefix": "$", "tickformat": ",.0f", "automargin": True},
            "yaxis": {"title": "# Listings", "automargin": True},
            "shapes": [{
                "type": "line",
                "x0": med, "x1": med, "y0": 0, "y1": 1, "yref": "paper",
                "line": {"color": "#e84040", "width": 2, "dash": "dash"},
            }],
            "annotations": [{
                "x": med, "y": 1, "yref": "paper",
                "text": f"Median ${med:,.0f}",
                "showarrow": False, "xanchor": "left", "xshift": 6,
                "font": {"color": "#e84040", "size": 11},
            }],
            "showlegend": False,
            "margin": {"t": 50, "b": 20, "l": 60, "r": 20},
        },
    }


def chart_scatter(df: pd.DataFrame) -> dict:
    hoods  = _hood_order(df)
    colors = _hood_colors(hoods)
    traces = []
    for hood in hoods:
        sub    = df[df["neighborhood"] == hood]
        jitter = np.random.uniform(-0.12, 0.12, len(sub)).tolist()
        traces.append({
            "type": "scatter", "mode": "markers",
            "name": hood,
            "x": (sub["num_bedrooms"].astype(float) + jitter).tolist(),
            "y": sub["price"].tolist(),
            "text": sub["title"].tolist(),
            "marker": {
                "color": colors[hood], "size": 8, "opacity": 0.7,
                "line": {"color": "white", "width": 0.5},
            },
            "hovertemplate": (
                "<b>%{text}</b><br>Bedrooms: %{x:.0f}<br>"
                "Price: $%{y:,.0f}<extra>" + hood + "</extra>"
            ),
        })
    return {
        "data": traces,
        "layout": {
            "title":      {"text": "Price vs Bedrooms by Neighborhood", "font": {"size": 15}},
            "xaxis":      {"title": "Bedrooms", "tickvals": [1,2,3,4,5], "automargin": True},
            "yaxis":      {"title": "Monthly Rent", "tickprefix": "$", "tickformat": ",.0f", "automargin": True},
            "legend":     {"orientation": "v", "font": {"size": 10}},
            "hovermode":  "closest",
            "margin":     {"t": 50, "b": 20, "l": 80, "r": 20},
        },
    }


def chart_price_over_time(df: pd.DataFrame):
    daily = (df.groupby("date")["price"]
               .agg(["median", "count"])
               .reset_index()
               .sort_values("date"))
    if len(daily) < 2:
        return None
    return {
        "data": [{
            "type": "scatter", "mode": "lines+markers",
            "x": [str(d) for d in daily["date"].tolist()],
            "y": daily["median"].round().tolist(),
            "text": [f"n={n}" for n in daily["count"].tolist()],
            "marker": {"color": "#4e79a7", "size": 7},
            "line":   {"color": "#4e79a7"},
            "hovertemplate": "%{x}<br>Median: $%{y:,.0f}<br>%{text}<extra></extra>",
        }],
        "layout": {
            "title":      {"text": "Daily Median Price Over Time", "font": {"size": 15}},
            "xaxis":      {"title": "Date", "automargin": True},
            "yaxis":      {"title": "Median $/mo", "tickprefix": "$", "tickformat": ",.0f", "automargin": True},
            "showlegend": False,
            "margin":     {"t": 50, "b": 40, "l": 80, "r": 20},
        },
    }


def chart_bike_times(df: pd.DataFrame):
    """Bar chart of median biking time to Caltrain by neighborhood (listings with known times only)."""
    sub = df[df["bike_time_minutes"].notna() & (df["neighborhood"] != CATCHALL_HOOD)]
    if sub.empty:
        return None
    hoods  = _hood_order(sub)
    colors = _hood_colors(hoods)
    g = sub.groupby("neighborhood")["bike_time_minutes"]
    stats = pd.DataFrame({"median": g.median(), "count": g.count()}).reindex(hoods).dropna()
    if stats.empty:
        return None
    return {
        "data": [{
            "type": "bar",
            "x": stats.index.tolist(),
            "y": stats["median"].round(1).tolist(),
            "marker": {"color": [colors.get(h, "#AAAAAA") for h in stats.index]},
            "text": [f"{v:.0f} min" for v in stats["median"]],
            "textposition": "outside",
            "cliponaxis": False,
            "hovertemplate": "<b>%{x}</b><br>Median bike: %{y:.0f} min<extra></extra>",
        }],
        "layout": {
            "title":      {"text": "Median Bike Time to Caltrain by Neighborhood", "font": {"size": 15}},
            "yaxis":      {"title": "Minutes", "automargin": True},
            "xaxis":      {"automargin": True, "tickangle": -30},
            "showlegend": False,
            "margin":     {"t": 50, "b": 20, "l": 60, "r": 20},
        },
    }


def chart_bart_bike_times(df: pd.DataFrame):
    """Bar chart of median biking time to BART by neighborhood (listings with known times only)."""
    sub = df[df["bart_bike_time_minutes"].notna() & (df["neighborhood"] != CATCHALL_HOOD)]
    if sub.empty:
        return None
    hoods  = _hood_order(sub)
    colors = _hood_colors(hoods)
    g = sub.groupby("neighborhood")["bart_bike_time_minutes"]
    stats = pd.DataFrame({"median": g.median(), "count": g.count()}).reindex(hoods).dropna()
    if stats.empty:
        return None
    return {
        "data": [{
            "type": "bar",
            "x": stats.index.tolist(),
            "y": stats["median"].round(1).tolist(),
            "marker": {"color": [colors.get(h, "#AAAAAA") for h in stats.index]},
            "text": [f"{v:.0f} min" for v in stats["median"]],
            "textposition": "outside",
            "cliponaxis": False,
            "hovertemplate": "<b>%{x}</b><br>Median bike to BART: %{y:.0f} min<extra></extra>",
        }],
        "layout": {
            "title":      {"text": "Median Bike Time to BART by Neighborhood", "font": {"size": 15}},
            "yaxis":      {"title": "Minutes", "automargin": True},
            "xaxis":      {"automargin": True, "tickangle": -30},
            "showlegend": False,
            "margin":     {"t": 50, "b": 20, "l": 60, "r": 20},
        },
    }


def build_folium_map_iframe(df: pd.DataFrame) -> str:
    """
    Builds a Folium neighborhood map (CartoDB Positron tiles, light-opacity
    polygon fills) and returns it as a self-contained <iframe> HTML string
    suitable for embedding directly in the dashboard.
    Excludes 'Way Out There'.
    """
    sys.path.insert(0, str(BASE_DIR))
    try:
        import folium
        from shapely.geometry import mapping
        from neighborhoods.neighborhood_shapes import neighborhood_shapes
    except Exception as e:
        return f'<p style="color:#888">Map unavailable: {type(e).__name__}: {e}</p>'

    known_hoods = [h for h in neighborhood_shapes if h != CATCHALL_HOOD]
    colors      = _hood_colors(_hood_order(df))
    df_known    = df[df["neighborhood"] != CATCHALL_HOOD]

    m = folium.Map(
        location=[37.758, -122.433],
        zoom_start=13,
        tiles="CartoDB positron",
        zoom_control=True,
        scrollWheelZoom=False,   # less jarring when scrolling the dashboard
    )

    for hood in known_hoods:
        poly  = neighborhood_shapes[hood]
        color = colors.get(hood, _WOT_COLOR)
        sub   = df_known[df_known["neighborhood"] == hood]

        # Stats for tooltip
        if len(sub):
            n          = len(sub)
            median_str = f"${sub['price'].median():,.0f}/mo"
            range_str  = f"${sub['price'].min():,.0f} – ${sub['price'].max():,.0f}"
            br_counts  = sub["br_bath"].value_counts()
            top_type   = br_counts.index[0] if len(br_counts) else "—"
        else:
            n, median_str, range_str, top_type = 0, "—", "—", "—"

        feature = {
            "type": "Feature",
            "geometry": mapping(poly),
            "properties": {
                "Neighborhood": hood,
                "Listings":     str(n),
                "Median Rent":  median_str,
                "Price Range":  range_str,
                "Top Type":     top_type,
            },
        }

        folium.GeoJson(
            feature,
            style_function=lambda _, c=color: {
                "fillColor":   c,
                "color":       c,
                "weight":      2,
                "fillOpacity": 0.22,
                "opacity":     0.85,
            },
            highlight_function=lambda _, c=color: {
                "fillColor":   c,
                "fillOpacity": 0.45,
                "weight":      3,
            },
            tooltip=folium.GeoJsonTooltip(
                fields=["Neighborhood", "Listings", "Median Rent", "Price Range", "Top Type"],
                aliases=["Neighborhood", "Listings", "Median Rent", "Price Range", "Top Type"],
                style=(
                    "font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;"
                    "font-size: 13px; padding: 8px 10px;"
                    "border-radius: 6px; border: none;"
                    "box-shadow: 0 2px 8px rgba(0,0,0,0.15);"
                ),
                sticky=True,
            ),
        ).add_to(m)

        # Label at centroid
        cx, cy = poly.centroid.x, poly.centroid.y
        folium.Marker(
            location=[cy, cx],
            icon=folium.DivIcon(
                html=(
                    f'<div style="'
                    f'font-family:-apple-system,sans-serif;'
                    f'font-size:11px;font-weight:700;'
                    f'color:{color};'
                    f'text-shadow:0 0 3px #fff,0 0 3px #fff,0 0 3px #fff;'
                    f'white-space:nowrap;pointer-events:none;'
                    f'">{hood}</div>'
                ),
                icon_size=(120, 20),
                icon_anchor=(60, 10),
            ),
        ).add_to(m)

    # ── Recent listing routes + markers ──────────────────────────────────────
    recent_cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3)
    df_markers = (
        df
        .drop_duplicates(subset="url")
        [lambda d:
            d["bike_time_minutes"].notna() &
            d["time_posted"].notna() &
            (d["time_posted"] >= recent_cutoff) &
            d["lat"].notna() &
            d["lon"].notna()
        ]
    )

    # Highlight the 3 most recently posted listings
    _NEW_COLOR  = "#3b82f6"   # vivid blue — pops against gray dots
    _OLD_COLOR  = "#9ca3af"
    _newest_urls = set(
        df_markers.nlargest(3, "time_posted")["url"].tolist()
    )

    # Load route caches
    try:
        with open(BIKE_ROUTES_FILE) as _f:
            route_cache = json.load(_f)
    except (FileNotFoundError, ValueError):
        route_cache = {}

    try:
        with open(BART_BIKE_ROUTES_FILE) as _f:
            bart_route_cache = json.load(_f)
    except (FileNotFoundError, ValueError):
        bart_route_cache = {}

    missing      = [row for _, row in df_markers.iterrows() if str(row["url"]) not in route_cache]
    bart_missing = [row for _, row in df_markers.iterrows() if str(row["url"]) not in bart_route_cache]

    if missing or bart_missing:
        try:
            import openrouteservice as _ors_mod
            from config import ORS_API_KEY as _ORS_KEY, BART_STATIONS as _BART_STATIONS
            _ors = _ors_mod.Client(key=_ORS_KEY, timeout=15)

            # Simple rate limiter: track call timestamps, sleep if approaching 40/min
            _ors_calls = deque()
            def _rate_limited_directions(*args, **kwargs):
                now = time.time()
                while _ors_calls and _ors_calls[0] < now - 60:
                    _ors_calls.popleft()
                if len(_ors_calls) >= 35:  # leave 5-req buffer
                    wait = 60 - (now - _ors_calls[0]) + 0.5
                    print(f"    Rate limit approaching, sleeping {wait:.0f}s…")
                    time.sleep(wait)
                _ors_calls.append(time.time())
                return _ors.directions(*args, **kwargs)

            _caltrain_stations = [("4th & King", [-122.3942, 37.7763]), ("22nd St", [-122.3925, 37.7577])]
            for row in missing:
                best_min, best_geom, best_stn = None, None, None
                for sname, coords in _caltrain_stations:
                    try:
                        r = _rate_limited_directions(
                            [(row["lon"], row["lat"]), (coords[0], coords[1])],
                            profile="cycling-regular", format="geojson",
                        )
                        mins = int(r["features"][0]["properties"]["summary"]["duration"] / 60)
                        raw  = r["features"][0]["geometry"]["coordinates"]
                        if best_min is None or mins < best_min:
                            best_min  = mins
                            best_stn  = sname
                            best_geom = [[c[1], c[0]] for c in raw]
                    except Exception:
                        pass
                if best_geom:
                    route_cache[str(row["url"])] = {"station": best_stn, "geometry": best_geom}
            if missing:
                with open(BIKE_ROUTES_FILE, "w") as _f:
                    json.dump(route_cache, _f)
                print(f"  Cached Caltrain routes for {len(missing)} listing(s).")

            for row in bart_missing:
                best_min, best_geom, best_stn = None, None, None
                for sname, coords in _BART_STATIONS:
                    try:
                        r = _rate_limited_directions(
                            [(row["lon"], row["lat"]), (coords[0], coords[1])],
                            profile="cycling-regular", format="geojson",
                        )
                        mins = int(r["features"][0]["properties"]["summary"]["duration"] / 60)
                        raw  = r["features"][0]["geometry"]["coordinates"]
                        if best_min is None or mins < best_min:
                            best_min  = mins
                            best_stn  = sname
                            best_geom = [[c[1], c[0]] for c in raw]
                    except Exception:
                        pass
                if best_geom:
                    bart_route_cache[str(row["url"])] = {"station": best_stn, "geometry": best_geom}
            if bart_missing:
                with open(BART_BIKE_ROUTES_FILE, "w") as _f:
                    json.dump(bart_route_cache, _f)
                print(f"  Cached BART routes for {len(bart_missing)} listing(s).")

        except Exception as e:
            print(f"  Could not compute missing routes: {e}")

    # FeatureGroups for bedroom filter
    fg_2br   = folium.FeatureGroup(name="2BR",  show=True)
    fg_3plus = folium.FeatureGroup(name="3BR+", show=True)
    fg_other = folium.FeatureGroup(name="Other BR", show=True)

    def _br_group(row):
        beds_val = row.get("num_bedrooms")
        if pd.notna(beds_val):
            n = int(beds_val)
            if n == 2:
                return fg_2br
            if n >= 3:
                return fg_3plus
        return fg_other

    # Caltrain station markers (always visible, go directly on map)
    for sname, slat, slon in [("4th & King", 37.7763, -122.3942), ("22nd St", 37.7577, -122.3925)]:
        folium.CircleMarker(
            [slat, slon], radius=7,
            color="white", weight=2,
            fill=True, fill_color="#D99441", fill_opacity=0.95,
            tooltip=f"Caltrain: {sname}",
        ).add_to(m)

    # BART station markers
    from config import BART_STATIONS as _BART_STATIONS_MAP
    for sname, coords in _BART_STATIONS_MAP:
        folium.CircleMarker(
            [coords[1], coords[0]], radius=7,
            color="white", weight=2,
            fill=True, fill_color="#0099CC", fill_opacity=0.95,
            tooltip=f"BART: {sname}",
        ).add_to(m)

    # Route polylines + listing dot markers, grouped by bedroom count
    import html as _html
    for _, row in df_markers.iterrows():
        fg = _br_group(row)
        cached = route_cache.get(str(row["url"]))
        if cached and cached.get("geometry"):
            folium.PolyLine(
                cached["geometry"],
                color="#A67D4B", weight=2, opacity=0.45, dash_array="8 6",
            ).add_to(fg)

        mins    = int(row["bike_time_minutes"])
        station = (cached or {}).get("station") or row.get("bike_station") or "Caltrain"
        price   = f"${int(row['price']):,}/mo" if pd.notna(row.get("price")) else "—"
        beds    = str(row["num_bedrooms"]) if pd.notna(row.get("num_bedrooms")) else "?"
        baths   = str(row["num_bathrooms"]) if pd.notna(row.get("num_bathrooms")) else "?"
        title_e = _html.escape(str(row.get("title", "")))
        url     = str(row.get("url", ""))

        # BART info
        bart_cached = bart_route_cache.get(str(row["url"]))
        bart_mins_val = row.get("bart_bike_time_minutes")
        if pd.notna(bart_mins_val):
            bart_mins = int(bart_mins_val)
            bart_stn  = (bart_cached or {}).get("station") or row.get("bart_station") or "BART"
            bart_line = f'<br><span style="color:#0099CC;">{bart_mins} min to {bart_stn} BART</span>'
            bart_tip  = f" · {bart_mins} min to {bart_stn} BART"
        elif bart_cached:
            bart_line = ""
            bart_tip  = ""
        else:
            bart_line = ""
            bart_tip  = ""

        popup_html = (
            f'<div style="font-family:-apple-system,sans-serif;font-size:13px;'
            f'min-width:200px;max-width:260px;line-height:1.6;">'
            f'<a href="{url}" target="_blank" '
            f'style="font-weight:700;color:#262312;text-decoration:none;">'
            f'{title_e[:70]}{"…" if len(title_e) > 70 else ""}</a><br>'
            f'<span style="color:#A67D4B;">{price}</span>'
            f' &nbsp;·&nbsp; {beds}bd/{baths}ba<br>'
            f'<span style="color:#555;">{mins} min to {station} Caltrain</span>'
            f'{bart_line}'
            f'</div>'
        )
        is_new   = row["url"] in _newest_urls
        dot_color = _NEW_COLOR if is_new else _OLD_COLOR
        dot_r     = 6 if is_new else 4
        folium.CircleMarker(
            [row["lat"], row["lon"]], radius=dot_r,
            color="white", weight=1.5,
            fill=True, fill_color=dot_color, fill_opacity=0.9,
            popup=folium.Popup(popup_html, max_width=270),
            tooltip=f"{mins} min to {station}{bart_tip} · {price}",
        ).add_to(fg)

    fg_2br.add_to(m)
    fg_3plus.add_to(m)
    fg_other.add_to(m)

    # Grab JS variable names so the dropdown can reference them
    _v2br   = fg_2br.get_name()
    _v3plus = fg_3plus.get_name()
    _vother = fg_other.get_name()

    _vmap = m.get_name()

    # Map legend + bedroom dropdown
    m.get_root().html.add_child(folium.Element(f"""
        <div style="position:fixed;bottom:16px;left:16px;z-index:999;
                    background:rgba(255,255,255,0.93);padding:10px 14px;
                    border-radius:8px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;
                    font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,0.13);line-height:2;">
          <div style="font-weight:700;font-size:13px;color:#1a1a2e;margin-bottom:2px;">Legend</div>
          <div>
            <svg width="14" height="14" style="vertical-align:middle;margin-right:5px;">
              <circle cx="7" cy="7" r="5.5" fill="#D99441" stroke="white" stroke-width="1.5"/>
            </svg>Caltrain station
          </div>
          <div>
            <svg width="14" height="14" style="vertical-align:middle;margin-right:5px;">
              <circle cx="7" cy="7" r="5.5" fill="#0099CC" stroke="white" stroke-width="1.5"/>
            </svg>BART station
          </div>
          <div>
            <svg width="14" height="14" style="vertical-align:middle;margin-right:5px;">
              <circle cx="7" cy="7" r="5.5" fill="#3b82f6" stroke="white" stroke-width="1.5"/>
            </svg>New listing (3 most recent)
          </div>
          <div>
            <svg width="14" height="14" style="vertical-align:middle;margin-right:5px;">
              <circle cx="7" cy="7" r="4.5" fill="#9ca3af" stroke="white" stroke-width="1.5"/>
            </svg>Listing — click to open
          </div>
          <div>
            <svg width="20" height="8" style="vertical-align:middle;margin-right:5px;">
              <line x1="0" y1="4" x2="20" y2="4" stroke="#A67D4B" stroke-width="2"
                    stroke-dasharray="6 5" opacity="0.7"/>
            </svg>Bike route
          </div>
        </div>

        <div style="position:fixed;top:12px;right:12px;z-index:999;
                    background:rgba(255,255,255,0.95);padding:7px 10px;
                    border-radius:8px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;
                    font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,0.13);">
          <label for="br-filter" style="font-weight:600;color:#1a1a2e;margin-right:6px;">Bedrooms</label>
          <select id="br-filter"
                  style="font-size:12px;border:1px solid #d1d5db;border-radius:5px;
                         padding:2px 6px;background:#fff;cursor:pointer;">
            <option value="all">All</option>
            <option value="2br">2 BR</option>
            <option value="3plus">3+ BR</option>
          </select>
        </div>

        <script>
          document.getElementById('br-filter').addEventListener('change', function() {{
            var val    = this.value;
            var map    = {_vmap};
            var fg2br   = {_v2br};
            var fg3plus = {_v3plus};
            var fgother = {_vother};
            if (val === 'all') {{
              if (!map.hasLayer(fg2br))   map.addLayer(fg2br);
              if (!map.hasLayer(fg3plus)) map.addLayer(fg3plus);
              if (!map.hasLayer(fgother)) map.addLayer(fgother);
            }} else if (val === '2br') {{
              if (!map.hasLayer(fg2br))   map.addLayer(fg2br);
              if (map.hasLayer(fg3plus))  map.removeLayer(fg3plus);
              if (map.hasLayer(fgother))  map.removeLayer(fgother);
            }} else if (val === '3plus') {{
              if (map.hasLayer(fg2br))    map.removeLayer(fg2br);
              if (!map.hasLayer(fg3plus)) map.addLayer(fg3plus);
              if (map.hasLayer(fgother))  map.removeLayer(fgother);
            }}
          }});
        </script>
    """))

    # Encode as base64 so the iframe is fully self-contained
    map_html = m.get_root().render()
    b64      = base64.b64encode(map_html.encode("utf-8")).decode("ascii")
    return (
        f'<iframe src="data:text/html;charset=utf-8;base64,{b64}" '
        f'width="100%" height="500px" '
        f'style="border:none;border-radius:8px;display:block;">'
        f'</iframe>'
    )


# ── Stat Cards ────────────────────────────────────────────────────────────────

def build_stat_cards(df: pd.DataFrame) -> list[dict]:
    unique_listings = df["url"].nunique()
    median_price    = df["price"].median()
    min_price       = df["price"].min()
    max_price       = df["price"].max()
    d_min, d_max    = df["date"].min(), df["date"].max()

    # Top neighborhood excluding catch-all
    hood_counts = df[df["neighborhood"] != CATCHALL_HOOD].groupby("neighborhood").size()
    top_hood    = hood_counts.idxmax() if not hood_counts.empty else CATCHALL_HOOD
    top_br      = df.groupby("br_bath").size().idxmax()

    return [
        {"label": "Unique Listings",  "value": f"{unique_listings:,}"},
        {"label": "Median Rent",      "value": f"${median_price:,.0f}/mo"},
        {"label": "Price Range",      "value": f"${min_price:,.0f} – ${max_price:,.0f}"},
        {"label": "Most Listings",    "value": top_hood},
        {"label": "Most Common Type", "value": top_br},
        {"label": "Date Range",       "value": f"{d_min} → {d_max}"},
    ]


# ── HTML Template ─────────────────────────────────────────────────────────────

HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>SF Craigslist — Price Dashboard</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
    background: #f0f2f5;
    color: #1a1a2e;
    padding: 24px;
  }

  header { margin-bottom: 24px; }
  header h1 {
    font-size: 1.6rem; font-weight: 700;
    color: #1a1a2e; letter-spacing: -0.02em;
  }
  header p { font-size: 0.85rem; color: #6b7280; margin-top: 4px; }

  /* ── Stat Cards ── */
  .cards {
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
    gap: 12px;
    margin-bottom: 24px;
  }
  .card {
    background: #fff; border-radius: 10px;
    padding: 16px 18px; box-shadow: 0 1px 4px rgba(0,0,0,.07);
  }
  .card .label {
    font-size: 0.72rem; font-weight: 600;
    text-transform: uppercase; letter-spacing: 0.06em;
    color: #9ca3af; margin-bottom: 6px;
  }
  .card .value { font-size: 1.1rem; font-weight: 700; color: #1a1a2e; line-height: 1.2; }

  /* ── Chart Grid ── */
  .grid {
    display: grid;
    gap: 16px;
    grid-template-columns: 1fr 1fr;
    grid-template-areas:
      "box    box"
      "time   time"
      "map    map"
      "heat   heat"
      "brbath hist"
      "scatter count"
      "bike   bart";
  }

  .chart-card {
    background: #fff; border-radius: 12px;
    padding: 8px 12px 4px;
    box-shadow: 0 1px 4px rgba(0,0,0,.07);
    min-height: 340px;
    /* give charts room so labels don't clip */
    overflow: visible;
  }

  .area-box     { grid-area: box; }
  .area-heat    { grid-area: heat; }
  .area-brbath  { grid-area: brbath; }
  .area-hist    { grid-area: hist; }
  .area-scatter { grid-area: scatter; }
  .area-count   { grid-area: count; }
  .area-bike    { grid-area: bike; }
  .area-bart    { grid-area: bart; }
  .area-map     { grid-area: map; min-height: 540px; }
  .area-time    { grid-area: time; }

  .plotly-chart { width: 100%; height: 340px; }

  footer { margin-top: 24px; text-align: center; font-size: 0.75rem; color: #9ca3af; }

  /* ── Mobile ── */
  @media (max-width: 640px) {
    body { padding: 12px; }
    header h1 { font-size: 1.25rem; }
    .cards { grid-template-columns: repeat(auto-fill, minmax(130px, 1fr)); gap: 8px; margin-bottom: 16px; }
    /* Stack all chart cards vertically — bypasses grid-template-areas */
    .grid { display: flex; flex-direction: column; gap: 12px; }
    .chart-card { min-height: 280px; }
    .plotly-chart { height: 280px; }
    /* Heatmap and map get a bit more room since they're content-dense */
    .area-heat .plotly-chart { height: 360px; }
    .area-map { min-height: 460px; }
  }
</style>
</head>
<body>

<header>
  <h1>SF Craigslist Rentals — Price Dashboard</h1>
  <p>Historical scraped data &nbsp;·&nbsp; Listings under $2,100/mo excluded &nbsp;·&nbsp;</p>
</header>

<div class="cards" id="cards"></div>

<div class="grid">
  <div class="chart-card area-box">
    <div class="plotly-chart" id="chart-box"></div>
  </div>
  __TIME_SLOT__
  <div class="chart-card area-map" style="padding:12px 14px 10px;">
    <div style="font-size:15px;font-weight:700;margin-bottom:8px;color:#1a1a2e;">
      Neighborhood Map &nbsp;<span style="font-size:11px;font-weight:400;color:#9ca3af;">hover polygons for price stats &nbsp;·&nbsp; neighborhood boundaries + listings from the last 3 days with bike times to Caltrain &amp; BART</span>
    </div>
    __MAP_IFRAME__
  </div>
  <div class="chart-card area-heat">
    <div class="plotly-chart" id="chart-heat" style="height:400px"></div>
  </div>
  <div class="chart-card area-brbath">
    <div class="plotly-chart" id="chart-brbath"></div>
  </div>
  <div class="chart-card area-hist">
    <div class="plotly-chart" id="chart-hist"></div>
  </div>
  <div class="chart-card area-scatter">
    <div class="plotly-chart" id="chart-scatter"></div>
  </div>
  <div class="chart-card area-count">
    <div class="plotly-chart" id="chart-count"></div>
  </div>
  __BIKE_SLOT__
  __BART_SLOT__
</div>

<footer>can't wait to live somewhere someday</footer>

<script>
const COMMON_CONFIG = { responsive: true, displayModeBar: false };
const COMMON_LAYOUT = {
  paper_bgcolor: "transparent",
  plot_bgcolor:  "#fff",
  font: {
    family: "-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif",
    size: 12,
  },
};

function renderChart(id, spec) {
  const layout = Object.assign({}, COMMON_LAYOUT, spec.layout);
  Plotly.newPlot(id, spec.data, layout, COMMON_CONFIG);
}

// ── Stat cards ──
const cards = __CARDS__;
const cardContainer = document.getElementById("cards");
cards.forEach(c => {
  const div = document.createElement("div");
  div.className = "card";
  div.innerHTML = `<div class="label">${c.label}</div><div class="value">${c.value}</div>`;
  cardContainer.appendChild(div);
});

// ── Charts ──
renderChart("chart-box",     __CHART_BOX__);
renderChart("chart-heat",    __CHART_HEAT__);
renderChart("chart-brbath",  __CHART_BRBATH__);
renderChart("chart-hist",    __CHART_HIST__);
renderChart("chart-scatter", __CHART_SCATTER__);
renderChart("chart-count",   __CHART_COUNT__);
__TIME_JS__
__BIKE_JS__
__BART_JS__
</script>
</body>
</html>"""


# ── HTML Assembly ─────────────────────────────────────────────────────────────

def build_html(df: pd.DataFrame) -> str:
    time_chart = chart_price_over_time(df)
    if time_chart:
        time_slot = (
            '<div class="chart-card area-time">'
            '<div class="plotly-chart" id="chart-time"></div></div>'
        )
        time_js = f'renderChart("chart-time", {json.dumps(time_chart)});'
    else:
        time_slot = ""
        time_js   = ""

    bike_chart = chart_bike_times(df)
    if bike_chart:
        bike_slot = (
            '<div class="chart-card area-bike">'
            '<div class="plotly-chart" id="chart-bike"></div></div>'
        )
        bike_js = f'renderChart("chart-bike", {json.dumps(bike_chart)});'
    else:
        bike_slot = ""
        bike_js   = ""

    bart_chart = chart_bart_bike_times(df)
    if bart_chart:
        bart_slot = (
            '<div class="chart-card area-bart">'
            '<div class="plotly-chart" id="chart-bart"></div></div>'
        )
        bart_js = f'renderChart("chart-bart", {json.dumps(bart_chart)});'
    else:
        bart_slot = ""
        bart_js   = ""

    print("Building neighborhood map…")
    map_iframe = build_folium_map_iframe(df)

    html = HTML_TEMPLATE
    html = html.replace("__CARDS__",         json.dumps(build_stat_cards(df)))
    html = html.replace("__CHART_BOX__",     json.dumps(chart_boxplots(df)))
    html = html.replace("__CHART_HEAT__",    json.dumps(chart_heatmap(df)))
    html = html.replace("__CHART_BRBATH__",  json.dumps(chart_brbath_bar(df)))
    html = html.replace("__CHART_HIST__",    json.dumps(chart_histogram(df)))
    html = html.replace("__CHART_SCATTER__", json.dumps(chart_scatter(df)))
    html = html.replace("__CHART_COUNT__",   json.dumps(chart_count_bar(df)))
    html = html.replace("__MAP_IFRAME__",    map_iframe)
    html = html.replace("__TIME_SLOT__",     time_slot)
    html = html.replace("__TIME_JS__",       time_js)
    html = html.replace("__BIKE_SLOT__",     bike_slot)
    html = html.replace("__BIKE_JS__",       bike_js)
    html = html.replace("__BART_SLOT__",     bart_slot)
    html = html.replace("__BART_JS__",       bart_js)
    return html


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Listings HTML dashboard")
    parser.add_argument("--no-html", action="store_true", help="Terminal summary only")
    parser.add_argument("--open",    action="store_true", help="Open dashboard in browser")
    args = parser.parse_args()

    df = load_data()
    print_terminal_summary(df)

    if not args.no_html:
        html = build_html(df)
        OUTPUT_HTML.write_text(html, encoding="utf-8")
        print(f"Dashboard saved → {OUTPUT_HTML}")
        if args.open:
            import subprocess, platform
            opener = "open" if platform.system() == "Darwin" else "xdg-open"
            subprocess.run([opener, str(OUTPUT_HTML)], check=False)


if __name__ == "__main__":
    main()
