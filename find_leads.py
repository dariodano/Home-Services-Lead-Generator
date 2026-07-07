#!/usr/bin/env python3
"""find_leads.py — CLI for the lead generator.

Runs the same search as the web UI and stores results in leads.db, so
everything you find here shows up at http://127.0.0.1:5050 (python app.py).
Optionally also exports the industry's full list to a CSV with --output.

Requires the GOOGLE_MAPS_API_KEY environment variable. See README.md.
"""

import argparse
import csv
import sys

import db
import leadgen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find local businesses without a website using the "
                    "Google Places API (New), and store them in leads.db.",
        epilog='Example: python find_leads.py -s "HVAC contractor" -r 15',
    )
    parser.add_argument(
        "--search", "-s", required=True,
        help='Industry search term, e.g. "HVAC contractor" or "roofing company"',
    )
    parser.add_argument(
        "--radius", "-r", type=float, default=leadgen.DEFAULT_RADIUS_MILES,
        help="Search radius in MILES around the center point (default: 20)",
    )
    parser.add_argument(
        "--center", default="%g,%g" % leadgen.DEFAULT_CENTER,
        help='Center point as "lat,lng" (default: Philadelphia City Hall)',
    )
    parser.add_argument(
        "--cell-km", type=float, default=leadgen.DEFAULT_CELL_KM,
        help="Grid cell size in km; smaller = more thorough but more API "
             "calls (default: 5)",
    )
    parser.add_argument(
        "--output", "-o", default=None,
        help="Also export this industry's full lead list to a CSV",
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


def export_csv(industry_id: int, path: str) -> int:
    leads = db.get_leads(industry_id)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, quoting=csv.QUOTE_MINIMAL)
        writer.writerow(["Business Name", "Phone", "Maps Link",
                         "Website Status", "Address", "Done"])
        for lead in leads:
            writer.writerow([lead["name"], lead["phone"], lead["maps_url"],
                             lead["website_status"], lead["address"],
                             "yes" if lead["clicked"] else ""])
    return len(leads)


def main() -> None:
    args = parse_args()

    def progress(p: dict) -> None:
        print(f"Cell {p['cell']}/{p['total_cells']}: {p['unique']} unique, "
              f"{p['new_leads']} new leads (API calls: {p['api_calls']})")

    try:
        center = leadgen.parse_center(args.center)
        summary = leadgen.run_search(
            args.search, radius_miles=args.radius, center=center,
            cell_km=args.cell_km, include_social=args.include_social,
            progress=progress)
    except leadgen.LeadGenError as exc:
        sys.exit(f"Error: {exc}")

    print("\n--- Summary ---")
    print(f"Unique businesses found:  {summary['unique']}")
    print(f"  No website:             {summary['no_website']}")
    print(f"  Social only:            {summary['social_only']}"
          + ("" if args.include_social else "  (excluded via --no-social)"))
    print(f"  Excluded (have a site): {summary['excluded_with_site']}")
    print(f"API search calls made:    {summary['api_calls']}  "
          f"(first 1,000/month are free on Google's Enterprise tier)")
    print(f"New leads stored:         {summary['new_leads']} -> {db.DB_PATH.name}")

    if args.output:
        count = export_csv(summary["industry_id"], args.output)
        print(f"CSV export:               {count} leads -> {args.output}")

    print("\nWork the list in the web UI:  python app.py  "
          "->  http://127.0.0.1:5050")


if __name__ == "__main__":
    main()
