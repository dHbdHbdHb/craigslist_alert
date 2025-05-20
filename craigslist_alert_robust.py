import os
import datetime
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
DATA_ACTIVE  = os.path.join(BASE_DIR, "craigslist_data", "listings_active.csv")

ORS_API_KEY = '5b3ce3597851110001cf624809183d29fbaa46ecb0f48f56e62f89cb'  # OpenRouteService API key https://account.heigit.org/manage/key
CALTRAIN_COORDS = [-122.3942, 37.7763]  # lon, lat
CHROMEDRIVER_PATH = '/usr/bin/chromedriver'

# Priority & Digest criteria...
priority_neighborhoods = {"Mission", "Duboce"}
priority_max_price = 3700
priority_min_bathrooms = 2

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
    df = pd.read_csv(ACTIVE_PATH)
    df = df[df['alerted'] == False]

    # --- Send priority single emails ---
    def hood_match(row):
        if pd.isnull(row['neighborhoods']):
            return False
        return any(h in priority_neighborhoods for h in row['neighborhoods'].split(','))

    priority_mask = (
        df.apply(hood_match, axis=1) &
        (df['price'] <= priority_max_price) &
        (df['num_bathrooms'] >= priority_min_bathrooms)
    )
    df_priority = df[priority_mask]
    df.loc[df_priority.index, 'alerted'] = True

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

    # --- Digest by neighborhood (once a day) ---
    try:
        with open('last_digest_date.txt', 'r') as f:
            last_date = f.read().strip()
    except FileNotFoundError:
        last_date = None
    today_str = datetime.date.today().isoformat()
    send_digest = (last_date != today_str)

    if send_digest:
        def digest_hood_match(row):
            if pd.isnull(row['neighborhoods']): return False
            return any(h in neighborhood_shapes for h in row['neighborhoods'].split(','))
        df_digest = df[df.apply(digest_hood_match, axis=1)]
        if not df_digest.empty:
            listings = df_digest.to_dict('records')
            map_png = build_map_png(listings)
            msg = MIMEMultipart('related')
            msg['From'] = GMAIL_ADDRESS
            msg['To'] = RECIPIENT_EMAIL
            msg['Subject'] = 'Craigslist Daily Digest'
            # Build HTML with inline styles for Gmail
            html = '<html><body style="font-family:Arial,sans-serif; margin:0; padding:10px;">'
            html += '<h2 style="color:#262312;">New Craigslist Listings by Neighborhood</h2>'
            current_hood = None
            for row in listings:
                hoods = row.get('neighborhoods','')
                html += f"<div style='border:1px solid #262312; border-radius:6px; padding:8px; margin-bottom:8px;'>"
                html += f"<div style='font-weight:bold; color:#262312;'>{row['title']}</div>"
                html += f"<div style='color:#A8BFB9;'>${row['price']} &nbsp; {row['num_bedrooms']}bd/{row['num_bathrooms']}ba</div>"
                html += f"<div><a href='{row['url']}' style='color:#A67D4B;'>View Listing</a></div>"
                html += '</div>'
            html += f"<div><img src='cid:mapimage' style='width:100%;max-width:800px;border-radius:8px;' /></div>"
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
            with open('last_digest_date.txt', 'w') as f:
                f.write(today_str)

    df.to_csv(ACTIVE_PATH, index=False)

if __name__ == '__main__':
    main()
