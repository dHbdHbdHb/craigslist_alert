# Setting up your housing search

This watches Craigslist for apartments, emails you when something good appears,
and builds a dashboard showing how prices are moving in the neighborhoods you
care about.

Everything you'll need to change lives in **one file**: `profiles/<yourname>.toml`.
You shouldn't need to touch any Python.

---

## What you're setting up

| | |
|---|---|
| **Scraper** | Checks Craigslist every 10 minutes for new listings |
| **Priority alert** | Immediate email when something matches your shortlist |
| **Daily digest** | One email a day with everything new, grouped by neighborhood |
| **Dashboard** | Charts + a map, rebuilt every 10 minutes |

It runs on a Raspberry Pi that's already set up. Multiple people can share it —
each person gets their own profile, their own data, and their own dashboard.

---

## 1. Make your profile

From the repo directory:

```bash
cp profiles/example.toml profiles/yourname.toml
```

Use your actual first name, lowercase, no spaces — it becomes a folder name.
Then open the file and work top to bottom. Every setting has a comment
explaining what it does, and the things you must change are marked `TODO`.

The four that matter most:

- **`[search] max_price`** — set this *generously*, ~15% above your real
  ceiling. It controls what gets scraped at all. Setting it tight means you
  never see near-misses on the dashboard and lose all your price context.
- **`[neighborhoods] include`** — everywhere you'd consider.
- **`[neighborhoods] priority`** — the shortlist worth interrupting your day for.
- **`[alerts] digest_to` / `priority_to`** — where email goes.

### Seeing the neighborhoods on a map

The neighborhood names are hand-drawn shapes, not official SF districts. To see
exactly where they are:

```bash
python neighborhoods/neighborhood_shapes.py
open sf_neighborhoods_map.html      # on Linux: xdg-open
```

Hover any shape for its name. A couple worth knowing: **"Chill Mission"** is the
quieter southeast slice of the Mission, and **"Way Out There"** is the western
half of the city plus anything that didn't land inside a defined shape.

---

## 2. Check it

```bash
python search_profile.py yourname
```

This validates your file and prints back what it understood — without scraping
anything or sending any email. It catches the mistakes that are otherwise
painful to debug: misspelled neighborhoods, a price floor above the ceiling,
priority neighborhoods you forgot to also list in `include`, malformed email
addresses.

You'll see something like:

```
✓ secrets.env — sending as someone@gmail.com

alex — Alex  [enabled]
  searching   sfbay.craigslist.org, San Francisco, 2+ bed, up to $5,600
  tracking    4 neighborhoods: Mission, Chill Mission, Bernal, Potrero Hill
  priority    Bernal
  digest      $2,100–$5,300 → alex@example.com
  urgent      $2,800–$4,600, 2+ bath, after 20 min → alex@example.com
  data        data/alex/
  dashboard   dashboards/alex.html
```

Read that back carefully — it's the last chance to catch a setting that's valid
but not what you meant.

---

## 3. Try it for real

Scrape once, then build your dashboard:

```bash
python craigslist_scraper.py --profile yourname
python analyze_listings.py --profile yourname --open
```

Then preview the emails **without sending anything**:

```bash
python email_alert.py --profile yourname --dry-run
```

`--dry-run` prints what it *would* send and skips the once-a-day check, so you
can run it repeatedly while tuning your filters.

If the digest is empty, your filters are probably too tight. The usual causes,
in order of likelihood: `[alerts.digest]` price band too narrow, `include` list
too short, or `min_bathrooms` set to 2 when most listings have 1.

---

## 4. Turn it on

Set `enabled = true` in your profile. That's it — the Pi's cron jobs pick up
every enabled profile automatically, so there's nothing else to schedule.

To pause later without losing anything, set it back to `false`.

---

## Reading the dashboard

Most of it is self-explanatory. Two things that aren't:

**The grey dotted line** on *Daily Median Price Over Time* is a frozen archive
of a search run in March–April 2026. It's there for historical context and is
kept completely separate from your live numbers — it never affects your medians,
counts, heatmaps, or map. Details and caveats are in
[`data/historical/README.md`](data/historical/README.md). Set
`show_historical = false` in your profile to hide it.

**Bike times** are cycling minutes to the nearest Caltrain or BART station, from
`[transit]` in your profile. If you commute somewhere specific, replace those
coordinates with your actual destination — but note they're `[longitude,
latitude]`, which is the reverse of `map_center`. In SF that means roughly
`[-122.x, 37.x]`. Get it backwards and the validator will tell you.

---

## Adding a neighborhood

If the shape you want doesn't exist:

1. Go to [geojson.io](https://geojson.io) and draw a polygon around the area.
2. Copy the `coordinates` array out of the GeoJSON panel on the right.
3. Add an entry to `neighborhoods/neighborhood_shapes.py`, following the
   existing ones — the coordinate order there is `[longitude, latitude]`, which
   is what geojson.io already gives you, so paste it as-is.
4. Add the name to your profile's `include` list.
5. Run `python search_profile.py yourname` to confirm it's recognised.

Shapes are shared by everyone on the Pi, so give it a name that'll make sense to
someone else.

---

## Troubleshooting

**"No profile is enabled"** — set `enabled = true` in your `.toml`, or name one
explicitly with `--profile yourname`.

**"Multiple profiles are enabled, so you have to say which one"** — you ran a
script without `--profile`. Add it.

**Digest says "already sent today" but no email arrived** — the date file got
written during a failed run. Reset it:

```bash
echo "$(date -d yesterday '+%Y-%m-%d')" > data/yourname/last_digest_date.txt
bash shell_scripts/run_alert.sh yourname
```

**`SMTPAuthenticationError`** — the shared Gmail app password expired or was
revoked. Generate a new one at
[myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords)
and update `GMAIL_APP_PASSWORD` in `secrets.env`. That file is gitignored and
never committed.

**Bike times are missing or absurd** — either `ORS_API_KEY` is unset in
`secrets.env` (bike times are then skipped entirely, which is fine), or your
`[transit]` coordinates are reversed. See above.

---

## Where things live

```
profiles/yourname.toml        your settings — the only file you should need to edit
secrets.env                   shared credentials (gitignored, never committed)
data/yourname/                your listings, your route cache, your digest state
data/historical/              frozen Mar–Apr 2026 archive (read-only)
dashboards/yourname.html      your dashboard
neighborhoods/                shared neighborhood shapes
shell_scripts/                what cron runs
```

Your data and someone else's never mix. The only shared things are the Gmail
sender, the routing API key, and the neighborhood shapes.
