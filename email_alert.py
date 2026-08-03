"""
craigslist_alert_robust.py

Two-phase alert system:
  1. Priority alerts  — individual emails sent immediately when a new listing
                        matches priority neighborhoods, price, and bathroom criteria.
  2. Daily digest     — one email per day grouping all new unalerted listings by
                        neighborhood, each with its door-to-door transit commute
                        (and cycling time to a station, for profiles that use it).

Runs via cron after each scraper run. Tracks sent alerts in listings_active.csv
('alerted' column) and the digest date in last_digest_date.txt.
"""

import argparse
import os
import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import requests
from config import (
    GMAIL_ADDRESS, GMAIL_APP_PASSWORD,
    DIGEST_RECIPIENT_EMAILS,
    DATA_ACTIVE, LAST_DIGEST_FILE,
    digest_min_price, digest_max_price,
    digest_min_posting_age_minutes, digest_scam_keywords,
    DASHBOARD_URL,
    INCLUDE_NEIGHBORHOODS, FILTER_BY_NEIGHBORHOOD,
    DISPLAY_NAME, PROFILE_NAME, add_profile_arg,
    HAS_COMMUTE, COMMUTE_MAX_MINUTES, COMMUTE_DESTINATION_NAME,
)
from transit_times import compute_bike_times, compute_bart_bike_times
from transit_commute import compute_commutes

ACTIVE_PATH      = DATA_ACTIVE

# Listings matching no polygon. Must stay in step with analyze_listings.
# CATCHALL_HOOD, or the digest and the dashboard disagree about where an
# unmatched listing went. It is deliberately not the name of any drawn shape.
CATCHALL_HOOD    = "Rest of City"


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
        hoods = [h.strip() for h in row['neighborhoods'].split(',') if h.strip()] or [CATCHALL_HOOD]
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

    # Table 1 — by neighborhood (excluding the catch-all bucket)
    known = exp[exp['neighborhood'] != CATCHALL_HOOD]
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
    add_profile_arg(parser)
    args = parser.parse_args()
    dry_run = args.dry_run

    print(f"Profile: {PROFILE_NAME} ({DISPLAY_NAME})")
    if dry_run:
        print("[DRY RUN] No emails will be sent and no files will be modified.")

    df = pd.read_csv(ACTIVE_PATH)

    # Ensure alerted column exists and is boolean
    if 'alerted' not in df.columns:
        df['alerted'] = False
    df['alerted'] = df['alerted'].fillna(False).astype(bool)

    # ── Daily digest ──────────────────────────────────────────────────────
    # Once per day, send a grouped digest of all new unalerted listings that
    # fall within a neighborhood this profile cares about and are inside the
    # digest price band.
    #
    # There used to be a second phase ahead of this one that emailed individual
    # listings the moment they appeared. It was removed: in practice almost
    # everything fresh enough to beat the digest was a scam, which is what the
    # keyword and posting-age filters below now catch instead.

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

    unalerted = df[~df['alerted']].copy()
    print(f"Unalerted listings: {len(unalerted)}")

    def has_scam_title(row) -> bool:
        title = str(row.get('title', '')).lower()
        return any(kw in title for kw in digest_scam_keywords)

    def is_old_enough(row) -> bool:
        """Give Craigslist's community flagging time to catch obvious scams."""
        try:
            posted = pd.to_datetime(row['time_posted'], utc=True)
            age_minutes = (pd.Timestamp.now(tz='UTC') - posted).total_seconds() / 60
            return age_minutes >= digest_min_posting_age_minutes
        except Exception:
            return True  # if unparseable, don't block on age

    if not unalerted.empty:
        scam_mask = unalerted.apply(has_scam_title, axis=1)
        young_mask = ~unalerted.apply(is_old_enough, axis=1)
        if scam_mask.any() or young_mask.any():
            print(f"  Filtered {int(scam_mask.sum())} scam-keyword and "
                  f"{int(young_mask.sum())} too-fresh listing(s).")
        unalerted = unalerted[~scam_mask & ~young_mask]

    def in_known_hood(s) -> bool:
        """True if the listing is in a neighborhood this profile cares about.

        Filters against the profile's include list rather than every shape we
        can identify, so two people sharing this Pi don't get each other's
        neighborhoods in their digests.
        """
        if not isinstance(s, str) or not s.strip():
            return False
        return any(h in INCLUDE_NEIGHBORHOODS for h in (t.strip() for t in s.split(',')))

    digest_mask = (
        (unalerted['price'] >= digest_min_price) &
        (unalerted['price'] <= digest_max_price)
    )
    # With `filter = false` the neighborhood list only decides grouping order.
    # The shapes cover a fraction of the city, so filtering on them would drop
    # most of what was scraped.
    if FILTER_BY_NEIGHBORHOOD:
        digest_mask &= unalerted['neighborhoods'].apply(in_known_hood)
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
    commutes       = compute_commutes(listings)

    # Persist bike and commute times back to the active CSV (always, even in dry run)
    # The text columns are created as object dtype on purpose. Read back from a
    # CSV where every value is empty, pandas types them float64, and writing a
    # station name or line list into one is an incompatible-dtype assignment --
    # a FutureWarning today, an error in a later pandas.
    _TEXT_COLS = ('bike_station', 'bart_station', 'transit_lines')
    for _col in ('bike_time_minutes', 'bike_station',
                 'bart_bike_time_minutes', 'bart_station',
                 'commute_minutes', 'commute_walk_minutes',
                 'commute_headway', 'transit_score', 'transit_lines'):
        if _col not in df.columns:
            df[_col] = None
        if _col in _TEXT_COLS and df[_col].isna().all():
            df[_col] = df[_col].astype(object)
    for url, info in bike_times.items():
        df.loc[df['url'] == url, 'bike_time_minutes'] = info['minutes']
        df.loc[df['url'] == url, 'bike_station']      = info['station']
    for url, info in bart_bike_times.items():
        df.loc[df['url'] == url, 'bart_bike_time_minutes'] = info['minutes']
        df.loc[df['url'] == url, 'bart_station']           = info['station']
    for url, info in commutes.items():
        row = df['url'] == url
        df.loc[row, 'commute_minutes']      = info['minutes']
        df.loc[row, 'commute_walk_minutes'] = info['walk_minutes']
        df.loc[row, 'commute_headway']      = info['worst_headway']
        df.loc[row, 'transit_score']        = info['transit_score']
        df.loc[row, 'transit_lines']        = ", ".join(info['lines'])
    df.to_csv(ACTIVE_PATH, index=False)
    print(f"  Saved bike times for {len(bike_times)} listings (Caltrain + BART).")
    if commutes:
        print(f"  Saved commute times for {len(commutes)} listings.")

    # Drop anything beyond the commute budget. Listings with no commute data —
    # routing failed, or no API key — are KEPT: a missing measurement is not
    # evidence of a bad commute, and silently hiding listings would be worse
    # than showing one that turns out to be too far.
    #
    # df_digest is narrowed alongside `listings` because it's what decides which
    # rows get stamped `alerted` at the end. Filtering only the list would mark
    # listings as sent that were never in the email.
    if HAS_COMMUTE and commutes:
        too_far = {
            url for url, info in commutes.items()
            if info['minutes'] > COMMUTE_MAX_MINUTES
        }
        if too_far:
            df_digest = df_digest[~df_digest['url'].isin(too_far)]
            listings  = [r for r in listings if r.get('url') not in too_far]
            print(f"  Dropped {len(too_far)} listing(s) over "
                  f"{COMMUTE_MAX_MINUTES} min from {COMMUTE_DESTINATION_NAME}.")
            if not listings:
                print("No digest listings within the commute budget.")
                return

    # Best transit first within each neighborhood — the whole point of scoring is
    # that the top of the email should be the places actually worth the trip.
    if commutes:
        listings.sort(
            key=lambda r: commutes.get(r.get('url'), {}).get('transit_score', -1),
            reverse=True,
        )

    if not DIGEST_RECIPIENT_EMAILS and not dry_run:
        print("No digest recipients configured (alerts.digest_to is empty) — "
              "nothing to send.")
        return

    # Group listings by neighborhood for the email body, in the order the
    # profile lists them — so the neighborhoods someone cares most about aren't
    # buried below ones they merely tolerate.
    # Listings matching none of the named shapes go in a bucket at the end
    # rather than vanishing — with `filter = false` that bucket is usually the
    # biggest one, since the shapes don't cover the whole city.
    OTHER_LABEL = CATCHALL_HOOD
    hood_to_listings = {hood: [] for hood in INCLUDE_NEIGHBORHOODS}
    hood_to_listings[OTHER_LABEL] = []
    for row in listings:
        # A listing that matched no shape has NaN here, not "". NaN is truthy,
        # so `or ''` doesn't catch it — check the type instead.
        raw = row.get('neighborhoods')
        raw = raw if isinstance(raw, str) else ''
        hoods = [h.strip() for h in raw.split(',')
                 if h.strip() in hood_to_listings]
        for hood in hoods:
            hood_to_listings[hood].append(row)
        if not hoods:
            hood_to_listings[OTHER_LABEL].append(row)

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

            # The commute line is the headline number for a transit-first search,
            # so it gets its own row with the lines and their frequency spelled
            # out — "38R every 6 min" is what makes a 35-minute trip tolerable.
            commute = commutes.get(row.get('url'))
            if commute:
                detail = []
                if commute.get('walk_minutes'):
                    detail.append(f"{commute['walk_minutes']} min walk to")
                if commute.get('lines'):
                    detail.append(" / ".join(commute['lines'][:3]))
                if commute.get('worst_headway'):
                    detail.append(f"every ~{commute['worst_headway']:.0f} min")
                commute_str = (
                    f"<div style='color:#2f6f4f;font-weight:bold;'>"
                    f"{commute['minutes']} min to {COMMUTE_DESTINATION_NAME}"
                    + (f" &nbsp;·&nbsp; {' &nbsp;·&nbsp; '.join(detail)}" if detail else "")
                    + "</div>"
                )
            else:
                commute_str = ""

            html += (
                "<div style='border:1px solid #262312;border-radius:6px;padding:8px;margin-bottom:8px;'>"
                f"<div style='font-weight:bold;color:#262312;'>{row['title']}</div>"
                f"<div style='color:#A8BFB9;'>${row['price']} &nbsp; {row['num_bedrooms']}bd/{row['num_bathrooms']}ba{bike_str}</div>"
                f"{commute_str}"
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
    msg['Subject'] = f"[{DISPLAY_NAME}] Housing Digest — {today.strftime('%B %d')}"
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
