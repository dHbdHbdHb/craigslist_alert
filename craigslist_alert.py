import pandas as pd
import datetime
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from neighborhood_shapes import neighborhood_shapes

# --- Config ---
GMAIL_ADDRESS = "hillsbunnell@gmail.com"
GMAIL_APP_PASSWORD = "eknq yzlh jkop vkdg" # https://myaccount.google.com/apppasswords
RECIPIENT_EMAIL = "hillsbunnell@gmail.com"  # Could add more addresses

ACTIVE_PATH = "craigslist_data/listings_active.csv"

# Criteria for HIGH-PRIORITY single email
priority_neighborhoods = {"Bernal", "Duboce", "Chill Mission"}
priority_max_price = 3600
priority_min_bathrooms = 2

# Digest filters (broader, or everything new)
digest_neighborhoods = set(neighborhood_shapes.keys())  # or just set() for everything

def send_email(subject, body, to_addr=RECIPIENT_EMAIL):
    msg = MIMEMultipart()
    msg['From'] = GMAIL_ADDRESS
    msg['To'] = to_addr
    msg['Subject'] = subject
    msg.attach(MIMEText(body, 'html'))
    with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.sendmail(GMAIL_ADDRESS, to_addr, msg.as_string())

def main():
    df = pd.read_csv(ACTIVE_PATH)
    df = df[df['alerted'] == False]  # Only unalerted

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

    # Mark priority listings alerted
    df.loc[df_priority.index, 'alerted'] = True

    for _, row in df_priority.iterrows():
        subject = f"🚨 New High-Priority Craigslist Listing: {row['title'][:40]}"
        body = f"""
        <h2>{row['title']}</h2>
        <p><b>Neighborhoods:</b> {row['neighborhoods']}</p>
        <p><b>Price:</b> ${row['price']}</p>
        <p><b>Beds/Baths:</b> {row['num_bedrooms']}/{row['num_bathrooms']}</p>
        <p><b>URL:</b> <a href="{row['url']}">{row['url']}</a></p>
        <p><b>City:</b> {row['city']}</p>
        <p><b>Posted:</b> {row['time_posted']}</p>
        <p><b>Location:</b> ({row['lat']}, {row['lon']})</p>
        """
        send_email(subject, body)
        print(f"Sent single alert: {row['title'][:40]}")

    # --- Digest by neighborhood ---
    try:
            with open("last_digest_date.txt", "r") as f:
                last_date = f.read().strip()
        except FileNotFoundError:
            last_date = None

    today_str = datetime.date.today().isoformat()
    send_digest = (last_date != today_str)

    if send_digest:
        def digest_hood_match(row):
            if pd.isnull(row['neighborhoods']):
                return False
            return any(h in digest_neighborhoods for h in row['neighborhoods'].split(','))

        df_digest = df[df.apply(digest_hood_match, axis=1)] if digest_neighborhoods else df
        if not df_digest.empty:
            digest_msg = "<h2>New Craigslist Listings by Neighborhood</h2><br>"
            for hood in sorted(digest_neighborhoods):
                subdf = df_digest[df_digest['neighborhoods'].str.contains(hood, na=False)]
                if not subdf.empty:
                    digest_msg += f"<b>--- {hood} ---</b><br>"
                    for _, row in subdf.iterrows():
                        digest_msg += (
                            f"- <b>{row['title']}</b> (${row['price']}), "
                            f"{row['num_bedrooms']}bd/{row['num_bathrooms']}ba, "
                            f"<a href='{row['url']}'>{row['url']}</a><br>"
                        )
                    digest_msg += "<br>"
                    # Mark digest listings alerted
                    df.loc[subdf.index, 'alerted'] = True
            send_email("Craigslist Digest Update", digest_msg)
            print("Sent digest email.")
            with open("last_digest_date.txt", "w") as f:
                f.write(today_str)

    # Save updates
    df.to_csv(ACTIVE_PATH, index=False)

if __name__ == "__main__":
    main()