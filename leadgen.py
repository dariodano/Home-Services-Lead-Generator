"""Core lead-generation logic: search the Google Places API (New) across a
grid of cells, keep businesses with no real website, and store them in the
local SQLite database (see db.py).

Used by both the web UI (app.py) and the CLI (find_leads.py).
Requires the GOOGLE_MAPS_API_KEY environment variable.
"""

import math
import os
import re
import time
from typing import Callable

import requests
from dotenv import load_dotenv

import db

# Load GOOGLE_MAPS_API_KEY (and any other vars) from a .env file in this
# folder, if present. A real shell env var still wins over the .env file.
load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Only the fields we actually need — the field mask controls what Google
# returns AND which billing tier the call lands in.
FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.nationalPhoneNumber",
    "places.websiteUri",
    "places.formattedAddress",
    "places.googleMapsUri",
    "places.location",
    "nextPageToken",
])

METERS_PER_MILE = 1609.34
METERS_PER_DEGREE_LAT = 111320.0

DEFAULT_CENTER = (39.9526, -75.1652)  # Philadelphia City Hall
DEFAULT_RADIUS_MILES = 20.0
DEFAULT_CELL_KM = 5.0

# Domains that indicate a social-media page or auto-generated placeholder
# rather than a real business website. Matched against the hostname
# (exact match or subdomain).
SOCIAL_DOMAINS = {
    "facebook.com",
    "m.facebook.com",
    "fb.com",
    "instagram.com",
    "linktr.ee",
    "nextdoor.com",
    "yelp.com",
    "business.site",  # Google's discontinued free one-page sites
}

# linktree also operates linktree.com and other TLD variants
SOCIAL_DOMAIN_PATTERNS = [re.compile(r"(^|\.)linktree\.[a-z.]+$")]

MAX_PAGES_PER_CELL = 3       # 3 pages x 20 results = 60 results max per cell
REQUEST_DELAY_S = 0.2        # polite delay between API calls
PAGE_TOKEN_DELAY_S = 1.0     # Google needs a moment before a nextPageToken is valid
MAX_RETRIES = 5              # retries on 429/5xx with exponential backoff

STATUS_NO_WEBSITE = "No website"
STATUS_SOCIAL_ONLY = "Social only"


class LeadGenError(RuntimeError):
    """A search failed in a way the caller should show to the user."""


def get_api_key() -> str | None:
    key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    return key or None


def parse_center(center: str) -> tuple[float, float]:
    """Parse a "lat,lng" string into a (lat, lng) float tuple."""
    try:
        lat_str, lng_str = center.split(",")
        lat, lng = float(lat_str.strip()), float(lng_str.strip())
    except ValueError:
        raise LeadGenError(f'Center must be "lat,lng", got: {center!r}') from None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise LeadGenError(f"Center out of range: {center!r}")
    return lat, lng


# ---------------------------------------------------------------------------
# Grid construction
# ---------------------------------------------------------------------------

def build_grid(center_lat: float, center_lng: float, radius_m: float,
               cell_m: float) -> list[dict]:
    """
    Tile the bounding box of the search circle into square cells of ~cell_m
    meters, keeping only cells whose center falls inside the radius. This
    approximates the circle and avoids wasted API calls in the box corners.

    Returns a list of cells, each a dict with the rectangle's SW ("low") and
    NE ("high") corners in the format the Places API expects.
    """
    # Meters per degree at this latitude. Latitude spacing is ~constant;
    # longitude spacing shrinks with cos(latitude).
    m_per_deg_lat = METERS_PER_DEGREE_LAT
    m_per_deg_lng = METERS_PER_DEGREE_LAT * math.cos(math.radians(center_lat))

    cell_deg_lat = cell_m / m_per_deg_lat
    cell_deg_lng = cell_m / m_per_deg_lng

    # Number of cell-steps needed to reach the edge of the bounding box.
    # The grid is anchored with the middle cell centered on the center point,
    # so even a radius smaller than one cell still yields that one cell.
    n = math.ceil(radius_m / cell_m)

    cells = []
    for i in range(-n, n + 1):
        for j in range(-n, n + 1):
            # This cell's center, offset from the true center in whole cells.
            cell_center_lat = center_lat + i * cell_deg_lat
            cell_center_lng = center_lng + j * cell_deg_lng

            # Distance from the true center to this cell's center, in meters
            # (equirectangular approximation — plenty accurate at city scale).
            dy = i * cell_deg_lat * m_per_deg_lat
            dx = j * cell_deg_lng * m_per_deg_lng
            if math.hypot(dx, dy) > radius_m:
                continue  # cell center outside the circle — skip it

            cells.append({
                "low": {"latitude": cell_center_lat - cell_deg_lat / 2,
                        "longitude": cell_center_lng - cell_deg_lng / 2},
                "high": {"latitude": cell_center_lat + cell_deg_lat / 2,
                         "longitude": cell_center_lng + cell_deg_lng / 2},
            })
    return cells


# ---------------------------------------------------------------------------
# Places API
# ---------------------------------------------------------------------------

class ApiCallCounter:
    """Tracks billable Text Search calls so the user can watch their quota."""

    def __init__(self):
        self.count = 0


def post_with_retry(session: requests.Session, body: dict, api_key: str,
                    counter: ApiCallCounter) -> dict:
    """
    POST to the Text Search endpoint. Retries on HTTP 429 and 5xx with
    exponential backoff (1s, 2s, 4s, 8s, 16s). Raises LeadGenError with a
    clear message on non-retryable errors (bad key, API not enabled, etc.).
    """
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": FIELD_MASK,
    }
    for attempt in range(MAX_RETRIES + 1):
        try:
            resp = session.post(PLACES_SEARCH_URL, json=body, headers=headers,
                                timeout=30)
        except requests.RequestException as exc:
            if attempt == MAX_RETRIES:
                raise LeadGenError(
                    f"Network failure after {MAX_RETRIES} retries: {exc}")
            wait = 2 ** attempt
            print(f"  Network error ({exc}); retrying in {wait}s...")
            time.sleep(wait)
            continue

        counter.count += 1

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                raise LeadGenError(
                    f"HTTP {resp.status_code} after {MAX_RETRIES} retries: "
                    f"{resp.text[:300]}")
            wait = 2 ** attempt
            print(f"  HTTP {resp.status_code}; retrying in {wait}s...")
            time.sleep(wait)
            continue

        # 4xx other than 429 won't succeed on retry — bail with the API's message.
        raise LeadGenError(
            f"Places API returned HTTP {resp.status_code}: {resp.text[:500]} — "
            "check that 'Places API (New)' is enabled and your key is valid "
            "(see README.md).")

    raise AssertionError("unreachable")


def search_cell(session: requests.Session, query: str, cell: dict,
                api_key: str, counter: ApiCallCounter) -> list[dict]:
    """Run a Text Search restricted to one grid cell, following pagination."""
    places: list[dict] = []
    page_token = None

    for page in range(MAX_PAGES_PER_CELL):
        body = {
            "textQuery": query,
            "locationRestriction": {"rectangle": cell},
            "maxResultCount": 20,
        }
        if page_token:
            body["pageToken"] = page_token
            time.sleep(PAGE_TOKEN_DELAY_S)  # token needs a moment to activate

        data = post_with_retry(session, body, api_key, counter)
        places.extend(data.get("places", []))

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(REQUEST_DELAY_S)

    return places


# ---------------------------------------------------------------------------
# Website classification
# ---------------------------------------------------------------------------

def extract_domain(url: str) -> str:
    """Pull the bare hostname out of a URL (no scheme, port, path, or www.)."""
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip(), flags=re.I)
    host = host.split("/")[0].split("?")[0].split(":")[0].lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def classify_website(website_uri: str | None) -> str | None:
    """
    Classify a place's web presence:
      - None/empty websiteUri            -> "No website"
      - social/placeholder domain        -> "Social only"
      - anything else (a real website)   -> None (not a lead)
    """
    if not website_uri or not website_uri.strip():
        return STATUS_NO_WEBSITE

    domain = extract_domain(website_uri)

    # Exact domain or a subdomain of one in the social list.
    for social in SOCIAL_DOMAINS:
        if domain == social or domain.endswith("." + social):
            return STATUS_SOCIAL_ONLY
    for pattern in SOCIAL_DOMAIN_PATTERNS:
        if pattern.search(domain):
            return STATUS_SOCIAL_ONLY

    return None  # has a real website — not a lead


# ---------------------------------------------------------------------------
# Search runner
# ---------------------------------------------------------------------------

def run_search(query: str, *,
               radius_miles: float = DEFAULT_RADIUS_MILES,
               center: tuple[float, float] = DEFAULT_CENTER,
               cell_km: float = DEFAULT_CELL_KM,
               include_social: bool = True,
               progress: Callable[[dict], None] | None = None) -> dict:
    """
    Search one industry and store the leads in the database under that
    industry. Existing leads (same place id) are left untouched, so clicked
    state survives re-runs. Returns a summary dict.

    `progress`, if given, is called after every grid cell with a dict:
    {"cell", "total_cells", "unique", "new_leads", "api_calls"}.
    """
    api_key = get_api_key()
    if not api_key:
        raise LeadGenError(
            "The GOOGLE_MAPS_API_KEY environment variable is not set. "
            "See README.md ('One-time Google Cloud setup').")

    query = query.strip()
    if not query:
        raise LeadGenError("Search term is empty.")

    cells = build_grid(center[0], center[1],
                       radius_miles * METERS_PER_MILE, cell_km * 1000.0)

    industry_id = db.get_or_create_industry(query)
    session = requests.Session()
    counter = ApiCallCounter()
    seen: set[str] = set()   # place ids seen this run, deduped across cells
    new_leads = 0
    no_website = social_only = excluded_with_site = 0

    for idx, cell in enumerate(cells, start=1):
        rows = []
        for place in search_cell(session, query, cell, api_key, counter):
            pid = place.get("id")
            if not pid or pid in seen:
                continue
            seen.add(pid)

            status = classify_website(place.get("websiteUri"))
            if status is None:
                excluded_with_site += 1
                continue
            if status == STATUS_NO_WEBSITE:
                no_website += 1
            else:
                social_only += 1
                if not include_social:
                    continue

            rows.append({
                "place_id": pid,
                "name": place.get("displayName", {}).get("text", ""),
                "phone": place.get("nationalPhoneNumber", ""),
                "maps_url": place.get("googleMapsUri", ""),
                "website_status": status,
                "address": place.get("formattedAddress", ""),
            })

        new_leads += db.insert_leads(industry_id, rows)

        if progress:
            progress({"cell": idx, "total_cells": len(cells),
                      "unique": len(seen), "new_leads": new_leads,
                      "api_calls": counter.count})
        time.sleep(REQUEST_DELAY_S)

    return {
        "query": query,
        "industry_id": industry_id,
        "total_cells": len(cells),
        "unique": len(seen),
        "no_website": no_website,
        "social_only": social_only,
        "excluded_with_site": excluded_with_site,
        "new_leads": new_leads,
        "api_calls": counter.count,
    }
