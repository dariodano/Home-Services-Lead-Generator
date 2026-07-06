#!/usr/bin/env python3
"""
find_leads.py — Find local businesses WITHOUT a website for cold-outreach lead generation.

Searches the Google Places API (New) Text Search endpoint across a grid of cells
covering a circular radius around a center point, then exports businesses that
have no website (or only a social-media page) to a CSV built for working the
list manually by phone/text.

Requires the GOOGLE_MAPS_API_KEY environment variable. See README.md for setup.
"""

import argparse
import csv
import math
import os
import re
import sys
import time
from datetime import date

import requests

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


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find local businesses without a website using the "
                    "Google Places API (New), and export them to a CSV for "
                    "cold outreach.",
        epilog='Example: python find_leads.py -s "HVAC contractor" -r 15',
    )
    parser.add_argument(
        "--search", "-s", required=True,
        help='Industry search term, e.g. "HVAC contractor" or "roofing company"',
    )
    parser.add_argument(
        "--radius", "-r", type=float, default=20,
        help="Search radius in MILES around the center point (default: 20)",
    )
    parser.add_argument(
        "--center", default="39.9526,-75.1652",
        help='Center point as "lat,lng" (default: Philadelphia City Hall)',
    )
    parser.add_argument(
        "--cell-km", type=float, default=5,
        help="Grid cell size in km; smaller = more thorough but more API "
             "calls (default: 5)",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help='Output CSV path (default: "<search>_<YYYY-MM-DD>.csv")',
    )
    parser.add_argument(
        "--include-social", action=argparse.BooleanOptionalAction, default=True,
        help="Include businesses whose only web presence is a social page "
             "(Facebook, Instagram, Linktree, ...). (default: include)",
    )
    # Shorter alias for --no-include-social.
    parser.add_argument(
        "--no-social", dest="include_social", action="store_false",
        help="Exclude social-only businesses (alias for --no-include-social)",
    )
    return parser.parse_args()


def parse_center(center: str) -> tuple[float, float]:
    """Parse a "lat,lng" string into a (lat, lng) float tuple."""
    try:
        lat_str, lng_str = center.split(",")
        lat, lng = float(lat_str.strip()), float(lng_str.strip())
    except ValueError:
        sys.exit(f'Error: --center must be "lat,lng", got: {center!r}')
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        sys.exit(f"Error: --center out of range: {center!r}")
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
    exponential backoff (1s, 2s, 4s, 8s, 16s). Exits with a clear message on
    non-retryable errors (bad key, API not enabled, etc.).
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
                sys.exit(f"Error: network failure after {MAX_RETRIES} retries: {exc}")
            wait = 2 ** attempt
            print(f"  Network error ({exc}); retrying in {wait}s...")
            time.sleep(wait)
            continue

        counter.count += 1

        if resp.status_code == 200:
            return resp.json()

        if resp.status_code == 429 or resp.status_code >= 500:
            if attempt == MAX_RETRIES:
                sys.exit(f"Error: HTTP {resp.status_code} after {MAX_RETRIES} "
                         f"retries: {resp.text[:300]}")
            wait = 2 ** attempt
            print(f"  HTTP {resp.status_code}; retrying in {wait}s...")
            time.sleep(wait)
            continue

        # 4xx other than 429 won't succeed on retry — bail with the API's message.
        sys.exit(f"Error: Places API returned HTTP {resp.status_code}: "
                 f"{resp.text[:500]}\n"
                 "Check that 'Places API (New)' is enabled and your key is "
                 "valid (see README.md).")

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
      - anything else (a real website)   -> None (exclude from output)
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
# Main
# ---------------------------------------------------------------------------

def default_output_name(search: str) -> str:
    """Build "<search>_<YYYY-MM-DD>.csv" with a filesystem-safe search term."""
    safe = re.sub(r"[^\w-]+", "_", search.strip()).strip("_").lower()
    return f"{safe}_{date.today().isoformat()}.csv"


def main() -> None:
    args = parse_args()

    api_key = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
    if not api_key:
        print("Error: the GOOGLE_MAPS_API_KEY environment variable is not set.\n"
              "See README.md ('One-time Google Cloud setup') for how to create\n"
              "an API key, then run:\n\n"
              '  export GOOGLE_MAPS_API_KEY="your-key-here"\n',
              file=sys.stderr)
        sys.exit(1)

    center_lat, center_lng = parse_center(args.center)
    radius_m = args.radius * METERS_PER_MILE
    cell_m = args.cell_km * 1000.0
    output_path = args.output or default_output_name(args.search)

    cells = build_grid(center_lat, center_lng, radius_m, cell_m)
    print(f'Searching for "{args.search}" within {args.radius:g} miles of '
          f"({center_lat}, {center_lng})")
    print(f"Grid: {len(cells)} cells of {args.cell_km:g} km "
          f"(up to {MAX_PAGES_PER_CELL} pages / 60 results per cell)\n")

    counter = ApiCallCounter()
    session = requests.Session()
    seen: dict[str, dict] = {}  # place id -> place, deduped across cells

    for idx, cell in enumerate(cells, start=1):
        found = search_cell(session, args.search, cell, api_key, counter)
        new = 0
        for place in found:
            pid = place.get("id")
            if pid and pid not in seen:
                seen[pid] = place
                new += 1
        print(f"Cell {idx}/{len(cells)}: {len(found)} results, {new} new "
              f"(total unique: {len(seen)}, API calls: {counter.count})")
        time.sleep(REQUEST_DELAY_S)

    # Classify every unique place and keep only the leads.
    leads = []
    excluded_with_site = 0
    social_only_seen = 0
    for place in seen.values():
        status = classify_website(place.get("websiteUri"))
        if status is None:
            excluded_with_site += 1
            continue
        if status == STATUS_SOCIAL_ONLY:
            social_only_seen += 1
            if not args.include_social:
                continue
        leads.append({
            "Business Name": place.get("displayName", {}).get("text", ""),
            "Phone": place.get("nationalPhoneNumber", ""),
            "Maps Link": place.get("googleMapsUri", ""),
            "Website Status": status,
            "Address": place.get("formattedAddress", ""),
            "Status": "",
            "Notes": "",
        })

    # Sort: "No website" first, then "Social only"; within each group, rows
    # with a phone number come before rows without one.
    leads.sort(key=lambda row: (
        0 if row["Website Status"] == STATUS_NO_WEBSITE else 1,
        0 if row["Phone"] else 1,
        row["Business Name"].lower(),
    ))

    fieldnames = ["Business Name", "Phone", "Maps Link", "Website Status",
                  "Address", "Status", "Notes"]
    with open(output_path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames,
                                quoting=csv.QUOTE_MINIMAL)
        writer.writeheader()
        writer.writerows(leads)

    no_site = sum(1 for r in leads if r["Website Status"] == STATUS_NO_WEBSITE)

    print("\n--- Summary ---")
    print(f"Unique businesses found:  {len(seen)}")
    print(f"  No website:             {no_site}")
    print(f"  Social only:            {social_only_seen}"
          + ("" if args.include_social else "  (excluded via --no-social)"))
    print(f"  Excluded (have a site): {excluded_with_site}")
    print(f"API search calls made:    {counter.count}  "
          f"(first 1,000/month are free on Google's Enterprise tier)")
    print(f"Leads written:            {len(leads)} -> {output_path}")


if __name__ == "__main__":
    main()
