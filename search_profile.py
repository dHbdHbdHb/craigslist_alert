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
    for key in ("GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "ORS_API_KEY"):
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
    city: str

    # neighborhoods
    include_neighborhoods: list[str]
    priority_neighborhoods: set[str]

    # recipients
    digest_to: list[str]
    priority_to: list[str]

    # priority thresholds
    priority_max_price: int
    priority_min_price: int
    priority_min_bathrooms: int
    priority_min_posting_age_minutes: int
    priority_scam_keywords: list[str]

    # digest thresholds
    digest_min_price: int
    digest_max_price: int

    # dashboard
    dashboard_url: str
    map_center: list[float]
    show_historical: bool

    # transit
    caltrain_stations: list[tuple[str, list[float]]]
    bart_stations: list[tuple[str, list[float]]]

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
    def last_digest_file(self) -> Path:
        return self.data_dir / "last_digest_date.txt"

    @property
    def dashboard_html(self) -> Path:
        return DASHBOARD_DIR / f"{self.name}.html"

    @property
    def search_url(self) -> str:
        return (
            f"https://{self.region}.craigslist.org/search/apa"
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


def _parse_stations(raw, path: str, where: str) -> list[tuple[str, list[float]]]:
    stations = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "name" not in entry or "coords" not in entry:
            raise ProfileError(
                f"{where}: `{path}[{i}]` must look like\n"
                f'    {{ name = "Station", coords = [-122.4, 37.77] }}'
            )
        coords = entry["coords"]
        if not isinstance(coords, list) or len(coords) != 2:
            raise ProfileError(f"{where}: `{path}[{i}].coords` must be [longitude, latitude].")
        lon, lat = coords
        # Check for the classic [lat, lon] swap first: its message is far more
        # useful than the bare out-of-range error the same input would trigger.
        if abs(lat) > 90 and abs(lon) <= 90:
            raise ProfileError(
                f"{where}: `{path}[{i}].coords` = {coords} looks reversed.\n"
                f"Coordinates here are [longitude, latitude] — longitude first.\n"
                f"In San Francisco that means roughly [-122.x, 37.x], "
                f"so you probably want [{lat}, {lon}]."
            )
        if not (-180 <= lon <= 180) or not (-90 <= lat <= 90):
            raise ProfileError(
                f"{where}: `{path}[{i}].coords` = {coords} is out of range "
                f"(longitude must be -180..180, latitude -90..90)."
            )
        stations.append((str(entry["name"]), [float(lon), float(lat)]))
    return stations


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
    prio   = _require(alerts, "priority", "[alerts.priority]", dict, where)
    dig    = _require(alerts, "digest", "[alerts.digest]", dict, where)
    dash   = raw.get("dashboard", {})
    transit = raw.get("transit", {})

    include  = list(_require(hoods, "include", "neighborhoods.include", list, where))
    priority = list(_require(hoods, "priority", "neighborhoods.priority", list, where))

    known = _known_neighborhoods()
    if known is not None:
        unknown = [h for h in include + priority if h not in known]
        if unknown:
            raise ProfileError(
                f"{where}: unknown neighborhood(s): {', '.join(repr(u) for u in unknown)}.\n"
                f"Valid names are:\n    " + "\n    ".join(sorted(known)) + "\n"
                f"Names are case- and punctuation-sensitive."
            )

    # An empty include list means "everywhere".
    effective_include = include or sorted(known or [])

    stray = [h for h in priority if h not in effective_include]
    if stray:
        raise ProfileError(
            f"{where}: {', '.join(repr(s) for s in stray)} "
            f"{'is' if len(stray) == 1 else 'are'} in neighborhoods.priority but not in "
            f"neighborhoods.include.\nPriority must be a subset of include — otherwise "
            f"you'd get urgent alerts for places you excluded."
        )

    digest_to   = list(_require(alerts, "digest_to", "alerts.digest_to", list, where))
    priority_to = list(_require(alerts, "priority_to", "alerts.priority_to", list, where))
    for label, addrs in (("digest_to", digest_to), ("priority_to", priority_to)):
        bad = [a for a in addrs if not _EMAIL_RE.match(str(a))]
        if bad:
            raise ProfileError(
                f"{where}: alerts.{label} contains "
                f"{'an invalid address' if len(bad) == 1 else 'invalid addresses'}: "
                f"{', '.join(repr(b) for b in bad)}"
            )

    p_min = _require(prio, "min_price", "alerts.priority.min_price", int, where)
    p_max = _require(prio, "max_price", "alerts.priority.max_price", int, where)
    d_min = _require(dig,  "min_price", "alerts.digest.min_price", int, where)
    d_max = _require(dig,  "max_price", "alerts.digest.max_price", int, where)
    s_max = _require(search, "max_price", "search.max_price", int, where)

    for lo, hi, label in ((p_min, p_max, "alerts.priority"), (d_min, d_max, "alerts.digest")):
        if lo > hi:
            raise ProfileError(
                f"{where}: {label}.min_price ({lo}) is greater than "
                f"{label}.max_price ({hi})."
            )

    # Not fatal, but it silently guarantees zero results, so it's worth shouting.
    for label, hi in (("alerts.priority.max_price", p_max), ("alerts.digest.max_price", d_max)):
        if hi > s_max:
            print(
                f"warning: {where}: {label} ({hi}) is above search.max_price ({s_max}), "
                f"so listings between {s_max} and {hi} are never scraped and can "
                f"never match. Raise search.max_price.",
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
        city=str(search.get("city", "San Francisco")),
        include_neighborhoods=effective_include,
        priority_neighborhoods=set(priority),
        digest_to=[str(a) for a in digest_to],
        priority_to=[str(a) for a in priority_to],
        priority_max_price=p_max,
        priority_min_price=p_min,
        priority_min_bathrooms=_require(prio, "min_bathrooms", "alerts.priority.min_bathrooms", int, where),
        priority_min_posting_age_minutes=int(prio.get("min_posting_age_minutes", 20)),
        priority_scam_keywords=[str(k) for k in prio.get("scam_keywords", [])],
        digest_min_price=d_min,
        digest_max_price=d_max,
        dashboard_url=str(dash.get("url", "")),
        map_center=[float(map_center[0]), float(map_center[1])],
        show_historical=bool(dash.get("show_historical", True)),
        caltrain_stations=_parse_stations(transit.get("caltrain", []), "transit.caltrain", where),
        bart_stations=_parse_stations(transit.get("bart", []), "transit.bart", where),
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
    print(f"  searching   {p.region}.craigslist.org, {p.city or 'all cities'}, "
          f"{p.min_bedrooms}+ bed, up to ${p.max_price:,}")
    print(f"  tracking    {len(p.include_neighborhoods)} neighborhoods: "
          f"{', '.join(p.include_neighborhoods)}")
    print(f"  priority    {', '.join(sorted(p.priority_neighborhoods)) or '(none)'}")
    print(f"  digest      ${p.digest_min_price:,}–${p.digest_max_price:,} "
          f"→ {', '.join(p.digest_to) or '(nobody — digest disabled)'}")
    print(f"  urgent      ${p.priority_min_price:,}–${p.priority_max_price:,}, "
          f"{p.priority_min_bathrooms}+ bath, after {p.priority_min_posting_age_minutes} min "
          f"→ {', '.join(p.priority_to) or '(nobody — alerts disabled)'}")
    print(f"  data        {p.data_dir.relative_to(BASE_DIR)}/")
    print(f"  dashboard   {p.dashboard_html.relative_to(BASE_DIR)}"
          f"{'  → ' + p.dashboard_url if p.dashboard_url else ''}")


def main() -> int:
    args = sys.argv[1:]

    try:
        secrets = load_secrets()
        print(f"✓ secrets.env — sending as {secrets['GMAIL_ADDRESS']}"
              f"{'' if secrets.get('ORS_API_KEY') else '  (no ORS_API_KEY: bike times disabled)'}")
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
