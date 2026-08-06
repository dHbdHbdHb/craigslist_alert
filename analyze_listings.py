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
# Paths come from the active profile, so each person's dashboard is built from
# their own data and written to their own file. See config.py / SETUP.md.
from config import (
    DATA_ACTIVE, DATA_ARCHIVE, DASHBOARD_HTML,
    BIKE_ROUTES_PATH, BART_ROUTES_PATH,
    MAP_CENTER, SHOW_HISTORICAL, DISPLAY_NAME, PROFILE_NAME,
    HAS_BIKE_TIMES, HAS_COMMUTE, COMMUTE_DESTINATION,
    COMMUTE_DESTINATION_NAME, COMMUTE_MAX_MINUTES, COMMUTE_CACHE_PATH,
    max_price, digest_max_price, digest_min_price,
    add_profile_arg,
)

BASE_DIR         = Path(__file__).parent
ACTIVE_CSV       = Path(DATA_ACTIVE)
ARCHIVE_CSV      = Path(DATA_ARCHIVE)
OUTPUT_HTML      = Path(DASHBOARD_HTML)
BIKE_ROUTES_FILE      = Path(BIKE_ROUTES_PATH)
BART_BIKE_ROUTES_FILE = Path(BART_ROUTES_PATH)
COMMUTE_CACHE_FILE    = Path(COMMUTE_CACHE_PATH)

# Which listings get their transit trip drawn.
#
# The gate used to be "in a named neighborhood, commute under 30 min", which is
# a readability rule rather than a useful one — it drew routes for whatever
# happened to be measured, mostly 1BRs, and still produced a tangle.
#
# These three are the shortlist instead: the profile's real ceiling ($4,000),
# 2BR or larger, and a commute short enough to be worth the rent. A route on
# this map now means "this is a candidate", so the lines are the answer rather
# than decoration. Everything else stays a dot and is still clickable.
#
# 2BR is a floor, not an exact match: a 3BR inside the same budget and commute
# is at least as good a find, and silently dropping one would be a surprise.
TRANSIT_ROUTE_MAX_MINUTES  = 30
TRANSIT_ROUTE_MIN_BEDROOMS = 2
TRANSIT_ROUTE_MAX_PRICE    = digest_max_price or max_price

# Per-mode styling.
#
# The first attempt used three near-identical dark greens on the theory that the
# modes should read as one family. Rendered, they were indistinguishable at
# 2.5px dotted -- so were their legend keys -- which defeats the point of
# colouring by mode at all. These are the first three slots of the validated
# categorical palette instead: every route is on screen simultaneously, so this
# is the all-pairs case, and those three are the set documented as clearing it
# on a light surface. Anything rarer folds into one neutral rather than taking a
# fourth slot, which is documented to fail against the orange.
#
# Three channels carry mode, so none of them has to carry it alone:
#
#   colour   the three palette slots above, + one neutral for the "Other" bucket
#   weight   BART and Muni Metro are drawn heavier. They are grade-separated and
#            frequent, so a trip on them is the one you'd rather have; the bus is
#            the same colour system but visually the lighter-weight option.
#   dash     longest for BART, medium for Muni, fine dot for bus, sparse for
#            other. Enough on its own to read the map in greyscale or print.
#
# Measured against the Positron land tile (#f2f2f0), the palette hues land at
# 3.94:1 (BART), 2.85:1 (bus) and 2.51:1 (Muni) -- the last two under the 3:1
# gate, which the viz rules say obliges relief rather than a shrug. On a map the
# relief is a casing: see _TRANSIT_CASING. The neutral is the one colour here
# that never had a contrast problem (7:1 at the old #52514e); it is nudged
# darker only to pull it away from Positron's own grey label text.
_TRANSIT_STYLE = {
    "bart":  {"color": "#2a78d6", "label": "BART",          "weight": 4.0, "dash": "12 5"},
    # "Muni" rather than "Muni Metro": the label is the legend key as well as the
    # leg tooltip, and the extra word was costing a legend row on a phone.
    "muni":  {"color": "#1baf7a", "label": "Muni",          "weight": 4.0, "dash": "7 5"},
    "bus":   {"color": "#eb6834", "label": "Bus",           "weight": 2.5, "dash": "3 5"},
    "rail":  {"color": "#3f3f3c", "label": "Other transit", "weight": 2.5, "dash": "2 4"},
    "other": {"color": "#3f3f3c", "label": "Other transit", "weight": 2.5, "dash": "2 4"},
    "walk":  {"color": "#9ca3af", "label": "Walk",          "weight": 1.5, "dash": "1 5"},
}
# Preferred-mode order, so the legend reads top-down as "best case first".
_TRANSIT_LEGEND_ORDER = ("bart", "muni", "bus", "rail", "other", "walk")

# Ascending paint order — Leaflet draws in insertion order, so the mode listed
# last wins every overlap. BART is the spine of a trip and the bus is the local
# leg feeding it, so BART reads on top. Unlisted modes sort as "other".
_TRANSIT_Z_ORDER = ("walk", "other", "rail", "bus", "muni", "bart")


def _transit_z(mode: str | None) -> int:
    """Paint rank for a leg; unknown modes rank with 'other'."""
    m = mode or "other"
    if m not in _TRANSIT_Z_ORDER:
        m = "other"
    return _TRANSIT_Z_ORDER.index(m)


# A dotted line loses most of its apparent contrast to the gaps, so the hues
# above were being read against whatever the basemap put under them -- a park
# fill, a road casing, another route. The fix is the standard cartographic one,
# and the same idea as the surface ring the viz rules put on overlapping dots: a
# solid white line under each leg, a little wider, so every leg carries its own
# local surface and the hue is judged against white rather than against the map.
# Costs one extra PolyLine per leg; that is why opacity can go to 0.95 without
# the map turning muddy.
_TRANSIT_CASING = {"color": "#ffffff", "opacity": 0.9, "extra_weight": 3.0}
_TRANSIT_OPACITY = 0.95

# Station colours, shared by the station dot and the bike route that ends at it
# -- the pairing is what lets you follow a route to its destination without
# consulting the legend, so these two always move together.
#
# BART was #C8363B, which sat ΔE 3.3 (normal vision) from the #CC3311 commute
# destination marker: same hue, same circle, differing only in radius, so on a
# profile drawing both there was no reading that told them apart. Violet is the
# validated palette's slot 7 and the only candidate tried that clears both CVD
# and normal-vision floors against everything else round on the map -- the
# obvious "BART is blue" choice collides with the #3b82f6 new-listing dot
# instead (ΔE 5.6), which is how the red problem started.
_CALTRAIN_COLOR = "#D99441"
_BART_COLOR     = "#4a3aa7"
_STATION_R      = 8

# Walk legs are cached but not drawn. There are roughly six of them per trip
# against one or two transit legs, so drawing them put ~140 grey dashes on the
# map against ~24 coloured ones and buried the thing the colour is for. The
# route reads fine as segments between stops. Flip this to show them.
TRANSIT_DRAW_WALK_LEGS = False

# Frozen record of the original 2026 SF search. Read-only, never mixed into the
# live figures — see data/historical/README.md.
HISTORICAL_CSV = BASE_DIR / "data" / "historical" / "2026-sf.csv"

# Follows the profile rather than being one shared number. It was hard-coded at
# $2,100, which was set for a 2BR/3BR search and quietly threw away 26% of every
# profile's listings — including, on a search that accepts 1BRs, real in-laws and
# rent-controlled studios at $1,850-2,050.
#
# It is not a taste setting: below it the data stops being apartments. The
# $900-1,200 band is room-shares mislabelled as 1BR, and repeated identical
# prices ($1,000 x7, $1,050 x5) are one poster bulk-spraying. The floor is where
# a whole unit stops being plausible, and that depends on what the person is
# looking for — so it comes from their own digest floor.
#
# Note this does NOT change the API bill: the scraper routes commutes on the raw
# scrape, before this filter ever runs, so these listings were already paid for
# and were simply being discarded afterwards.
PRICE_FLOOR = digest_min_price or 2_100
PRICE_CEIL  = 15_000

# Listings matching no polygon at all are bucketed here. Deliberately NOT the
# name of any drawn shape: "Way Out There" is a real polygon covering western
# SF, and folding unmatched listings into it made a genuine neighborhood look
# like a dumping ground and kept it out of every per-neighborhood chart.
CATCHALL_HOOD = "Rest of City"

# Places a listing ends up without anyone having asked for it: the catch-all,
# and "Way Out There", which is a real shape but a deliberately vague one
# covering everything west. Map highlighting skips both, so the blue dots mean
# "somewhere you named" rather than "somewhere nothing else claimed".
UNHIGHLIGHTED_HOODS = {CATCHALL_HOOD, "Way Out There"}

# ── Load & Clean ──────────────────────────────────────────────────────────────

def load_data(paths: list[Path] | None = None, *, label: str = "live") -> pd.DataFrame:
    """Load and clean listings from one or more CSVs.

    Defaults to the active profile's own data. Passing `paths` explicitly is how
    the frozen historical dataset is loaded through the same cleaning rules, so
    the two are always directly comparable.
    """
    paths = paths if paths is not None else [ACTIVE_CSV, ARCHIVE_CSV]

    dfs = []
    for path in paths:
        if path.exists():
            try:
                df = pd.read_csv(path)
            except (pd.errors.EmptyDataError, pd.errors.ParserError):
                continue
            if not df.empty:
                dfs.append(df)
    if not dfs:
        if label != "live":
            return pd.DataFrame()
        sys.exit(
            f"No CSV data found for profile '{PROFILE_NAME}'.\n"
            f"Looked in: {', '.join(str(p) for p in paths)}\n"
            f"Run the scraper first:  python craigslist_scraper.py --profile {PROFILE_NAME}"
        )

    df = pd.concat(dfs, ignore_index=True)
    df = df.drop_duplicates(subset="url", keep="first")

    df["time_posted"]       = pd.to_datetime(df["time_posted"], utc=True, errors="coerce")
    df["date"]              = df["time_posted"].dt.date
    df["price"]             = pd.to_numeric(df["price"],             errors="coerce")
    df["num_bedrooms"]      = pd.to_numeric(df["num_bedrooms"],      errors="coerce").astype("Int64")
    df["num_bathrooms"]     = pd.to_numeric(df["num_bathrooms"],     errors="coerce").astype("Int64")
    df["bike_time_minutes"]      = pd.to_numeric(df.get("bike_time_minutes"),      errors="coerce")
    df["bart_bike_time_minutes"] = pd.to_numeric(df.get("bart_bike_time_minutes"), errors="coerce")
    df["commute_minutes"]        = pd.to_numeric(df.get("commute_minutes"),        errors="coerce")
    df["commute_walk_minutes"]   = pd.to_numeric(df.get("commute_walk_minutes"),   errors="coerce")
    df["commute_headway"]        = pd.to_numeric(df.get("commute_headway"),        errors="coerce")
    df["transit_score"]          = pd.to_numeric(df.get("transit_score"),          errors="coerce")
    df["transit_lines"]          = (df["transit_lines"].fillna("")
                                    if "transit_lines" in df.columns else "")
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

    # Listings with no polygon match become CATCHALL_HOOD
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


def load_historical(live: pd.DataFrame | None = None) -> pd.DataFrame:
    """The frozen 2026 SF dataset, or empty if it's missing or switched off.

    Deliberately kept out of load_data(): these listings are months old, and
    letting them into the live figures would quietly skew every median, count,
    and heatmap on the dashboard.

    Any listing already present in `live` is dropped. That matters for the
    profile the snapshot was originally taken from — without it, its own history
    would be counted twice and plotted as two identical lines.
    """
    if not SHOW_HISTORICAL or not HISTORICAL_CSV.exists():
        return pd.DataFrame()

    hist = load_data([HISTORICAL_CSV], label="historical")
    if hist.empty or live is None or live.empty:
        return hist

    overlap = hist["url"].isin(set(live["url"]))
    if overlap.any():
        print(f"  ({overlap.sum()} archived listing(s) already in live data — "
              f"not double-counted)")
    return hist[~overlap].reset_index(drop=True)


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
    """Neighborhoods sorted by count, with the catch-all bucket always last."""
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


def _daily_median(df: pd.DataFrame) -> pd.DataFrame:
    return (df.groupby("date")["price"]
              .agg(["median", "count"])
              .reset_index()
              .sort_values("date"))


def chart_price_over_time(df: pd.DataFrame, historical: pd.DataFrame | None = None):
    """Daily median price, with the frozen historical search as a second series.

    The two are drawn as separate traces rather than one merged line: they're
    months apart, so joining them would invent a trend across a gap where
    nothing was ever measured.
    """
    daily = _daily_median(df)
    hist_daily = _daily_median(historical) if historical is not None and not historical.empty \
        else pd.DataFrame()

    if len(daily) < 2 and len(hist_daily) < 2:
        return None

    traces = []

    if not hist_daily.empty:
        traces.append({
            "type": "scatter", "mode": "lines+markers",
            "name": "2026 search (archived)",
            "x": [str(d) for d in hist_daily["date"].tolist()],
            "y": hist_daily["median"].round().tolist(),
            "text": [f"n={n}" for n in hist_daily["count"].tolist()],
            "marker": {"color": "#b0b0b0", "size": 5},
            "line":   {"color": "#b0b0b0", "width": 1.5, "dash": "dot"},
            "hovertemplate": "%{x}<br>Median: $%{y:,.0f}<br>%{text}"
                             "<extra>archived</extra>",
        })

    if len(daily) >= 2:
        traces.append({
            "type": "scatter", "mode": "lines+markers",
            "name": "Current search",
            "x": [str(d) for d in daily["date"].tolist()],
            "y": daily["median"].round().tolist(),
            "text": [f"n={n}" for n in daily["count"].tolist()],
            "marker": {"color": "#4e79a7", "size": 7},
            "line":   {"color": "#4e79a7"},
            "hovertemplate": "%{x}<br>Median: $%{y:,.0f}<br>%{text}<extra></extra>",
        })

    return {
        "data": traces,
        "layout": {
            "title":      {"text": "Daily Median Price Over Time", "font": {"size": 15}},
            "xaxis":      {"title": "Date", "automargin": True},
            "yaxis":      {"title": "Median $/mo", "tickprefix": "$", "tickformat": ",.0f", "automargin": True},
            # Always label when the archived series is on screen, even if it's
            # the only one — otherwise a new profile's first day shows a lone
            # grey line that reads as if it were their own data.
            "showlegend": not hist_daily.empty or len(traces) > 1,
            "legend":     {"orientation": "h", "y": -0.25, "x": 0},
            "margin":     {"t": 50, "b": 40, "l": 80, "r": 20},
        },
    }


def _chart_median_minutes(df: pd.DataFrame, column: str, title: str, hover: str):
    """Bar chart of a duration column's median by neighborhood.

    Shared by the bike-to-station and commute-to-work charts, which differ only
    in which column they read and what they call it. Rows without a value are
    excluded rather than counted as zero.
    """
    sub = df[df[column].notna() & (df["neighborhood"] != CATCHALL_HOOD)]
    if sub.empty:
        return None
    hoods  = _hood_order(sub)
    colors = _hood_colors(hoods)
    g = sub.groupby("neighborhood")[column]
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
            "hovertemplate": "<b>%{x}</b><br>" + hover + ": %{y:.0f} min<extra></extra>",
        }],
        "layout": {
            "title":      {"text": title, "font": {"size": 15}},
            "yaxis":      {"title": "Minutes", "automargin": True},
            "xaxis":      {"automargin": True, "tickangle": -30},
            "showlegend": False,
            "margin":     {"t": 50, "b": 20, "l": 60, "r": 20},
        },
    }


def chart_bike_times(df: pd.DataFrame):
    return _chart_median_minutes(
        df, "bike_time_minutes",
        "Median Bike Time to Caltrain by Neighborhood", "Median bike",
    )


def chart_bart_bike_times(df: pd.DataFrame):
    return _chart_median_minutes(
        df, "bart_bike_time_minutes",
        "Median Bike Time to BART by Neighborhood", "Median bike to BART",
    )


def chart_commute_times(df: pd.DataFrame):
    """Median door-to-door transit commute by neighborhood, against the budget."""
    spec = _chart_median_minutes(
        df, "commute_minutes",
        f"Median Transit Commute to {COMMUTE_DESTINATION_NAME} by Neighborhood",
        "Median commute",
    )
    if spec is None:
        return None

    # The budget line is what turns the bars from trivia into a decision: bars
    # under it are viable, bars over it are not.
    spec["layout"]["shapes"] = [{
        "type": "line", "xref": "paper", "x0": 0, "x1": 1,
        "yref": "y", "y0": COMMUTE_MAX_MINUTES, "y1": COMMUTE_MAX_MINUTES,
        "line": {"color": "#CC3311", "width": 1.5, "dash": "dash"},
    }]
    spec["layout"]["annotations"] = [{
        "xref": "paper", "x": 1, "xanchor": "right",
        "yref": "y", "y": COMMUTE_MAX_MINUTES, "yanchor": "bottom",
        "text": f"{COMMUTE_MAX_MINUTES} min budget",
        "showarrow": False,
        "font": {"size": 10, "color": "#CC3311"},
    }]
    return spec


def chart_transit_quality(df: pd.DataFrame):
    """Median transit score by neighborhood, broken out by what drives it.

    The score blends commute time, service frequency, and line redundancy, so the
    bar alone is not self-explanatory — the hover carries the three inputs, which
    is what makes it possible to tell "close but infrequent" from "far but on the
    N Judah every four minutes".
    """
    sub = df[df["transit_score"].notna() & (df["neighborhood"] != CATCHALL_HOOD)]
    if sub.empty:
        return None

    hoods  = _hood_order(sub)
    colors = _hood_colors(hoods)
    g      = sub.groupby("neighborhood")
    stats  = pd.DataFrame({
        "score":   g["transit_score"].median(),
        "commute": g["commute_minutes"].median(),
        "headway": g["commute_headway"].median(),
        "walk":    g["commute_walk_minutes"].median(),
    }).reindex(hoods).dropna(subset=["score"])
    if stats.empty:
        return None

    # Most common set of lines seen in each neighborhood, for the hover.
    top_lines = (
        sub[sub["transit_lines"].astype(str).str.len() > 0]
        .groupby("neighborhood")["transit_lines"]
        .agg(lambda s: s.value_counts().index[0] if len(s) else "—")
    )

    stats = stats.sort_values("score")
    custom = [
        [
            f"{r.commute:.0f}" if pd.notna(r.commute) else "—",
            f"{r.headway:.0f}" if pd.notna(r.headway) else "—",
            f"{r.walk:.0f}"    if pd.notna(r.walk)    else "—",
            top_lines.get(hood, "—"),
        ]
        for hood, r in stats.iterrows()
    ]

    return {
        "data": [{
            "type": "bar",
            "orientation": "h",
            "y": stats.index.tolist(),
            "x": stats["score"].round(0).tolist(),
            "marker": {"color": [colors.get(h, "#AAAAAA") for h in stats.index]},
            "text": [f"{v:.0f}" for v in stats["score"]],
            "textposition": "outside",
            "cliponaxis": False,
            "customdata": custom,
            "hovertemplate": (
                "<b>%{y}</b><br>Transit score: %{x:.0f}/100<br>"
                "Commute: %{customdata[0]} min<br>"
                "Service every ~%{customdata[1]} min<br>"
                "Walk to stop: %{customdata[2]} min<br>"
                "Usual lines: %{customdata[3]}<extra></extra>"
            ),
        }],
        "layout": {
            "title": {"text": "Transit Access Score by Neighborhood", "font": {"size": 15}},
            "xaxis": {"title": "Score (time + frequency + redundancy)",
                      "range": [0, 108], "automargin": True},
            "yaxis": {"automargin": True},
            "showlegend": False,
            "margin": {"t": 50, "b": 40, "l": 20, "r": 20},
        },
    }


def build_folium_map_iframe(df: pd.DataFrame) -> str:
    """
    Builds a Folium neighborhood map (CartoDB Positron tiles, light-opacity
    polygon fills) and returns it as a self-contained <iframe> HTML string
    suitable for embedding directly in the dashboard.
    Excludes the catch-all bucket.
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
        location=MAP_CENTER,
        zoom_start=13,
        tiles="CartoDB positron",
        zoom_control=True,
        scrollWheelZoom=False,   # less jarring when scrolling the dashboard
    )

    # No viewport meta is added here on purpose. folium's own Figure template
    # already emits one, later in <head> than anything added via header, so a
    # second tag is both redundant and silently overridden — it looked like it
    # was doing something while doing nothing. The overlays' mobile problem was
    # their own size, and it is fixed where they are built.

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
    # Coordinates and recency are the only requirements. This used to also
    # require a bike time, which silently emptied the map for any profile that
    # doesn't do bike routing at all.
    recent_cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=3)

    # load_data() reads active + archive on purpose — the stats want the removed
    # listings, since the ones that went fastest are the most informative rows.
    # The map does not: a pin you can't click through to is worse than no pin.
    # Without this, anything taken down within the recency window stayed on the
    # map until it aged out, which was a quarter of all markers.
    if "removed_at" in df.columns:
        still_listed = df["removed_at"].isna()
    else:
        still_listed = pd.Series(True, index=df.index)

    df_markers = (
        df[still_listed]
        .drop_duplicates(subset="url")
        [lambda d:
            d["time_posted"].notna() &
            (d["time_posted"] >= recent_cutoff) &
            d["lat"].notna() &
            d["lon"].notna()
        ]
    )

    # Highlight the 3 most recently posted listings that landed in a named
    # neighborhood. Drawing from the whole set meant the highlight was usually
    # spent on a listing in the catch-all or in "Way Out There" — the two
    # buckets nobody actually searched for — which is exactly backwards.
    _NEW_COLOR  = "#3b82f6"   # vivid blue — pops against gray dots
    _OLD_COLOR  = "#9ca3af"

    def _in_named_hood(raw) -> bool:
        # The raw column is comma-joined and NaN when nothing matched, so a
        # listing counts if any one of its shapes is a named one.
        if not isinstance(raw, str):
            return False
        return any(
            h.strip() and h.strip() not in UNHIGHLIGHTED_HOODS
            for h in raw.split(",")
        )

    # Guarded on empty: .apply() over a zero-row column returns a bare object
    # Series, and indexing with it yields a frame with no columns at all — so
    # nlargest("time_posted") raised KeyError and took the whole dashboard build
    # down with it. That happens whenever nothing was posted in the recency
    # window, which is not an error: a stale profile, or any scrape gap longer
    # than the window, lands there. An empty map is the correct output.
    #
    # This is now only the paint the map loads with — the filter script
    # recomputes the same three on every dropdown change, and its unfiltered
    # answer is this one, so there is no flicker on load. It stays because it is
    # also the fallback: the filter block has broken before on script ordering,
    # and a map whose highlight silently vanished would be worse than one that
    # simply doesn't follow the dropdowns.
    if len(df_markers):
        # Named hoods rank first, recency breaks it within each tier — the same
        # preference-not-requirement ordering the filter script uses, so the two
        # never disagree about which three are newest.
        _ranked = df_markers.assign(
            _named=df_markers["neighborhoods"].apply(_in_named_hood)
        ).sort_values(["_named", "time_posted"], ascending=[False, False])
        _newest_urls = set(_ranked.head(3)["url"].tolist())
    else:
        _newest_urls = set()

    # Load route caches. Profiles with no [transit] stations skip all of this —
    # there is nothing to route to, and the backfill below would otherwise churn
    # through every listing producing nothing.
    route_cache, bart_route_cache = {}, {}
    missing, bart_missing = [], []

    # Transit routes come from the commute cache rather than a fetch here: the
    # scraper and the digest are what call the Routes API, so the map draws
    # whatever they have already paid for and never triggers a request itself.
    commute_cache = {}
    # Collected while drawing so the legend lists only the modes that actually
    # appear — a BART key on a map with no BART leg is just noise.
    transit_modes_drawn = set()
    if HAS_COMMUTE:
        try:
            with open(COMMUTE_CACHE_FILE) as _f:
                commute_cache = json.load(_f)
        except (FileNotFoundError, ValueError):
            commute_cache = {}

    if HAS_BIKE_TIMES:
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

            from config import CALTRAIN_STATIONS as _caltrain_stations
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

    # One group for everything filterable. Bedrooms and commute are independent
    # filters, so a FeatureGroup per bedroom bucket can't express both without a
    # group per combination — the dropdowns below filter layer-by-layer instead.
    fg_listings = folium.FeatureGroup(name="Listings", show=True)

    # `filterable` pairs each Leaflet layer's JS variable name with the values
    # the dropdowns filter on. folium 0.14 drops unrecognised kwargs from
    # CircleMarker, so custom attributes can't ride along on the marker itself;
    # get_name() is the supported way to reach the layer from our own script.
    filterable = []

    # Station markers, for profiles that route to stations at all.
    # Profile coords are [lon, lat]; folium wants [lat, lon].
    #
    # Size is the class channel here, so a glance sorts the circles without
    # reading colour: destination 11 > station 8 > new listing 6 > old listing 4.
    # Stations used to sit at 7, one pixel off the new-listing dot, which made
    # two unrelated things look like the same kind of thing.
    if HAS_BIKE_TIMES:
        from config import CALTRAIN_STATIONS as _CALTRAIN_STATIONS_MAP
        for sname, coords in _CALTRAIN_STATIONS_MAP:
            folium.CircleMarker(
                [coords[1], coords[0]], radius=_STATION_R,
                color="white", weight=2,
                fill=True, fill_color=_CALTRAIN_COLOR, fill_opacity=0.95,
                tooltip=f"Caltrain: {sname}",
            ).add_to(m)

        from config import BART_STATIONS as _BART_STATIONS_MAP
        for sname, coords in _BART_STATIONS_MAP:
            folium.CircleMarker(
                [coords[1], coords[0]], radius=_STATION_R,
                color="white", weight=2,
                fill=True, fill_color=_BART_COLOR, fill_opacity=0.95,
                tooltip=f"BART: {sname}",
            ).add_to(m)

    import html as _html
    for _, row in df_markers.iterrows():
        url        = str(row.get("url", ""))
        layer_vars = []

        cached = route_cache.get(url)
        if cached and cached.get("geometry"):
            line = folium.PolyLine(
                cached["geometry"],
                color=_CALTRAIN_COLOR, weight=2.5, opacity=0.75, dash_array="8 6",
                tooltip="Bike route to Caltrain",
            )
            line.add_to(fg_listings)
            layer_vars.append(line.get_name())

        bart_cached = bart_route_cache.get(url)
        if bart_cached and bart_cached.get("geometry"):
            line = folium.PolyLine(
                bart_cached["geometry"],
                color=_BART_COLOR, weight=2.5, opacity=0.75, dash_array="2 6",
                tooltip="Bike route to BART",
            )
            line.add_to(fg_listings)
            layer_vars.append(line.get_name())

        # Transit trip to work, one dotted line per leg so bus, Muni and BART
        # are told apart at a glance. Drawn only for shortlist candidates — see
        # the TRANSIT_ROUTE_* constants for why that's the gate rather than
        # "everything we happen to have measured".
        commute_cached = commute_cache.get(url)
        _beds_v  = row.get("num_bedrooms")
        _price_v = row.get("price")
        _draws_route = (
            bool(commute_cached)
            and (commute_cached.get("minutes") or 0) <= TRANSIT_ROUTE_MAX_MINUTES
            and pd.notna(_beds_v)  and _beds_v  >= TRANSIT_ROUTE_MIN_BEDROOMS
            and pd.notna(_price_v) and _price_v <= TRANSIT_ROUTE_MAX_PRICE
        )
        if _draws_route:
            total = commute_cached.get("minutes")
            # Fall back to the whole-trip polyline if a response came back
            # without step geometry, so the route still shows up uncoloured.
            drawn = commute_cached.get("segments") or (
                [{"mode": "other", "geometry": commute_cached["geometry"]}]
                if commute_cached.get("geometry") else []
            )
            legs = [
                seg for seg in drawn
                if seg.get("geometry")
                and not (seg.get("mode") == "walk" and not TRANSIT_DRAW_WALK_LEGS)
            ]
            legs.sort(key=lambda s: _transit_z(s.get("mode")))

            # Two passes, not one per leg. Drawing casing+line together only put
            # a leg's casing under its *own* colour — across legs, a later
            # casing still landed on top of an earlier colour, which is how the
            # BART leg ended up buried under the white bed of the bus leg it
            # connects to. Every casing goes down first, then the colours in
            # _TRANSIT_Z_ORDER, so BART finishes on top of the local legs.
            for seg in legs:
                style = _TRANSIT_STYLE.get(seg.get("mode"), _TRANSIT_STYLE["other"])
                # Solid, not dashed: the point is an unbroken white bed for the
                # dashes to sit on.
                casing = folium.PolyLine(
                    seg["geometry"],
                    color=_TRANSIT_CASING["color"],
                    weight=style["weight"] + _TRANSIT_CASING["extra_weight"],
                    opacity=_TRANSIT_CASING["opacity"],
                )
                casing.add_to(fg_listings)
                layer_vars.append(casing.get_name())

            for seg in legs:
                style = _TRANSIT_STYLE.get(seg.get("mode"), _TRANSIT_STYLE["other"])
                transit_modes_drawn.add(seg.get("mode") or "other")
                line = folium.PolyLine(
                    seg["geometry"],
                    color=style["color"], weight=style["weight"],
                    opacity=_TRANSIT_OPACITY, dash_array=style["dash"],
                    tooltip=(
                        f"{style['label']} — {total} min to "
                        f"{COMMUTE_DESTINATION_NAME}"
                    ),
                )
                line.add_to(fg_listings)
                layer_vars.append(line.get_name())

        price   = f"${int(row['price']):,}/mo" if pd.notna(row.get("price")) else "—"
        beds    = str(row["num_bedrooms"]) if pd.notna(row.get("num_bedrooms")) else "?"
        baths   = str(row["num_bathrooms"]) if pd.notna(row.get("num_bathrooms")) else "?"
        title_e = _html.escape(str(row.get("title", "")))

        detail_lines, tip_bits = [], []

        # Commute first — for a transit-first search it's the headline number.
        #
        # The absent case gets its own line rather than nothing. A popup that
        # simply omitted the commute was indistinguishable from one reporting a
        # good one, which is the same confusion the dimmed dot fixes on the map:
        # most listings out west have never been measured, and "we didn't check"
        # has to read differently from "it's fine".
        commute_val = row.get("commute_minutes")
        if pd.notna(commute_val):
            commute_min = int(commute_val)
            # No line names here on purpose — see transit_commute._describe.
            # transit_lines is the redundancy set, so listing it beside the walk
            # time claimed a route the trip may not use.
            bits = []
            if pd.notna(row.get("commute_walk_minutes")):
                bits.append(f"{int(row['commute_walk_minutes'])} min walk to transit")
            if pd.notna(row.get("commute_headway")):
                bits.append(f"every ~{int(row['commute_headway'])} min")
            detail_lines.append(
                f'<span style="color:#2f6f4f;font-weight:700;">'
                f'{commute_min} min to {_html.escape(COMMUTE_DESTINATION_NAME)}</span>'
                + (f'<br><span style="color:#6b7280;font-size:12px;">'
                   f'{_html.escape(", ".join(bits))}</span>' if bits else "")
            )
            tip_bits.append(f"{commute_min} min to {COMMUTE_DESTINATION_NAME}")
            if pd.notna(row.get("transit_score")):
                detail_lines.append(
                    f'<span style="color:#6b7280;font-size:12px;">'
                    f'transit score {int(row["transit_score"])}/100</span>'
                )
        elif HAS_COMMUTE and COMMUTE_DESTINATION:
            detail_lines.append(
                '<span style="color:#9a6a2f;font-weight:600;">'
                'Commute not calculated</span>'
                '<br><span style="color:#6b7280;font-size:12px;">'
                'No transit time was ever measured for this address — it is not '
                'a short commute, it is an unknown one.</span>'
            )
            tip_bits.append("commute not calculated")

        # Bike times, when the profile computes them. Guarded on notna: this
        # used to assume every marker had one, which crashed the whole dashboard
        # for a profile that routes no bikes at all.
        bike_val = row.get("bike_time_minutes")
        if pd.notna(bike_val):
            station = (cached or {}).get("station") or row.get("bike_station") or "Caltrain"
            detail_lines.append(
                f'<span style="color:#D99441;">{int(bike_val)} min bike to '
                f'{_html.escape(str(station))} Caltrain</span>'
            )
            tip_bits.append(f"{int(bike_val)} min to {station}")

        bart_val = row.get("bart_bike_time_minutes")
        if pd.notna(bart_val):
            bart_stn = (bart_cached or {}).get("station") or row.get("bart_station") or "BART"
            detail_lines.append(
                f'<span style="color:#C8363B;">{int(bart_val)} min bike to '
                f'{_html.escape(str(bart_stn))} BART</span>'
            )

        popup_html = (
            f'<div style="font-family:-apple-system,sans-serif;font-size:13px;'
            f'min-width:200px;max-width:260px;line-height:1.6;">'
            f'<a href="{url}" target="_blank" '
            f'style="font-weight:700;color:#262312;text-decoration:none;">'
            f'{title_e[:70]}{"…" if len(title_e) > 70 else ""}</a><br>'
            f'<span style="color:#A67D4B;">{price}</span>'
            f' &nbsp;&nbsp; {beds}bd/{baths}ba<br>'
            + "<br>".join(detail_lines)
            + '</div>'
        )

        is_new    = url in _newest_urls
        dot_color = _NEW_COLOR if is_new else _OLD_COLOR
        dot_r     = 6 if is_new else 4

        # A listing with no commute figure is drawn faded. It is the majority of
        # the map west of the include list, and it used to be indistinguishable
        # from a measured listing — which mattered because the commute filter
        # lets these through (a missing measurement still isn't evidence of a bad
        # commute). Fading them means the filter's output reads as "these
        # qualify, and these we never checked" instead of one undifferentiated
        # set. Opacity carries it rather than a fourth colour: the dot vocabulary
        # is already three sizes deep and this is a qualifier on a dot, not a
        # new kind of thing.
        # Gated on the profile having commutes at all, to match the popup branch
        # and the legend key. Without the gate, a profile with no [commute]
        # section fades *every* dot: load_data() fills commute_minutes with NaN
        # when the column is absent, so nothing is ever "measured" — and the
        # "Commute unknown" key that would explain the fading is itself gated on
        # HAS_COMMUTE, so the map would dim wholesale with no legend for it.
        _fades     = HAS_COMMUTE and COMMUTE_DESTINATION and pd.isna(commute_val)
        _fill_op   = 0.28 if _fades else 0.9
        _stroke_op = 0.45 if _fades else 1.0
        marker = folium.CircleMarker(
            [row["lat"], row["lon"]], radius=dot_r,
            color="white", weight=1.5, opacity=_stroke_op,
            fill=True, fill_color=dot_color, fill_opacity=_fill_op,
            popup=folium.Popup(popup_html, max_width=270),
            tooltip=", ".join([*tip_bits, price]) or price,
        )
        marker.add_to(fg_listings)
        layer_vars.append(marker.get_name())

        beds_val  = row.get("num_bedrooms")
        price_val = row.get("price")
        filterable.append({
            "vars":    layer_vars,
            # The dot on its own, separate from `vars`, which also holds this
            # listing's route polylines. The highlight restyles a circle and
            # only a circle — setRadius() on a PolyLine is not a function.
            "dot":     marker.get_name(),
            # Epoch seconds, so the browser can re-rank by recency without
            # parsing anything. `named` is the same eligibility test the
            # build-time highlight used, carried over so JS can reapply it.
            "ts":      int(row["time_posted"].timestamp()),
            "named":   _in_named_hood(row.get("neighborhoods")),
            "beds":    int(beds_val) if pd.notna(beds_val) else None,
            "commute": int(commute_val) if pd.notna(commute_val) else None,
            "price":   int(price_val) if pd.notna(price_val) else None,
        })

    fg_listings.add_to(m)

    # Work destination — the anchor the whole commute column is measured against,
    # so it belongs on the map. Added last on purpose: as a CircleMarker it shares
    # the SVG overlay pane with the listings, so draw order is the only thing
    # keeping it on top of them.
    if COMMUTE_DESTINATION:
        folium.CircleMarker(
            [COMMUTE_DESTINATION[1], COMMUTE_DESTINATION[0]], radius=11,
            color="white", weight=2.5,
            fill=True, fill_color="#CC3311", fill_opacity=0.95,  # palette vermillion
            tooltip=f"{COMMUTE_DESTINATION_NAME} — commute destination",
        ).add_to(m)

    # Legend keys are built to match what this profile actually shows — a
    # "Bike route to Caltrain" key on a map with no bike routes is just noise.
    #
    # Each key is an inline-block, and the container wraps them. The old version
    # was one <div> per key at line-height 2, which on a phone was a column
    # eight rows tall sitting on top of a 500px map — the legend was winning
    # against the thing it describes. Wrapping puts the same keys in two or
    # three lines at any width, and the prose that used to live down here has
    # moved to the subtitle above the map, where it can use the full page width
    # instead of overlaying the map at max-width:230px.
    # The container is a real flex row, not inline-blocks in a text flow. Inline
    # boxes only break at a whitespace opportunity, and these are join()ed with
    # nothing between them — so the first attempt didn't wrap at all, it just
    # ran off the right edge and clipped "Newest" in half.
    def _key(swatch: str, label: str) -> str:
        return (
            '<span style="display:flex;align-items:center;white-space:nowrap;">'
            f'{swatch}<span style="margin-left:4px;">{label}</span></span>'
        )

    legend_rows = []
    if COMMUTE_DESTINATION:
        legend_rows.append(_key(
            '<svg width="16" height="16" style="display:block;">'
            '<circle cx="8" cy="8" r="6.5" fill="#CC3311" stroke="white" stroke-width="2"/>'
            '</svg>',
            _html.escape(COMMUTE_DESTINATION_NAME),
        ))
    if HAS_BIKE_TIMES:
        legend_rows += [
            _key(
                '<svg width="14" height="14" style="display:block;">'
                f'<circle cx="7" cy="7" r="5.5" fill="{_CALTRAIN_COLOR}" stroke="white" stroke-width="1.5"/>'
                '</svg>', "Caltrain"),
            _key(
                '<svg width="14" height="14" style="display:block;">'
                f'<circle cx="7" cy="7" r="5.5" fill="{_BART_COLOR}" stroke="white" stroke-width="1.5"/>'
                '</svg>', "BART"),
            _key(
                '<svg width="20" height="8" style="display:block;">'
                f'<line x1="0" y1="4" x2="20" y2="4" stroke="{_CALTRAIN_COLOR}" stroke-width="2.5" '
                'stroke-dasharray="8 6" opacity="0.75"/></svg>', "Bike→Caltrain"),
            _key(
                '<svg width="20" height="8" style="display:block;">'
                f'<line x1="0" y1="4" x2="20" y2="4" stroke="{_BART_COLOR}" stroke-width="2.5" '
                'stroke-dasharray="2 6" opacity="0.75"/></svg>', "Bike→BART"),
        ]
    # The three real modes are always listed once this profile draws transit at
    # all, whether or not today's listings happen to include one of each. They
    # are the fixed vocabulary of the map, so a stable legend is easier to learn
    # than one that changes shape daily -- and a missing BART key used to be
    # indistinguishable from "no BART key exists".
    #
    # "Other transit" stays conditional: it is a fallback, not a mode, so it
    # earns a key only on a map that actually has one. After the geometry
    # backfill (transit_commute.py --dry-run) it should be rare.
    _always = {"bart", "muni", "bus"} if transit_modes_drawn else set()
    _seen_labels = set()
    for _mode in _TRANSIT_LEGEND_ORDER:
        if _mode not in transit_modes_drawn and _mode not in _always:
            continue
        _st = _TRANSIT_STYLE[_mode]
        # "rail" and "other" deliberately share a label and colour, so the
        # legend must not print the same key twice.
        if _st["label"] in _seen_labels:
            continue
        _seen_labels.add(_st["label"])
        # Taller/wider than the bike keys so a 4px stroke plus its casing fits,
        # and drawn with the casing so the key looks like the thing on the map.
        legend_rows.append(_key(
            '<svg width="24" height="12" style="display:block;">'
            f'<line x1="0" y1="6" x2="24" y2="6" stroke="{_TRANSIT_CASING["color"]}" '
            f'stroke-width="{_st["weight"] + _TRANSIT_CASING["extra_weight"]}"/>'
            f'<line x1="0" y1="6" x2="24" y2="6" stroke="{_st["color"]}" '
            f'stroke-width="{_st["weight"]}" stroke-dasharray="{_st["dash"]}" '
            f'opacity="{_TRANSIT_OPACITY}"/></svg>',
            _st["label"],
        ))
    # The prose that used to sit here ("routes are drawn only for…") is now in
    # the map subtitle — see _map_subtitle(). It was three wrapped lines of grey
    # text inside a floating panel, which is the most expensive place on the
    # page to put a sentence.
    legend_rows += [
        _key(
            '<svg width="14" height="14" style="display:block;">'
            '<circle cx="7" cy="7" r="5.5" fill="#3b82f6" stroke="white" stroke-width="1.5"/>'
            # "shown" rather than a bare "Newest": the highlight tracks the
            # dropdowns, so on a filtered map these are the newest of the
            # matches, not the newest on the map.
            '</svg>', "3 newest shown"),
        _key(
            '<svg width="14" height="14" style="display:block;">'
            '<circle cx="7" cy="7" r="4.5" fill="#9ca3af" stroke="white" stroke-width="1.5"/>'
            '</svg>', "Listing"),
    ]
    # Only worth a key where the faded dots actually exist. On a profile whose
    # every listing is measured this would be a key for an empty set.
    if HAS_COMMUTE and COMMUTE_DESTINATION:
        legend_rows.append(_key(
            '<svg width="14" height="14" style="display:block;">'
            '<circle cx="7" cy="7" r="4.5" fill="#9ca3af" fill-opacity="0.28" '
            'stroke="white" stroke-width="1.5" stroke-opacity="0.45"/></svg>',
            "Commute unknown",
        ))

    # Controls are a horizontal strip, not a stacked panel. Stacked — label on
    # its own line above a width:100% select, three times over — the box was
    # 140x190px, which on a 350px-wide phone map covered the whole top-right
    # quadrant and had become a bigger obstruction than the legend it was
    # sharing the map with. Inline label+select pairs in a wrapping flex row
    # hold the same three controls in one or two lines.
    _SELECT_CSS = (
        "font-size:12px;border:1px solid #d1d5db;border-radius:5px;"
        "padding:1px 4px;background:#fff;cursor:pointer;max-width:110px;"
    )

    def _control(label: str, el_id: str, options: str) -> str:
        return (
            '<span style="display:flex;align-items:center;gap:4px;">'
            f'<label for="{el_id}" style="font-weight:600;color:#1a1a2e;">{label}</label>'
            f'<select id="{el_id}" style="{_SELECT_CSS}">{options}</select></span>'
        )

    # The commute dropdown only appears when there are commute numbers to filter
    # on, so it can never sit there looking broken.
    has_commute_data = any(f["commute"] is not None for f in filterable)
    if has_commute_data:
        budget = COMMUTE_MAX_MINUTES
        commute_control = _control("Commute", "commute-filter", (
            '<option value="all">Any</option>'
            f'<option value="{budget}">&lt;{budget} min</option>'
            f'<option value="{int(budget * 2 / 3)}">&lt;{int(budget * 2 / 3)} min</option>'
            f'<option value="{int(budget / 2)}">&lt;{int(budget / 2)} min</option>'
        ))
    else:
        commute_control = ""

    # Price tiers are derived from the profile's own ceiling rather than
    # hard-coded, so a profile that tops out at $2,800 doesn't get a useless
    # "Under $4,000" option. digest_max_price is the stated ceiling; max_price
    # is the wider search bound, used only when there's no digest ceiling set.
    # Steps are rounded to $250 so the labels read like round numbers.
    _price_ceiling = digest_max_price or max_price
    price_control = ""
    if _price_ceiling:
        _tiers, _seen = [], set()
        for _frac in (1.0, 0.875, 0.75):
            _t = int(round(_price_ceiling * _frac / 250.0) * 250)
            if _t > 0 and _t not in _seen:
                _seen.add(_t)
                _tiers.append(_t)
        _opts = "".join(
            f'<option value="{t}">&lt;${t:,}</option>' for t in _tiers
        )
        price_control = _control(
            "Price", "price-filter", '<option value="all">Any</option>' + _opts
        )

    # Built outside the f-string below: the layer names are raw JS identifiers,
    # not JSON, so they have to be interpolated rather than serialised.
    listings_js = "[" + ",".join(
        "{{vars:[{v}],dot:{d},ts:{t},named:{n},beds:{b},commute:{c},price:{p}}}".format(
            v=",".join(f["vars"]),
            d=f["dot"],
            t=f["ts"],
            n="true" if f["named"] else "false",
            b=f["beds"] if f["beds"] is not None else "null",
            c=f["commute"] if f["commute"] is not None else "null",
            p=f["price"] if f["price"] is not None else "null",
        )
        for f in filterable
    ) + "]"

    m.get_root().html.add_child(folium.Element(f"""
        <!-- max-width rather than a right edge, so the strip hugs its keys on a
             desktop map and only spans the width when it has to wrap.
             bottom:22px and z-index:1001 keep it clear of Leaflet's attribution
             bar, which is z-index 1000 with a translucent white background and
             wraps to two lines on a phone — at 999 and bottom:8px it painted
             over the legend's last row, which on this map is the "Commute
             unknown" key the whole faded-dot change depends on. -->
        <div style="position:fixed;bottom:22px;left:8px;max-width:calc(100% - 16px);
                    z-index:1001;display:flex;flex-wrap:wrap;gap:3px 12px;
                    background:rgba(255,255,255,0.93);padding:6px 9px;
                    border-radius:8px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;
                    font-size:11px;color:#1a1a2e;
                    box-shadow:0 2px 8px rgba(0,0,0,0.13);">
          {''.join(legend_rows)}
        </div>

        <!-- left:52px clears Leaflet's zoom buttons, which are the one thing on
             the map that must never be covered. -->
        <div style="position:fixed;top:8px;left:52px;max-width:calc(100% - 60px);
                    z-index:999;
                    display:flex;flex-wrap:wrap;align-items:center;gap:5px 12px;
                    background:rgba(255,255,255,0.95);padding:6px 9px;
                    border-radius:8px;font-family:-apple-system,BlinkMacSystemFont,sans-serif;
                    font-size:12px;box-shadow:0 2px 8px rgba(0,0,0,0.13);">
          {_control("Beds", "br-filter",
                    '<option value="all">All</option>'
                    '<option value="1">1 BR</option>'
                    '<option value="2">2 BR</option>'
                    '<option value="3">3+ BR</option>')}
          {price_control}
          {commute_control}
          <span id="filter-count" style="color:#6b7280;font-size:11px;"></span>
        </div>

        <script>
          // Injected into the document body, which folium renders BEFORE the
          // script section holding the map and every layer variable. Running
          // this immediately threw a ReferenceError on the first layer name,
          // which killed the whole block: the dropdowns rendered but changing
          // them did nothing. Waiting for load is what makes them live.
          window.addEventListener('load', function() {{
            var group    = {fg_listings.get_name()};
            var listings = {listings_js};

            function apply() {{
              var bedSel = document.getElementById('br-filter').value;
              var cmtEl  = document.getElementById('commute-filter');
              var cmtSel = cmtEl ? cmtEl.value : 'all';
              var prcEl  = document.getElementById('price-filter');
              var prcSel = prcEl ? prcEl.value : 'all';
              var shown  = 0;

              listings.forEach(function(item) {{
                var ok = true;

                if (bedSel !== 'all') {{
                  var want = parseInt(bedSel, 10);
                  // "3" means 3 or more; anything with no bedroom count is
                  // hidden once a specific size is asked for.
                  ok = item.beds !== null &&
                       (want === 3 ? item.beds >= 3 : item.beds === want);
                }}

                if (ok && prcSel !== 'all') {{
                  // Unlike commute, a missing price hides the listing. The
                  // commute figure is one we failed to compute; a missing price
                  // is one the poster never gave, and "under $3,500" is not a
                  // claim we can make about it.
                  ok = item.price !== null &&
                       item.price <= parseInt(prcSel, 10);
                }}

                if (ok && cmtSel !== 'all') {{
                  // Listings with no commute figure stay visible: a missing
                  // measurement isn't evidence of a bad commute.
                  ok = item.commute === null ||
                       item.commute <= parseInt(cmtSel, 10);
                }}

                if (ok) shown++;
                item.ok = ok;
                item.vars.forEach(function(layer) {{
                  if (ok && !group.hasLayer(layer))  group.addLayer(layer);
                  if (!ok && group.hasLayer(layer))  group.removeLayer(layer);
                }});
              }});

              // The highlight is recomputed here rather than baked in at build
              // time, so "the 3 newest" means the 3 newest of what you actually
              // asked for. Pinned to the whole map it was near useless once a
              // filter was on: narrow to 2BR under $4k and the blue dots were
              // usually three listings the filter had just hidden, leaving the
              // surviving set with no recency signal at all.
              //
              // `named` is a preference, not a filter. It carries over the
              // build-time bias toward listings in a named neighborhood — a
              // highlight spent on the catch-all or "Way Out There" bucket is
              // a highlight wasted — but as a hard requirement it emptied the
              // highlight outright on narrow filters: only a fifth of markers
              // land in a named hood, and e.g. 3BR has none at all, so that
              // filter lit up nothing. Ranking named first and backfilling
              // keeps the bias where there's a choice and still gives a
              // recency signal where there isn't. Unfiltered, there are always
              // more than three named, so the default map is unchanged.
              var hot = listings.filter(function(item) {{ return item.ok; }});
              hot.sort(function(a, b) {{
                if (a.named !== b.named) return a.named ? -1 : 1;
                return b.ts - a.ts;
              }});
              hot = hot.slice(0, 3);

              listings.forEach(function(item) {{
                var isNew = hot.indexOf(item) !== -1;
                // setStyle with fillColor alone on purpose: fillOpacity is
                // carrying the separate "commute never measured" fade, and
                // restating it here would burn those dots back to full.
                item.dot.setStyle({{fillColor: isNew ? '{_NEW_COLOR}' : '{_OLD_COLOR}'}});
                item.dot.setRadius(isNew ? 6 : 4);
                // Same SVG pane as every other dot, so without this a fresh
                // listing drawn early sits under the gray ones around it.
                if (isNew) item.dot.bringToFront();
              }});

              document.getElementById('filter-count').textContent =
                shown + ' of ' + listings.length + ' shown';
            }}

            document.getElementById('br-filter').addEventListener('change', apply);
            var c = document.getElementById('commute-filter');
            if (c) c.addEventListener('change', apply);
            var p = document.getElementById('price-filter');
            if (p) p.addEventListener('change', apply);
            apply();
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

  /* ── Chart Grid ──
     The last row holds whichever of commute / transit / bike / bart this
     profile actually has data for, so it's an auto-flow row rather than a
     named area — a named area for a chart that isn't rendered leaves a gap.

     Keep this order and the card order in the markup in sync. Mobile drops to
     flex column, which follows DOM order and ignores the areas entirely, so
     reordering only here silently reorders the desktop view alone. */
  .grid {
    display: grid;
    gap: 16px;
    grid-template-columns: 1fr 1fr;
    grid-template-areas:
      "map    map"
      "time   time"
      "box    box"
      "heat   heat"
      "brbath hist"
      "scatter count";
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
<!--    <p>Historical scraped data. Listings under $2,100/mo excluded.</p> -->
</header>

<div class="cards" id="cards"></div>

<div class="grid">
  <div class="chart-card area-map" style="padding:12px 14px 10px;">
    <div style="font-size:15px;font-weight:700;color:#1a1a2e;">
      Where the Last 3 Days Landed
    </div>
    <!-- Its own block rather than trailing the heading inline: it now carries
         the map's rules (which listings get a route, what a faded dot means),
         which is more text than fits beside a title on a phone. -->
    <div style="font-size:11px;font-weight:400;color:#6b7280;line-height:1.5;
                margin:3px 0 8px;max-width:70ch;">__MAP_SUBTITLE__</div>
    __MAP_IFRAME__
  </div>
  __TIME_SLOT__
  <div class="chart-card area-box">
    <div class="plotly-chart" id="chart-box"></div>
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
  __TRANSIT_SLOTS__
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
__TRANSIT_JS__
</script>
</body>
</html>"""


# ── HTML Assembly ─────────────────────────────────────────────────────────────

def _map_subtitle() -> str:
    """What the map is actually showing, for this profile.

    Built here rather than hardcoded in the template: the old copy promised
    "bike times to Caltrain & BART" on every profile, including ones that do no
    bike routing at all, and never mentioned the transit routes — which by then
    were the most prominent thing on the map.

    This is also where the map's two non-obvious rules are explained, both moved
    out of the floating legend. Prose belongs in the page flow: down there it
    was a 230px-wide grey block sitting on the map on a phone, and it is the
    part a reader needs exactly once.
    """
    import html as _html

    # Sentences rather than a separator character: these are whole clauses, and
    # one of them already carries its own commas, so any glyph between them just
    # competed with the punctuation inside them.
    bits = ["Hover a polygon for price stats", "click a dot to open the listing"]
    if HAS_COMMUTE and COMMUTE_DESTINATION:
        bits.append(
            "Dashed lines are the transit trip to "
            f"{_html.escape(COMMUTE_DESTINATION_NAME)}, colored by mode — drawn "
            f"only for shortlist candidates ({TRANSIT_ROUTE_MIN_BEDROOMS}BR or "
            f"larger, under ${TRANSIT_ROUTE_MAX_PRICE:,}, under "
            f"{TRANSIT_ROUTE_MAX_MINUTES} min), with walking legs left off"
        )
        # The single most misreadable thing on the map, so it gets its own
        # sentence rather than a parenthetical: most of the west side has never
        # been measured, and a faded dot surviving the commute filter is not the
        # same claim as a short commute.
        bits.append(
            "Faded dots have no commute time — they were never measured, not "
            "measured and found close"
        )
    if HAS_BIKE_TIMES:
        bits.append("Dotted lines are bike routes to Caltrain &amp; BART")
    # The first two are one sentence; anything after is its own.
    head = ", ".join(bits[:2])
    return ". ".join([head, *bits[2:]]) + "."


def build_html(df: pd.DataFrame, historical: pd.DataFrame | None = None) -> str:
    time_chart = chart_price_over_time(df, historical)
    if time_chart:
        time_slot = (
            '<div class="chart-card area-time">'
            '<div class="plotly-chart" id="chart-time"></div></div>'
        )
        time_js = f'renderChart("chart-time", {json.dumps(time_chart)});'
    else:
        time_slot = ""
        time_js   = ""

    # Each of these four is optional and independent: a profile that does
    # commutes but no bike routing renders two of them, and vice versa.
    optional_slots, optional_js = [], []
    for area, chart_id, spec in (
        ("commute", "chart-commute", chart_commute_times(df)),
        ("transit", "chart-transit", chart_transit_quality(df)),
        ("bike",    "chart-bike",    chart_bike_times(df)),
        ("bart",    "chart-bart",    chart_bart_bike_times(df)),
    ):
        if not spec:
            continue
        optional_slots.append(
            f'<div class="chart-card area-{area}">'
            f'<div class="plotly-chart" id="{chart_id}"></div></div>'
        )
        optional_js.append(f'renderChart("{chart_id}", {json.dumps(spec)});')

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
    html = html.replace("__MAP_SUBTITLE__",  _map_subtitle())
    html = html.replace("__TIME_SLOT__",     time_slot)
    html = html.replace("__TIME_JS__",       time_js)
    html = html.replace("__TRANSIT_SLOTS__", "\n  ".join(optional_slots))
    html = html.replace("__TRANSIT_JS__",    "\n".join(optional_js))
    return html


# ── Entry Point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Listings HTML dashboard")
    parser.add_argument("--no-html", action="store_true", help="Terminal summary only")
    parser.add_argument("--open",    action="store_true", help="Open dashboard in browser")
    add_profile_arg(parser)
    args = parser.parse_args()

    print(f"Profile: {PROFILE_NAME} ({DISPLAY_NAME})")
    df = load_data()
    print_terminal_summary(df)

    historical = load_historical(df)
    if not historical.empty:
        print(f"Historical layer: {len(historical):,} archived listings "
              f"(median ${historical['price'].median():,.0f})")

    if not args.no_html:
        html = build_html(df, historical)
        OUTPUT_HTML.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_HTML.write_text(html, encoding="utf-8")
        print(f"Dashboard saved → {OUTPUT_HTML}")
        if args.open:
            import subprocess, platform
            opener = "open" if platform.system() == "Darwin" else "xdg-open"
            subprocess.run([opener, str(OUTPUT_HTML)], check=False)


if __name__ == "__main__":
    main()
