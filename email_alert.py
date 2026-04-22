"""
craigslist_alert_robust.py

Two-phase alert system:
  1. Priority alerts  — individual emails sent immediately when a new listing
                        matches priority neighborhoods, price, and bathroom criteria.
  2. Daily digest     — one email per day grouping all new unalerted listings by
                        neighborhood, with a biking-time map from Caltrain.

Runs via cron after each scraper run. Tracks sent alerts in listings_active.csv
('alerted' column) and the digest date in last_digest_date.txt.
"""

import argparse
import json
import os
import datetime
import time
from collections import deque
from zoneinfo import ZoneInfo
import pandas as pd
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
import openrouteservice
from neighborhoods.neighborhood_shapes import neighborhood_shapes

from config import (
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
    DIGEST_RECIPIENT_EMAILS, ALERT_RECIPIENT_EMAILS,
    DATA_ACTIVE, LAST_DIGEST_FILE,
    ORS_API_KEY, CALTRAIN_4TH_KING_COORDS, CALTRAIN_22ND_ST_COORDS,
    BART_STATIONS,
    priority_neighborhoods, priority_max_price, priority_min_price,
    priority_min_bathrooms, priority_min_posting_age_minutes, priority_scam_keywords,
    digest_min_price, digest_max_price, DASHBOARD_URL,
)

ACTIVE_PATH      = DATA_ACTIVE
BIKE_ROUTES_PATH = os.path.join(os.path.dirname(DATA_ACTIVE), "bike_routes.json")


# ──────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────

def send_email(msg: MIMEMultipart):
    with smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=30) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def is_listing_active(url: str) -> bool:
    """Return False if the listing has been flagged or deleted, True otherwise."""
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        flagged  = "This posting has been flagged for removal."
        deleted  = "This posting has been deleted by its author."
        return not (flagged in response.text or deleted in response.text)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 410:
            print(f"  Listing confirmed gone (410): {url}")
            return False
        return True  # unknown HTTP error — keep the listing
    except requests.RequestException as e:
        print(f"  Could not check listing ({e}): {url}")
        return True  # network error — keep the listing


def build_price_summary_html(df: pd.DataFrame) -> str:
    """
    Compact price-context tables appended to the digest email.
    Shows median rent by neighborhood and by BR/bath type, using
    all active listings above the $2,100 floor (same as the dashboard).
    """
    df = df.copy()
    df['price']         = pd.to_numeric(df['price'],         errors='coerce')
    df['num_bedrooms']  = pd.to_numeric(df['num_bedrooms'],  errors='coerce')
    df['num_bathrooms'] = pd.to_numeric(df['num_bathrooms'], errors='coerce')
    df = df[df['price'] >= 2100].dropna(subset=['price', 'num_bedrooms'])

    # Expand comma-separated neighborhoods into one row each
    df['neighborhoods'] = df['neighborhoods'].fillna('')
    rows = []
    for _, row in df.iterrows():
        hoods = [h.strip() for h in row['neighborhoods'].split(',') if h.strip()] or ['Way Out There']
        for hood in hoods:
            rows.append({
                'neighborhood':  hood,
                'price':         row['price'],
                'num_bedrooms':  row['num_bedrooms'],
                'num_bathrooms': row['num_bathrooms'],
            })
    if not rows:
        return ''
    exp = pd.DataFrame(rows)

    # ── Inline style constants (email-safe, no classes) ──
    S = {
        'section': 'margin:28px 0 10px 0;font-size:15px;font-weight:bold;color:#262312;border-bottom:2px solid #D99441;padding-bottom:4px;',
        'table':   'border-collapse:collapse;width:100%;max-width:580px;margin-bottom:8px;font-family:Arial,sans-serif;',
        'th':      'padding:7px 12px;background:#262312;color:#f5f0e8;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:0.04em;',
        'th_r':    'padding:7px 12px;background:#262312;color:#f5f0e8;font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:0.04em;text-align:right;',
        'td':      'padding:6px 12px;font-size:12px;color:#262312;border-bottom:1px solid #e8e2d8;',
        'td_r':    'padding:6px 12px;font-size:12px;color:#262312;border-bottom:1px solid #e8e2d8;text-align:right;',
        'td_alt':  'padding:6px 12px;font-size:12px;color:#262312;border-bottom:1px solid #e8e2d8;background:#faf8f5;',
        'td_alt_r':'padding:6px 12px;font-size:12px;color:#262312;border-bottom:1px solid #e8e2d8;background:#faf8f5;text-align:right;',
    }

    def td(val, right=False, alt=False):
        key = ('td_alt_r' if right else 'td_alt') if alt else ('td_r' if right else 'td')
        return f'<td style="{S[key]}">{val}</td>'

    html = f'<div style="{S["section"]}">Historical Price Context</div>'

    # Table 1 — by neighborhood (excluding Way Out There)
    known = exp[exp['neighborhood'] != 'Way Out There']
    if not known.empty:
        g = known.groupby('neighborhood')['price']
        stats = pd.DataFrame({
            'n':   g.count(),
            'med': g.median(),
            'min': g.min(),
            'max': g.max(),
        }).sort_values('n', ascending=False)

        html += (
            f'<table style="{S["table"]}">'
            f'<thead><tr>'
            f'<th style="{S["th"]}">Neighborhood</th>'
            f'<th style="{S["th_r"]}">Listings</th>'
            f'<th style="{S["th_r"]}">Median</th>'
            f'<th style="{S["th_r"]}">Range</th>'
            f'</tr></thead><tbody>'
        )
        for i, (hood, row) in enumerate(stats.iterrows()):
            alt = (i % 2 == 1)
            html += (
                f'<tr>'
                + td(hood, alt=alt)
                + td(int(row['n']), right=True, alt=alt)
                + td(f"${row['med']:,.0f}", right=True, alt=alt)
                + td(f"${row['min']:,.0f}–${row['max']:,.0f}", right=True, alt=alt)
                + '</tr>'
            )
        html += '</tbody></table>'

    # Table 2 — by BR/bath
    exp['br_bath'] = (
        exp['num_bedrooms'].astype(int).astype(str) + 'BR / '
        + exp['num_bathrooms'].fillna(0).astype(int).astype(str) + 'BA'
    )
    g2 = exp.groupby('br_bath')['price']
    stats2 = pd.DataFrame({'n': g2.count(), 'med': g2.median()}).sort_index()

    html += (
        f'<table style="{S["table"]}">'
        f'<thead><tr>'
        f'<th style="{S["th"]}">Type</th>'
        f'<th style="{S["th_r"]}">Listings</th>'
        f'<th style="{S["th_r"]}">Median</th>'
        f'</tr></thead><tbody>'
    )
    for i, (brt, row) in enumerate(stats2.iterrows()):
        alt = (i % 2 == 1)
        html += (
            f'<tr>'
            + td(brt, alt=alt)
            + td(int(row['n']), right=True, alt=alt)
            + td(f"${row['med']:,.0f}", right=True, alt=alt)
            + '</tr>'
        )
    html += '</tbody></table>'

    return html


_CALTRAIN_STATIONS = [
    ('4th & King', CALTRAIN_4TH_KING_COORDS),
    ('22nd St',    CALTRAIN_22ND_ST_COORDS),
]

BART_ROUTES_PATH = os.path.join(os.path.dirname(DATA_ACTIVE), "bart_bike_routes.json")

# ORS free tier: 40 req/min — shared tracker across both compute functions
_ors_call_times = deque()

def _ors_rate_limit():
    """Sleep if approaching the 40 req/min ORS limit."""
    now = time.time()
    while _ors_call_times and _ors_call_times[0] < now - 60:
        _ors_call_times.popleft()
    if len(_ors_call_times) >= 35:  # 5-req safety buffer
        wait = 60 - (now - _ors_call_times[0]) + 0.5
        print(f"    Rate limit approaching, sleeping {wait:.0f}s…")
        time.sleep(wait)
    _ors_call_times.append(time.time())


def _compute_cycling_times(listings, stations, cache_path, label) -> dict:
    """
    For each listing, find the closest station by cycling time via ORS.
    Skips listings already present in the cache or with missing coordinates.
    Individual ORS errors are caught so one bad route doesn't abort the run.
    Returns {url: {'minutes': int, 'station': str}}.
    """
    ors = openrouteservice.Client(key=ORS_API_KEY, timeout=15)

    try:
        with open(cache_path) as f:
            route_cache = json.load(f)
    except (FileNotFoundError, ValueError):
        route_cache = {}

    result = {}
    for pt in listings:
        url = pt['url']
        cached = route_cache.get(url)
        if cached and 'minutes' in cached and 'station' in cached:
            result[url] = {'minutes': cached['minutes'], 'station': cached['station']}
            continue

        lon, lat = pt.get('lon'), pt.get('lat')
        if not (pd.notna(lon) and pd.notna(lat)):
            print(f"  {label}: skipping {url[-30:]} (no coordinates)")
            continue

        best_minutes, best_station, best_geom = None, None, None
        for name, coords in stations:
            _ors_rate_limit()
            try:
                route    = ors.directions(
                    [(lon, lat), (coords[0], coords[1])],
                    profile='cycling-regular', format='geojson',
                )
                minutes  = int(route['features'][0]['properties']['summary']['duration'] / 60)
                geom_raw = route['features'][0]['geometry']['coordinates']
                if best_minutes is None or minutes < best_minutes:
                    best_minutes = minutes
                    best_station = name
                    best_geom    = [[c[1], c[0]] for c in geom_raw]
            except Exception as e:
                print(f"  {label} ORS error for {url[-30:]} → {name}: {e}")

        if best_minutes is None:
            print(f"  {label}: all routes failed for {url[-30:]}, skipping")
            continue

        result[url]      = {'minutes': best_minutes, 'station': best_station}
        route_cache[url] = {
            'minutes':  best_minutes,
            'station':  best_station,
            'geometry': best_geom,
        }
        print(f"  {label}: {url[-30:]} → {best_minutes} min to {best_station}")

    try:
        with open(cache_path, 'w') as f:
            json.dump(route_cache, f)
    except OSError as e:
        print(f"  {label}: could not write cache ({e})")

    return result


def compute_bike_times(listings) -> dict:
    return _compute_cycling_times(listings, _CALTRAIN_STATIONS, BIKE_ROUTES_PATH, 'Caltrain')


def compute_bart_bike_times(listings) -> dict:
    return _compute_cycling_times(listings, BART_STATIONS, BART_ROUTES_PATH, 'BART')


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Send housing alerts and digest email.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Simulate a full run: bypass the daily date check, build the map and "
             "compute bike times, but do not send any emails or write to any files.",
    )
    args = parser.parse_args()
    dry_run = args.dry_run

    if dry_run:
        print("[DRY RUN] No emails will be sent and no files will be modified.")

    df = pd.read_csv(ACTIVE_PATH)

    # Ensure alerted column exists and is boolean
    if 'alerted' not in df.columns:
        df['alerted'] = False
    df['alerted'] = df['alerted'].fillna(False).astype(bool)

    # Work only with listings not yet alerted
    unalerted = df[~df['alerted']].copy()
    print(f"Unalerted listings: {len(unalerted)}")

    # ── Phase 1: Priority alerts ──────────────────────────────────────────
    # Send an individual email for each new listing in a priority neighborhood
    # that meets price and bathroom thresholds.

    def in_priority_hood(row) -> bool:
        if pd.isnull(row['neighborhoods']):
            return False
        return any(h in priority_neighborhoods for h in row['neighborhoods'].split(','))

    def has_scam_title(row) -> bool:
        title = str(row.get('title', '')).lower()
        return any(kw in title for kw in priority_scam_keywords)

    def is_old_enough(row) -> bool:
        try:
            posted = pd.to_datetime(row['time_posted'], utc=True)
            age_minutes = (pd.Timestamp.now(tz='UTC') - posted).total_seconds() / 60
            return age_minutes >= priority_min_posting_age_minutes
        except Exception:
            return True  # if unparseable, don't block on age

    priority_mask = (
        unalerted.apply(in_priority_hood, axis=1) &
        (unalerted['price'] >= priority_min_price) &
        (unalerted['price'] <= priority_max_price) &
        (unalerted['num_bathrooms'] >= priority_min_bathrooms) &
        ~unalerted.apply(has_scam_title, axis=1) &
        unalerted.apply(is_old_enough, axis=1)
    )
    df_priority = unalerted[priority_mask].copy()

    if not df_priority.empty:
        df_priority['active'] = df_priority['url'].apply(is_listing_active)
        df_priority = df_priority[df_priority['active']].drop(columns='active')

    print(f"Priority listings to alert: {len(df_priority)}")

    for _, row in df_priority.iterrows():
        subject   = f"New High-Priority Listing: {row['title'][:50]}"
        html_body = f"""
        <html><body style="font-family:'Helvetica Neue',Arial,sans-serif;">
        <h2 style="color:#262312;">{row['title']}</h2>
        <div><strong>Neighborhoods:</strong> {row['neighborhoods']}</div>
        <div><strong>Price:</strong> ${row['price']}</div>
        <div><strong>Beds/Baths:</strong> {row['num_bedrooms']}/{row['num_bathrooms']}</div>
        <div><strong>Posted:</strong> {row['time_posted']}</div>
        <div><a href="{row['url']}" style="color:#A67D4B;">View Listing</a></div>
        </body></html>
        """
        if dry_run:
            print(f"  [DRY RUN] Would send priority alert: {subject}")
        else:
            msg = MIMEMultipart('alternative')
            msg['From']    = GMAIL_ADDRESS
            msg['To']      = ', '.join(ALERT_RECIPIENT_EMAILS)
            msg['Subject'] = subject
            msg.attach(MIMEText(html_body, 'html'))
            send_email(msg)
            print(f"  Sent priority alert: {row['title'][:50]}")

    # Mark priority listings as alerted and save (skipped in dry run)
    if not dry_run and not df_priority.empty:
        df.loc[df_priority.index, 'alerted'] = True
        df.to_csv(ACTIVE_PATH, index=False)

    # ── Phase 2: Daily digest ─────────────────────────────────────────────
    # Once per day, send a grouped digest of all new unalerted listings
    # that fall within any known neighborhood and are under the digest price cap.

    try:
        with open(LAST_DIGEST_FILE, 'r') as f:
            last_date = f.read().strip()
    except FileNotFoundError:
        last_date = None

    today     = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date()
    today_str = today.isoformat()

    if not dry_run and last_date == today_str:
        print("Digest already sent today, skipping.")
        return

    # Re-read from disk so priority alerts marked above are reflected
    # (in dry run the CSV wasn't written, so re-read is a no-op but harmless)
    df = pd.read_csv(ACTIVE_PATH)
    df['alerted'] = df['alerted'].fillna(False).astype(bool)
    unalerted = df[~df['alerted']].copy()

    def in_known_hood(s) -> bool:
        if not isinstance(s, str) or not s.strip():
            return False
        return any(h in neighborhood_shapes for h in (t.strip() for t in s.split(',')))

    digest_mask = (unalerted['price'] >= digest_min_price) & (unalerted['price'] <= digest_max_price) & unalerted['neighborhoods'].apply(in_known_hood)
    df_digest = unalerted[digest_mask].copy()

    if not df_digest.empty:
        df_digest['active'] = df_digest['url'].apply(is_listing_active)
        df_digest = df_digest[df_digest['active']].drop(columns='active')

    print(f"Digest listings: {len(df_digest)}")

    if df_digest.empty:
        print("No digest listings to send.")
        return

    listings       = df_digest.to_dict('records')
    bike_times     = compute_bike_times(listings)
    bart_bike_times = compute_bart_bike_times(listings)

    # Persist bike times back to the active CSV (always, even in dry run)
    if 'bike_time_minutes' not in df.columns:
        df['bike_time_minutes'] = None
    if 'bike_station' not in df.columns:
        df['bike_station'] = None
    if 'bart_bike_time_minutes' not in df.columns:
        df['bart_bike_time_minutes'] = None
    if 'bart_station' not in df.columns:
        df['bart_station'] = None
    for url, info in bike_times.items():
        df.loc[df['url'] == url, 'bike_time_minutes'] = info['minutes']
        df.loc[df['url'] == url, 'bike_station']      = info['station']
    for url, info in bart_bike_times.items():
        df.loc[df['url'] == url, 'bart_bike_time_minutes'] = info['minutes']
        df.loc[df['url'] == url, 'bart_station']           = info['station']
    df.to_csv(ACTIVE_PATH, index=False)
    print(f"  Saved bike times for {len(bike_times)} listings (Caltrain + BART).")

    # Group listings by neighborhood for the email body
    hood_to_listings = {hood: [] for hood in neighborhood_shapes}
    for row in listings:
        hoods = [h.strip() for h in (row.get('neighborhoods') or '').split(',') if h.strip() in neighborhood_shapes]
        for hood in hoods:
            hood_to_listings[hood].append(row)

    html = '<html><body style="font-family:Arial,sans-serif;margin:0;padding:10px;">'
    html += f'<h2 style="color:#262312;">New Listings — {today.strftime("%B %d")}</h2>'

    for hood, hood_listings in hood_to_listings.items():
        if not hood_listings:
            continue
        html += f"<h3 style='margin:20px 0 6px 0;color:#D99441;'>{hood}</h3>"
        for row in hood_listings:
            bike_info = bike_times.get(row.get('url'))
            bike_str  = (
                f" &nbsp;·&nbsp; {bike_info['minutes']} min to {bike_info['station']} Caltrain"
                if bike_info else ""
            )
            bart_info = bart_bike_times.get(row.get('url'))
            bart_str  = (
                f" &nbsp;·&nbsp; {bart_info['minutes']} min to {bart_info['station']} BART"
                if bart_info else ""
            )
            html += (
                "<div style='border:1px solid #262312;border-radius:6px;padding:8px;margin-bottom:8px;'>"
                f"<div style='font-weight:bold;color:#262312;'>{row['title']}</div>"
                f"<div style='color:#A8BFB9;'>${row['price']} &nbsp; {row['num_bedrooms']}bd/{row['num_bathrooms']}ba{bike_str}</div>"
                f"<div style='color:#A8BFB9;'>{bart_str}</div>"
                f"<div><a href='{row['url']}' style='color:#A67D4B;'>View Listing</a></div>"
                "</div>"
            )

    # Re-read full active CSV for historical price context (not just today's digest)
    df_all = pd.read_csv(ACTIVE_PATH)
    html += build_price_summary_html(df_all)

    dashboard_link = (
        f"<div style='margin:20px 0 4px 0;font-size:11px;color:#9ca3af;'>"
        f"<a href='{DASHBOARD_URL}' style='color:#9ca3af;'>price dashboard</a>"
        f"</div>"
    ) if DASHBOARD_URL else ""

    html += dashboard_link + "</body></html>"

    if dry_run:
        # Save the rendered HTML so you can open it in a browser and inspect it
        preview_path = os.path.join(os.path.dirname(ACTIVE_PATH), "digest_preview.html")
        with open(preview_path, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f"  [DRY RUN] Digest HTML saved for preview → {preview_path}")
        print(f"  [DRY RUN] Would send digest to: {', '.join(DIGEST_RECIPIENT_EMAILS)}")
        return

    msg = MIMEMultipart('alternative')
    msg['From']    = GMAIL_ADDRESS
    msg['To']      = ', '.join(DIGEST_RECIPIENT_EMAILS)
    msg['Subject'] = f"Housing Digest — {today.strftime('%B %d')}"
    msg.attach(MIMEText(html, 'html'))

    send_email(msg)
    print(f"Sent digest email with {len(df_digest)} listings.")

    # Mark digest listings as alerted and record today's date
    df.loc[df.index.isin(df_digest.index), 'alerted'] = True
    df.to_csv(ACTIVE_PATH, index=False)
    with open(LAST_DIGEST_FILE, 'w') as f:
        f.write(today_str)


if __name__ == '__main__':
    main()
