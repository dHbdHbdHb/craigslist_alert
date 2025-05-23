import os
import datetime
from zoneinfo import ZoneInfo
import pandas as pd
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.image import MIMEImage
import folium
from folium.features import DivIcon
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
import time
import openrouteservice
from neighborhoods.neighborhood_shapes import neighborhood_shapes

# ----- Configurable -----
GMAIL_ADDRESS = "hillsbunnell@gmail.com"
GMAIL_APP_PASSWORD = "eknq yzlh jkop vkdg" # https://myaccount.google.com/apppasswords
RECIPIENT_EMAIL = "hillsbunnell@gmail.com"  # Could add more addresses
BASE_DIR = os.path.expanduser("~/craigslist_alert")
ACTIVE_PATH  = os.path.join(BASE_DIR, "craigslist_data", "listings_active.csv")
LAST_DIGEST_FILE = os.path.join(BASE_DIR, "last_digest_date.txt")

ORS_API_KEY = '5b3ce3597851110001cf624809183d29fbaa46ecb0f48f56e62f89cb'  # OpenRouteService API key https://account.heigit.org/manage/key
CALTRAIN_COORDS = [-122.3942, 37.7763]  # lon, lat
CHROMEDRIVER_PATH = '/usr/bin/chromedriver'

# Priority & Digest criteria...
priority_neighborhoods = {"Mission", "Duboce"}
priority_max_price = 3700
priority_min_bathrooms = 2

# Debug: show working directory and paths
print(f"DEBUG: cwd = {os.getcwd()}")
print(f"DEBUG: ACTIVE_PATH = {ACTIVE_PATH}")
print(f"DEBUG: LAST_DIGEST_FILE = {LAST_DIGEST_FILE}")

def send_email(msg: MIMEMultipart):
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def build_map_png(listings, map_html="email_map.html", map_png="email_map.png"):
    ors = openrouteservice.Client(key=ORS_API_KEY)
    lats = [r['lat'] for r in listings] + [CALTRAIN_COORDS[1]]
    lons = [r['lon'] for r in listings] + [CALTRAIN_COORDS[0]]
    center = [sum(lats)/len(lats), sum(lons)/len(lons)]
    m = folium.Map(location=center, zoom_start=14, tiles="CartoDB positron")
    folium.CircleMarker(
        [CALTRAIN_COORDS[1], CALTRAIN_COORDS[0]], radius=6,
        color='#D99441', fill=True, fill_color='#D99441'
    ).add_to(m)
    for pt in listings:
        coords = [(pt['lon'], pt['lat']), (CALTRAIN_COORDS[0], CALTRAIN_COORDS[1])]
        route = ors.directions(coords, profile='cycling-regular', format='geojson')
        geom = route['features'][0]['geometry']['coordinates']
        duration = int(route['features'][0]['properties']['summary']['duration'] / 60)
        folium.PolyLine(
            [(c[1], c[0]) for c in geom], color='#A67D4B', weight=4, opacity=0.7
        ).add_to(m)
        folium.CircleMarker(
            [pt['lat'], pt['lon']], radius=6, color='#262312', fill=True, fill_color='#262312'
        ).add_to(m)
        folium.map.Marker(
            [pt['lat'], pt['lon']],
            icon=DivIcon(html=f"""
                <div style="font-family:'Helvetica Neue',Arial,sans-serif;
                             font-size:16px;color:#262312;
                             background:rgba(197,217,213,0.9);
                             padding:8px 14px;border-radius:6px;
                             text-align:center;font-weight:bold; min-width:50px;">
                  {duration} min
                </div>
            """)
        ).add_to(m)
    m.save(map_html)
    options = Options()
    options.add_argument('--headless=new')
    options.add_argument('--no-sandbox')
    options.add_argument('--disable-dev-shm-usage')
    service = Service(CHROMEDRIVER_PATH)
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_window_size(1200, 900)
    driver.get('file://' + os.path.abspath(map_html))
    time.sleep(4)
    driver.save_screenshot(map_png)
    driver.quit()
    return map_png


def main():
    # Load active listings
    df = pd.read_csv(ACTIVE_PATH)
    print("DEBUG: initial alerted counts =", df['alerted'].value_counts(dropna=False).to_dict())
    df = df[df['alerted'] == False]
    print("DEBUG: unalerted rows after filter =", len(df))

    # --- Send priority single emails ---
    # --- Priority alerts ---
    def hood_match(row):
        if pd.isnull(row['neighborhoods']): return False
        return any(h in priority_neighborhoods for h in row['neighborhoods'].split(','))

    priority_mask = (
        df.apply(hood_match, axis=1) &
        (df['price'] <= priority_max_price) &
        (df['num_bathrooms'] >= priority_min_bathrooms)
    )
    df_priority = df[priority_mask]
    print("DEBUG: priority listings count =", len(df_priority))

    if not df_priority.empty:
        df.loc[df_priority.index, 'alerted'] = True
        print("DEBUG: alerted counts after priority =", df['alerted'].value_counts(dropna=False).to_dict())
        df.to_csv(ACTIVE_PATH, index=False)

        for _, row in df_priority.iterrows():
            # send priority email
            for _, row in df_priority.iterrows():
                subject = f"🚨 New High-Priority Craigslist Listing: {row['title'][:40]}"
                html_body = f"""
                <html><body style="font-family:'Helvetica Neue',Arial,sans-serif;">
                <h2 style="color:#262312; margin-bottom:10px;">{row['title']}</h2>
                <div style="margin-bottom:8px;"><strong>Neighborhoods:</strong> {row['neighborhoods']}</div>
                <div style="margin-bottom:8px;"><strong>Price:</strong> ${row['price']}</div>
                <div style="margin-bottom:8px;"><strong>Beds/Baths:</strong> {row['num_bedrooms']}/{row['num_bathrooms']}</div>
                <div style="margin-bottom:8px;"><strong>City:</strong> {row['city']}</div>
                <div style="margin-bottom:8px;"><strong>Posted:</strong> {row['time_posted']}</div>
                <div style="margin-bottom:8px;"><strong>URL:</strong> <a href="{row['url']}" style="color:#A67D4B;">View Listing</a></div>
                <div style="margin-bottom:8px;"><strong>Location:</strong> ({row['lat']}, {row['lon']})</div>
                </body></html>
                """
                msg = MIMEMultipart('alternative')
                msg['From'] = GMAIL_ADDRESS
                msg['To'] = RECIPIENT_EMAIL
                msg['Subject'] = subject
                msg.attach(MIMEText(html_body, 'html'))
                send_email(msg)
                print(f"Sent single alert: {row['title'][:40]}")
            pass
    
    # --- Digest by neighborhood (once a day) ---
    try:
        with open(LAST_DIGEST_FILE, 'r') as f:
            last_date = f.read().strip()
    except FileNotFoundError:
        last_date = None
    today = datetime.datetime.now(ZoneInfo("America/Los_Angeles")).date()
    today_str = today.isoformat()
    send_digest = (last_date != today_str)
    print(f"DEBUG: last_date={last_date}, today_str={today_str}, send_digest={send_digest}")

    if send_digest:
        df = pd.read_csv(ACTIVE_PATH)
        df = df[df['alerted'] == False]
        df_digest = df[df.apply(lambda r: not pd.isnull(r['neighborhoods']) and any(h in neighborhood_shapes for h in r['neighborhoods'].split(',')), axis=1)]
        print("DEBUG: digest listings count =", len(df_digest))
        if not df_digest.empty:
            listings = df_digest.to_dict('records')
            map_png = build_map_png(listings)
            msg = MIMEMultipart('related')
            msg['From'] = GMAIL_ADDRESS
            msg['To'] = RECIPIENT_EMAIL
            subject_date = today.strftime('%B %d')
            msg['Subject'] = f"Craigslist Daily Digest, {subject_date}"

            # ---- Group by neighborhood ----
            hood_to_listings = {hood: [] for hood in neighborhood_shapes.keys()}
            for row in listings:
                # row['neighborhoods'] could be "Mission,Duboce"
                hoods = [h.strip() for h in (row.get('neighborhoods') or '').split(',') if h.strip() in neighborhood_shapes]
                for hood in hoods:
                    hood_to_listings[hood].append(row)

            # ---- Build HTML ----
            html = '<html><body style="font-family:Arial,sans-serif; margin:0; padding:10px;">'
            html += '<h2 style="color:#262312;">New Craigslist Listings by Neighborhood</h2>'

            for hood, hood_listings in hood_to_listings.items():
                if not hood_listings:
                    continue
                html += f"<h3 style='margin:20px 0 6px 0; color:#D99441;'>{hood}</h3>"
                for row in hood_listings:
                    html += (
                        "<div style='border:1px solid #262312; border-radius:6px; "
                        "padding:8px; margin-bottom:8px;'>"
                        f"<div style='font-weight:bold; color:#262312;'>{row['title']}</div>"
                        f"<div style='color:#A8BFB9;'>${row['price']} &nbsp; {row['num_bedrooms']}bd/{row['num_bathrooms']}ba</div>"
                        f"<div><a href='{row['url']}' style='color:#A67D4B;'>View Listing</a></div>"
                        "</div>"
                    )
            html += (
                "<div><img src='cid:mapimage' "
                "style='width:100%;max-width:800px;border-radius:8px;' /></div>"
            )
            html += '</body></html>'

            body = MIMEMultipart('alternative')
            body.attach(MIMEText(html, 'html'))
            msg.attach(body)

            with open(map_png, 'rb') as f:
                img = MIMEImage(f.read())
                img.add_header('Content-ID', '<mapimage>')
                msg.attach(img)
            send_email(msg)
            print('Sent digest email.')
            df_all = pd.read_csv(ACTIVE_PATH)
            df_all.loc[df_all.index.isin(df_digest.index), 'alerted'] = True
            df_all.to_csv(ACTIVE_PATH, index=False)
            with open(LAST_DIGEST_FILE, 'w') as f:
                f.write(today_str)
            print(f"DEBUG: flagged {len(df_digest)} digest listings and updated LAST_DIGEST_FILE")

if __name__ == '__main__':
    main()
