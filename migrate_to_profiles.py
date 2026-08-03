"""
migrate_to_profiles.py — one-time move from the single-user layout to profiles.

Before:  craigslist_data/listings_active.csv        (one global dataset)
After:   data/<profile>/listings_active.csv         (one dataset per person)

It also builds data/historical/ — a frozen, read-only snapshot of the original
search, so months of price history stay queryable without contaminating anyone's
live numbers.

Safe to run more than once: it only moves files that haven't been moved yet, and
it refuses to overwrite anything. Run it on the Mac and on the Pi.

    python migrate_to_profiles.py --dry-run     # show the plan, change nothing
    python migrate_to_profiles.py               # do it
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import pandas as pd

from search_profile import BASE_DIR, DATA_DIR, ProfileError, load_profile

LEGACY_DIR = BASE_DIR / "craigslist_data"
HISTORICAL_DIR = DATA_DIR / "historical"

# legacy filename -> new filename
FILE_MAP = {
    "listings_active.csv":   "listings_active.csv",
    "listings_archive.csv":  "listings_archive.csv",
    "bike_routes.json":      "bike_routes.json",
    "bart_bike_routes.json": "bart_bike_routes.json",
}

# Per-run state that means nothing in a historical record.
RUNTIME_COLUMNS = ["alerted", "priority_alerted"]


def _move(src: Path, dst: Path, dry_run: bool) -> bool:
    if not src.exists():
        return False
    if dst.exists():
        print(f"  skip   {dst.relative_to(BASE_DIR)} already exists")
        return False
    print(f"  move   {src.relative_to(BASE_DIR)} -> {dst.relative_to(BASE_DIR)}")
    if not dry_run:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
    return True


def migrate_files(profile_name: str, dry_run: bool) -> None:
    print(f"\nMoving legacy data into data/{profile_name}/")
    if not LEGACY_DIR.exists():
        print(f"  nothing to do — {LEGACY_DIR.name}/ doesn't exist")
        return

    moved = 0
    for old_name, new_name in FILE_MAP.items():
        moved += _move(LEGACY_DIR / old_name, DATA_DIR / profile_name / new_name, dry_run)

    moved += _move(BASE_DIR / "last_digest_date.txt",
                   DATA_DIR / profile_name / "last_digest_date.txt", dry_run)

    if not moved:
        print("  nothing left to move")

    # Only remove the legacy directory once it's genuinely empty, so nothing
    # unexpected is ever deleted.
    if not dry_run and LEGACY_DIR.exists() and not any(LEGACY_DIR.iterdir()):
        LEGACY_DIR.rmdir()
        print(f"  rmdir  {LEGACY_DIR.name}/ (empty)")


def freeze_historical(profile_name: str, dry_run: bool, rebuild: bool = False) -> None:
    """Build an immutable snapshot of everything scraped so far."""
    print("\nFreezing historical snapshot")
    out_csv = HISTORICAL_DIR / "2026-sf.csv"

    if out_csv.exists() and not rebuild:
        print(f"  skip   {out_csv.relative_to(BASE_DIR)} already exists "
              f"(use --rebuild-historical to refresh it)")
        return

    # Look in the new location first, then fall back to the legacy one — so this
    # reports accurately during a --dry-run, when the move hasn't happened yet.
    sources = []
    for name in ("listings_active.csv", "listings_archive.csv"):
        path = next((p for p in (DATA_DIR / profile_name / name, LEGACY_DIR / name)
                     if p.exists()), None)
        if path is not None:
            sources.append(path)

    # Fold in any existing snapshot. The active CSV is a rolling window, so
    # listings age out of it over time — rebuilding from the live files alone
    # would silently drop every listing that has since rotated away.
    if out_csv.exists():
        sources.append(out_csv)

    frames = []
    for path in sources:
        try:
            df = pd.read_csv(path)
        except (pd.errors.EmptyDataError, pd.errors.ParserError):
            continue
        if not df.empty:
            print(f"  read   {path.relative_to(BASE_DIR)} ({len(df)} rows)")
            frames.append(df)

    if not frames:
        print("  no source data found — nothing to freeze")
        return

    df = pd.concat(frames, ignore_index=True)
    before = len(df)
    df = df.drop_duplicates(subset="url", keep="first")
    df = df.drop(columns=[c for c in RUNTIME_COLUMNS if c in df.columns])

    df["time_posted"] = pd.to_datetime(df["time_posted"], utc=True, errors="coerce")
    df = df.sort_values("time_posted").reset_index(drop=True)

    valid = df["time_posted"].dropna()
    span = (f"{valid.min():%Y-%m-%d} – {valid.max():%Y-%m-%d}"
            if not valid.empty else "an unknown range")

    print(f"  {before} rows in, {len(df)} after dedupe by url")
    print(f"  covering {span}")
    print(f"  write  {out_csv.relative_to(BASE_DIR)}")

    if dry_run:
        return

    HISTORICAL_DIR.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)

    priced = pd.to_numeric(df["price"], errors="coerce").dropna()
    (HISTORICAL_DIR / "README.md").write_text(
        f"""# Historical listings — frozen snapshot

`2026-sf.csv` is a **read-only** record of {len(df):,} San Francisco Craigslist
rental listings covering **{span}**, captured during the original search this
project was built for.

It is deliberately kept separate from the live per-profile datasets in
`data/<name>/`. The dashboard loads it as its own layer so it can show how
prices moved over time without those older listings distorting anyone's
current medians, heatmaps, or neighborhood stats.

## What's in it

| | |
|---|---|
| Rows | {len(df):,} |
| Date range | {span} |
| Priced listings | {len(priced):,} |
| Median price | ${priced.median():,.0f} |
| Price range | ${priced.min():,.0f} – ${priced.max():,.0f} |
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
""",
        encoding="utf-8",
    )
    print(f"  write  {(HISTORICAL_DIR / 'README.md').relative_to(BASE_DIR)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    parser.add_argument("--profile", metavar="NAME",
                        help="Profile to migrate the legacy data into "
                             "(default: the only enabled one, or $HOUSING_PROFILE)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the plan without changing anything")
    parser.add_argument("--rebuild-historical", action="store_true",
                        help="Refresh data/historical/ by folding newly-pulled "
                             "listings into the existing snapshot")
    args = parser.parse_args()

    try:
        # require_secrets=False: migrating data shouldn't demand a Gmail password.
        profile = load_profile(args.profile, require_secrets=False)
    except ProfileError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    if args.dry_run:
        print("DRY RUN — nothing will be changed.")

    print(f"Target profile: {profile.name}")
    migrate_files(profile.name, args.dry_run)
    freeze_historical(profile.name, args.dry_run, rebuild=args.rebuild_historical)

    print("\nDone." if not args.dry_run else "\nDry run complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
