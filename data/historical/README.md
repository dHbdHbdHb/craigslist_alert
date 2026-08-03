# Historical listings — frozen snapshot

`2026-sf.csv` is a **read-only** record of 843 San Francisco Craigslist
rental listings covering **2026-03-10 – 2026-05-16**, captured during the original search this
project was built for.

It is deliberately kept separate from the live per-profile datasets in
`data/<name>/`. The dashboard loads it as its own layer so it can show how
prices moved over time without those older listings distorting anyone's
current medians, heatmaps, or neighborhood stats.

## What's in it

| | |
|---|---|
| Rows | 843 |
| Date range | 2026-03-10 – 2026-05-16 |
| Priced listings | 843 |
| Median price | $4,379 |
| Price range | $750 – $5,600 |
| Source | craigslist (sfbay), San Francisco only |

Columns match the live CSVs, minus the per-run state (`alerted`,
`priority_alerted`) which has no meaning in a historical record.

## Caveats

Two things to know before drawing conclusions from this data:

1. **There's a gap.** Coverage is not continuous — the scraper was down for
   stretches, and until the archive fix (see `craigslist_scraper.py`), listings
   that were flagged or deleted on Craigslist were dropped from the dataset
   entirely rather than archived. Listings that vanished quickly are therefore
   underrepresented, which biases the surviving sample toward listings that sat
   on the market longer, and so slightly *upward* on price.

2. **`time_posted` is the posting time, not the rental price date.** Craigslist
   reposts are common, so the same unit can appear more than once under
   different URLs.

## Don't edit this file

Nothing writes to it after the initial freeze. To rebuild it from scratch,
delete it and re-run `python migrate_to_profiles.py`.
