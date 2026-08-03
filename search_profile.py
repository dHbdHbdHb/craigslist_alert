"""
search_profile.py — loads and validates a search profile.

A profile is one person's housing search: their budget, neighborhoods, alert
thresholds, and email recipients. Profiles live in profiles/*.toml and are safe
to commit. Credentials are shared across profiles and live in secrets.env.

Selecting the active profile, in priority order:

    1. an explicit name passed to load_profile("alex")
    2. the HOUSING_PROFILE environment variable
    3. the single enabled profile, if there's exactly one

Validate a profile without running anything:

    python search_profile.py alex        # check one
    python search_profile.py             # check all
"""

from __future__ import annotations

import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

BASE_DIR     = Path(__file__).parent.resolve()
PROFILES_DIR = BASE_DIR / "profiles"
DATA_DIR     = BASE_DIR / "data"
DASHBOARD_DIR = BASE_DIR / "dashboards"
SECRETS_FILE = BASE_DIR / "secrets.env"

# Rows kept in listings_active.csv before the oldest spill into the archive.
MAX_ACTIVE_ROWS = 1000

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ProfileError(Exception):
    """Raised for any malformed profile or missing credential.

    The message is written to be read by someone setting this up for the first
    time, so it should always say what to fix and where.
    """


# ── Secrets ───────────────────────────────────────────────────────────────────

def load_secrets() -> dict[str, str]:
    """Parse secrets.env into a dict. Values already in the real environment win,
    which is what lets cron or CI override without touching the file."""
    values: dict[str, str] = {}

    if SECRETS_FILE.exists():
        for lineno, raw in enumerate(SECRETS_FILE.read_text().splitlines(), 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                raise ProfileError(
                    f"secrets.env line {lineno} is not KEY=VALUE:\n"
                    f"    {raw}\n"
                    f"Every line must be a KEY=VALUE pair, blank, or a # comment."
                )
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")

    # Real environment variables take precedence over the file. Membership, not
    # truthiness: exporting ORS_API_KEY= (empty) is how you deliberately turn
    # bike-time lookups off for a run, and a truthiness check would silently
    # ignore that and use the key from the file instead.
    for key in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ORS_API_KEY",
                "GOOGLE_MAPS_API_KEY"):
        if key in os.environ:
            values[key] = os.environ[key]

    missing = [k for k in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD") if not values.get(k)]
    if missing:
        raise ProfileError(
            f"Missing {' and '.join(missing)}.\n"
            f"Create {SECRETS_FILE.name} by copying the template:\n"
            f"    cp secrets.env.example secrets.env\n"
            f"then fill in your Gmail address and app password."
        )

    return values


# ── Profile ───────────────────────────────────────────────────────────────────

@dataclass
class Profile:
    name: str
    display_name: str
    enabled: bool

    # search
    max_price: int
    min_bedrooms: int
    region: str
    subarea: str
    city: str

    # neighborhoods
    include_neighborhoods: list[str]
    filter_by_neighborhood: bool

    # recipients
    digest_to: list[str]

    # digest thresholds
    digest_min_price: int
    digest_max_price: int
    digest_min_posting_age_minutes: int
    digest_scam_keywords: list[str]

    # dashboard
    dashboard_url: str
    map_center: list[float]
    show_historical: bool

    # transit
    caltrain_stations: list[tuple[str, list[float]]]
    bart_stations: list[tuple[str, list[float]]]

    # commute — door-to-door transit to one fixed destination (see [commute])
    commute_destination: list[float] | None
    commute_destination_name: str
    commute_arrive_by: tuple[int, int]
    commute_max_minutes: int
    commute_weights: dict[str, float]

    secrets: dict[str, str] = field(repr=False, default_factory=dict)

    # ── Derived paths — every profile gets its own data directory ─────────────

    @property
    def data_dir(self) -> Path:
        return DATA_DIR / self.name

    @property
    def active_csv(self) -> Path:
        return self.data_dir / "listings_active.csv"

    @property
    def archive_csv(self) -> Path:
        return self.data_dir / "listings_archive.csv"

    @property
    def bike_routes_json(self) -> Path:
        return self.data_dir / "bike_routes.json"

    @property
    def bart_routes_json(self) -> Path:
        return self.data_dir / "bart_bike_routes.json"

    @property
    def transit_commutes_json(self) -> Path:
        return self.data_dir / "transit_commutes.json"

    @property
    def last_digest_file(self) -> Path:
        return self.data_dir / "last_digest_date.txt"

    @property
    def dashboard_html(self) -> Path:
        return DASHBOARD_DIR / f"{self.name}.html"

    @property
    def search_url(self) -> str:
        """The Craigslist search page this profile scrapes.

        `subarea` matters far more than it looks. A region search covers the
        whole metro — for sfbay that is Santa Rosa to San Jose — and only the
        first page is fetched, so a San Francisco search without a subarea
        returns a handful of SF listings buried in a few hundred East Bay ones.
        Scoping to `sfc` returns SF listings instead.
        """
        area = f"{self.subarea}/" if self.subarea else ""
        return (
            f"https://{self.region}.craigslist.org/search/{area}apa"
            f"?max_price={self.max_price}&min_bedrooms={self.min_bedrooms}"
        )

    @property
    def gmail_address(self) -> str:
        return self.secrets["GMAIL_ADDRESS"]

    @property
    def gmail_app_password(self) -> str:
        return self.secrets["GMAIL_APP_PASSWORD"]

    @property
    def ors_api_key(self) -> str:
        return self.secrets.get("ORS_API_KEY", "")

    @property
    def google_maps_api_key(self) -> str:
        return self.secrets.get("GOOGLE_MAPS_API_KEY", "")

    @property
    def has_bike_times(self) -> bool:
        """Whether to compute cycling times to stations at all.

        A profile that lists no stations has opted out — the friend-facing
        profiles care about door-to-door transit, not bike-to-Caltrain, and
        running ORS for them would burn the rate limit for nothing.
        """
        return bool(self.caltrain_stations or self.bart_stations)

    @property
    def has_commute(self) -> bool:
        return self.commute_destination is not None

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        DASHBOARD_DIR.mkdir(parents=True, exist_ok=True)


# ── Parsing ───────────────────────────────────────────────────────────────────

def _require(table: dict, key: str, path: str, kind: type, where: str):
    if key not in table:
        raise ProfileError(f"{where}: missing required setting `{path}`.")
    val = table[key]
    if kind is int and isinstance(val, bool):
        raise ProfileError(f"{where}: `{path}` must be a number, got {val!r}.")
    if not isinstance(val, kind):
        want = {int: "a number", str: "text in \"quotes\"", list: "a list",
                bool: "true or false"}.get(kind, kind.__name__)
        raise ProfileError(f"{where}: `{path}` must be {want}, got {val!r}.")
    return val


def _parse_lonlat(coords, path: str, where: str) -> list[float]:
    """Validate one [longitude, latitude] pair."""
    if not isinstance(coords, list) or len(coords) != 2:
        raise ProfileError(f"{where}: `{path}` must be [longitude, latitude].")
    lon, lat = coords
    # Check for the classic [lat, lon] swap first: its message is far more
    # useful than the bare out-of-range error the same input would trigger.
    if abs(lat) > 90 and abs(lon) <= 90:
        raise ProfileError(
            f"{where}: `{path}` = {coords} looks reversed.\n"
            f"Coordinates here are [longitude, latitude] — longitude first.\n"
            f"In San Francisco that means roughly [-122.x, 37.x], "
            f"so you probably want [{lat}, {lon}]."
        )
    if not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
        raise ProfileError(
            f"{where}: `{path}` = {coords} is out of range "
            f"(longitude must be -180..180, latitude -90..90)."
        )
    return [float(lon), float(lat)]


def _parse_stations(raw, path: str, where: str) -> list[tuple[str, list[float]]]:
    stations = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "name" not in entry or "coords" not in entry:
            raise ProfileError(
                f"{where}: `{path}[{i}]` must look like\n"
                f'    {{ name = "Station", coords = [-122.4, 37.77] }}'
            )
        coords = _parse_lonlat(entry["coords"], f"{path}[{i}].coords", where)
        stations.append((str(entry["name"]), coords))
    return stations


# How the three commute factors trade off against each other. Overridable per
# profile — someone who only cares about door-to-door speed can zero the rest.
_DEFAULT_COMMUTE_WEIGHTS = {"time": 0.45, "frequency": 0.35, "redundancy": 0.20}


def _parse_commute(raw, where: str) -> dict:
    """Parse [commute], the door-to-door transit destination and its scoring."""
    if not raw:
        return {
            "commute_destination":      None,
            "commute_destination_name": "",
            "commute_arrive_by":        (9, 0),
            "commute_max_minutes":      45,
            "commute_weights":          dict(_DEFAULT_COMMUTE_WEIGHTS),
        }

    if "destination" not in raw:
        raise ProfileError(
            f"{where}: [commute] needs a `destination = [longitude, latitude]`.\n"
            f"Delete the whole [commute] section to turn commute times off."
        )
    destination = _parse_lonlat(raw["destination"], "commute.destination", where)

    arrive_raw = raw.get("arrive_by", "09:00")
    try:
        hour, _, minute = str(arrive_raw).partition(":")
        arrive_by = (int(hour), int(minute or 0))
        if not (0 <= arrive_by[0] <= 23 and 0 <= arrive_by[1] <= 59):
            raise ValueError
    except ValueError:
        raise ProfileError(
            f"{where}: `commute.arrive_by` must be a 24-hour \"HH:MM\" time, "
            f"got {arrive_raw!r}."
        ) from None

    max_minutes = raw.get("max_minutes", 45)
    if not isinstance(max_minutes, int) or isinstance(max_minutes, bool) or max_minutes <= 0:
        raise ProfileError(
            f"{where}: `commute.max_minutes` must be a positive number of minutes, "
            f"got {max_minutes!r}."
        )

    weights = dict(_DEFAULT_COMMUTE_WEIGHTS)
    for key, val in (raw.get("weights") or {}).items():
        if key not in weights:
            raise ProfileError(
                f"{where}: unknown `commute.weights.{key}`. "
                f"Valid keys: {', '.join(sorted(weights))}."
            )
        if not isinstance(val, (int, float)) or isinstance(val, bool) or val < 0:
            raise ProfileError(
                f"{where}: `commute.weights.{key}` must be a number >= 0, got {val!r}."
            )
        weights[key] = float(val)
    if sum(weights.values()) <= 0:
        raise ProfileError(f"{where}: `commute.weights` can't all be zero.")

    return {
        "commute_destination":      destination,
        "commute_destination_name": str(raw.get("destination_name", "work")),
        "commute_arrive_by":        arrive_by,
        "commute_max_minutes":      max_minutes,
        "commute_weights":          weights,
    }


def _known_neighborhoods() -> set[str] | None:
    """Neighborhood names defined in the shapes module, or None if shapely
    isn't installed (validation is then skipped rather than failing)."""
    try:
        from neighborhoods.neighborhood_shapes import neighborhood_shapes
        return set(neighborhood_shapes)
    except Exception:
        return None


def parse_profile(path: Path, secrets: dict[str, str] | None = None) -> Profile:
    where = f"profiles/{path.name}"

    try:
        raw = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"{where} is not valid TOML: {e}") from e

    name = _require(raw, "name", "name", str, where)
    if name != path.stem:
        raise ProfileError(
            f"{where}: `name` is \"{name}\" but the file is named {path.name}.\n"
            f"They must match — either rename the file to {name}.toml "
            f"or set name = \"{path.stem}\"."
        )
    if not re.fullmatch(r"[a-z0-9_-]+", name):
        raise ProfileError(
            f"{where}: `name` must be lowercase letters, numbers, - or _ "
            f"(it becomes a folder name). Got \"{name}\"."
        )

    search = _require(raw, "search", "[search]", dict, where)
    hoods  = _require(raw, "neighborhoods", "[neighborhoods]", dict, where)
    alerts = _require(raw, "alerts", "[alerts]", dict, where)
    dig    = _require(alerts, "digest", "[alerts.digest]", dict, where)
    dash    = raw.get("dashboard", {})
    transit = raw.get("transit", {})
    commute = _parse_commute(raw.get("commute", {}), where)

    # Immediate per-listing alerts were removed — in practice they mostly caught
    # scams, which is what the keyword and posting-age filters below are for.
    # Warn rather than fail, so an old profile still loads and still works.
    for stale, moved_to in (
        ("alerts.priority",     "[alerts.digest]"),
        ("alerts.priority_to",  "alerts.digest_to"),
        ("neighborhoods.priority", "neighborhoods.include"),
    ):
        table, _, key = stale.rpartition(".")
        parent = {"alerts": alerts, "neighborhoods": hoods}[table]
        if key in parent:
            print(
                f"warning: {where}: `{stale}` is no longer used — priority alerts "
                f"were removed. Move anything you still want to {moved_to} and "
                f"delete it.",
                file=sys.stderr,
            )

    include = list(_require(hoods, "include", "neighborhoods.include", list, where))

    known = _known_neighborhoods()
    if known is not None:
        unknown = [h for h in include if h not in known]
        if unknown:
            raise ProfileError(
                f"{where}: unknown neighborhood(s): {', '.join(repr(u) for u in unknown)}.\n"
                f"Valid names are:\n    " + "\n    ".join(sorted(known)) + "\n"
                f"Names are case- and punctuation-sensitive."
            )

    # An empty include list means "everywhere".
    effective_include = include or sorted(known or [])

    # `filter = false` turns the include list into a pure ordering preference:
    # nothing is dropped for being outside a shape, and listings that match no
    # shape are grouped at the end instead of disappearing. That matters because
    # the hand-drawn shapes cover only part of the city — most scraped listings
    # match nothing, and filtering on them silently throws away the majority.
    filter_by_neighborhood = bool(hoods.get("filter", True))

    digest_to = list(_require(alerts, "digest_to", "alerts.digest_to", list, where))
    bad = [a for a in digest_to if not _EMAIL_RE.match(str(a))]
    if bad:
        raise ProfileError(
            f"{where}: alerts.digest_to contains "
            f"{'an invalid address' if len(bad) == 1 else 'invalid addresses'}: "
            f"{', '.join(repr(b) for b in bad)}"
        )

    d_min = _require(dig,  "min_price", "alerts.digest.min_price", int, where)
    d_max = _require(dig,  "max_price", "alerts.digest.max_price", int, where)
    s_max = _require(search, "max_price", "search.max_price", int, where)

    if d_min > d_max:
        raise ProfileError(
            f"{where}: alerts.digest.min_price ({d_min}) is greater than "
            f"alerts.digest.max_price ({d_max})."
        )

    # Not fatal, but it silently guarantees zero results, so it's worth shouting.
    if d_max > s_max:
        print(
            f"warning: {where}: alerts.digest.max_price ({d_max}) is above "
            f"search.max_price ({s_max}), so listings between {s_max} and {d_max} "
            f"are never scraped and can never match. Raise search.max_price.",
            file=sys.stderr,
        )

    map_center = dash.get("map_center", [37.758, -122.433])
    if not isinstance(map_center, list) or len(map_center) != 2:
        raise ProfileError(f"{where}: dashboard.map_center must be [latitude, longitude].")

    return Profile(
        name=name,
        display_name=str(raw.get("display_name", name.title())),
        enabled=bool(raw.get("enabled", True)),
        max_price=s_max,
        min_bedrooms=_require(search, "min_bedrooms", "search.min_bedrooms", int, where),
        region=str(search.get("region", "sfbay")),
        subarea=str(search.get("subarea", "")).strip("/"),
        city=str(search.get("city", "San Francisco")),
        include_neighborhoods=effective_include,
        filter_by_neighborhood=filter_by_neighborhood,
        digest_to=[str(a) for a in digest_to],
        digest_min_price=d_min,
        digest_max_price=d_max,
        digest_min_posting_age_minutes=int(dig.get("min_posting_age_minutes", 20)),
        digest_scam_keywords=[str(k) for k in dig.get("scam_keywords", [])],
        dashboard_url=str(dash.get("url", "")),
        map_center=[float(map_center[0]), float(map_center[1])],
        show_historical=bool(dash.get("show_historical", True)),
        caltrain_stations=_parse_stations(transit.get("caltrain", []), "transit.caltrain", where),
        bart_stations=_parse_stations(transit.get("bart", []), "transit.bart", where),
        **commute,
        secrets=secrets if secrets is not None else {},
    )


# ── Discovery ─────────────────────────────────────────────────────────────────

def list_profiles() -> list[Path]:
    if not PROFILES_DIR.exists():
        return []
    return sorted(p for p in PROFILES_DIR.glob("*.toml") if p.stem != "example")


def load_profile(name: str | None = None, *, require_secrets: bool = True) -> Profile:
    """Load a profile by name, by HOUSING_PROFILE, or by being the only one enabled."""
    secrets = load_secrets() if require_secrets else {}
    available = list_profiles()

    if not available:
        raise ProfileError(
            "No profiles found in profiles/.\n"
            "Create one by copying the template:\n"
            "    cp profiles/example.toml profiles/yourname.toml"
        )

    name = name or os.environ.get("HOUSING_PROFILE")

    if name:
        path = PROFILES_DIR / f"{name}.toml"
        if not path.exists():
            raise ProfileError(
                f"No profile named \"{name}\".\n"
                f"Available: {', '.join(p.stem for p in available)}"
            )
        return parse_profile(path, secrets)

    enabled = [p for p in available if parse_profile(p, secrets).enabled]

    if len(enabled) == 1:
        return parse_profile(enabled[0], secrets)

    if not enabled:
        raise ProfileError(
            f"No profile is enabled, so there's nothing to run.\n"
            f"Found: {', '.join(p.stem for p in available)}.\n"
            f"Set `enabled = true` in one of them, or name one explicitly with "
            f"--profile NAME."
        )

    raise ProfileError(
        f"Multiple profiles are enabled ({', '.join(p.stem for p in enabled)}), "
        f"so you have to say which one.\n"
        f"Pass --profile NAME, or set HOUSING_PROFILE=NAME."
    )


def load_enabled_profiles(*, require_secrets: bool = True) -> list[Profile]:
    """Every enabled profile — used by the shell scripts to loop over everyone."""
    secrets = load_secrets() if require_secrets else {}
    return [p for p in (parse_profile(f, secrets) for f in list_profiles()) if p.enabled]


def add_profile_arg(parser):
    """Register the shared --profile flag on an argparse parser."""
    parser.add_argument(
        "--profile", metavar="NAME",
        help="Which profile to run (default: $HOUSING_PROFILE, or the only enabled one)",
    )


# ── CLI: validate and explain ─────────────────────────────────────────────────

def _describe(p: Profile) -> None:
    status = "enabled" if p.enabled else "PAUSED (enabled = false)"
    print(f"\n\033[1m{p.name}\033[0m — {p.display_name}  [{status}]")
    print(f"  searching   {p.region}.craigslist.org"
          f"{'/' + p.subarea if p.subarea else ' (whole region)'}, "
          f"{p.city or 'all cities'}, {p.min_bedrooms}+ bed, up to ${p.max_price:,}")
    if p.city and not p.subarea:
        print(f"              \033[33mwarning:\033[0m no subarea set — only the first "
              f"page of the whole region is scraped, so most results will be "
              f"filtered out by city. See search.subarea.")
    print(f"  {'filtering to' if p.filter_by_neighborhood else 'sorting by '} "
          f"{len(p.include_neighborhoods)} neighborhoods: "
          f"{', '.join(p.include_neighborhoods)}")
    if not p.filter_by_neighborhood:
        print(f"              (filter = false — nothing is dropped for being "
              f"outside these; they only set the order)")
    print(f"  digest      ${p.digest_min_price:,}–${p.digest_max_price:,}, "
          f"posted {p.digest_min_posting_age_minutes}+ min ago "
          f"→ {', '.join(p.digest_to) or '(nobody — digest disabled)'}")
    print(f"  filtering   {len(p.digest_scam_keywords)} scam keyword(s)")
    if p.has_commute:
        lon, lat = p.commute_destination
        print(f"  commute     ≤{p.commute_max_minutes} min by transit to "
              f"{p.commute_destination_name} [{lon}, {lat}], "
              f"arriving {p.commute_arrive_by[0]:02d}:{p.commute_arrive_by[1]:02d}")
        print(f"  weighting   " + ", ".join(
            f"{k} {v:g}" for k, v in p.commute_weights.items()))
    else:
        print(f"  commute     (none — no [commute] section)")
    print(f"  bike times  {'on' if p.has_bike_times else 'off (no [transit] stations)'}")
    print(f"  data        {p.data_dir.relative_to(BASE_DIR)}/")
    print(f"  dashboard   {p.dashboard_html.relative_to(BASE_DIR)}"
          f"{'  → ' + p.dashboard_url if p.dashboard_url else ''}")


def main() -> int:
    args = sys.argv[1:]

    try:
        secrets = load_secrets()
        missing_keys = [
            label for key, label in (
                ("ORS_API_KEY",         "no ORS_API_KEY: bike times disabled"),
                ("GOOGLE_MAPS_API_KEY", "no GOOGLE_MAPS_API_KEY: commute times disabled"),
            ) if not secrets.get(key)
        ]
        print(f"✓ secrets.env — sending as {secrets['GMAIL_ADDRESS']}"
              + (f"  ({'; '.join(missing_keys)})" if missing_keys else ""))
    except ProfileError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1

    paths = [PROFILES_DIR / f"{a}.toml" for a in args] if args else list_profiles()
    if not paths:
        print("✗ No profiles found in profiles/", file=sys.stderr)
        return 1

    failed = 0
    for path in paths:
        if not path.exists():
            print(f"✗ No profile named \"{path.stem}\"", file=sys.stderr)
            failed += 1
            continue
        try:
            _describe(parse_profile(path, secrets))
        except ProfileError as e:
            print(f"\n✗ {e}", file=sys.stderr)
            failed += 1

    print()
    if failed:
        print(f"{failed} profile(s) need fixing.", file=sys.stderr)
        return 1
    print(f"All {len(paths)} profile(s) valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
