# Home Services Lead Generator

A command-line tool that finds local businesses **without a website** using the
Google Places API (New), and exports them to a CSV built for cold outreach by
phone or text.

It searches a grid of cells covering a radius around a center point (default:
Philadelphia), collects every matching business, and keeps only the ones with
**no website at all** or with **only a social-media page** (Facebook,
Instagram, Linktree, Yelp, etc.). Businesses that already have a real website
are excluded — they're not your leads.

## One-time Google Cloud setup

You need a Google Cloud API key with the **Places API (New)** enabled. This
takes about 10 minutes and only has to be done once.

1. **Create a project.** Go to the
   [Google Cloud Console](https://console.cloud.google.com/), sign in, and
   create a new project (e.g. `lead-generator`).

2. **Enable "Places API (New)".** In the console, go to **APIs & Services →
   Library**, search for **Places API (New)** — make sure it's the *(New)*
   one, not the legacy "Places API" — and click **Enable**.

3. **Enable billing.** Go to **Billing** and attach a payment method to the
   project. **This is required even if you stay entirely within the free
   tier** — Google won't serve Places API requests without billing enabled.

4. **Create a restricted API key.** Go to **APIs & Services → Credentials →
   Create credentials → API key**. Then click the new key to edit it, and
   under **API restrictions** choose **Restrict key** and select only
   **Places API (New)**. This way, if the key ever leaks, it can't be used
   for anything else.

5. **Set a budget alert.** Go to **Billing → Budgets & alerts → Create
   budget**, set it to **$10**, and keep the default email alerts at 50/90/100%.
   This is your safety net against surprise charges.

6. **Add the key to your `.env` file.** The tool reads the key from a `.env`
   file in this folder — it is never hardcoded. Open the `.env` file and paste
   your key between the quotes:

   ```
   GOOGLE_MAPS_API_KEY="AIza...your-key-here"
   ```

   Save the file and you're done — the app and CLI both pick it up
   automatically. If the `.env` file is missing, copy `.env.example` to `.env`
   first. (`.env` is git-ignored so your key never gets committed. A real
   `GOOGLE_MAPS_API_KEY` shell environment variable, if set, still takes
   precedence.)

## Install

Requires Python 3.10+. The only dependency is `requests`.

```bash
git clone <this-repo>
cd Home-Services-Lead-Generator

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
# HVAC contractors within 20 miles of Philadelphia City Hall (all defaults)
python find_leads.py --search "HVAC contractor"

# Roofers within 10 miles
python find_leads.py --search "roofing company" --radius 10

# Plumbers around a different center point (Cherry Hill, NJ), skip
# social-only businesses, custom output file
python find_leads.py -s "plumber" -r 8 --center "39.9268,-75.0246" \
    --no-social -o plumbers_cherry_hill.csv

# More thorough search with a finer 3 km grid (more API calls)
python find_leads.py -s "landscaping" -r 15 --cell-km 3
```

### Arguments

| Flag | Default | Meaning |
|---|---|---|
| `--search`, `-s` | *(required)* | Industry query, e.g. `"HVAC contractor"` |
| `--radius`, `-r` | `20` | Search radius in **miles** |
| `--center` | `"39.9526,-75.1652"` | Center as `"lat,lng"` (Philadelphia City Hall) |
| `--cell-km` | `5` | Grid cell size in km. Smaller = more thorough, more API calls |
| `--output`, `-o` | `<search>_<YYYY-MM-DD>.csv` | Output CSV path |
| `--include-social` / `--no-social` | include | Include businesses whose only web presence is a social page |

### Output CSV

Columns, in order: **Business Name, Phone, Maps Link, Website Status,
Address, Status, Notes**.

- **Website Status** is `No website` or `Social only`.
- **Status** and **Notes** are left blank for you to fill in as you work the
  list (called, texted, not interested, callback Tuesday, ...).
- Rows are sorted so `No website` leads come first, then `Social only`, and
  within each group, businesses **with a phone number** come before those
  without one — so the most actionable leads are at the top.
- The file is UTF-8 and opens directly in Excel, Google Sheets, or Numbers.

## Cost and quota

- The phone-number and website fields put each Text Search call in Google's
  **Enterprise** billing tier for Text Search.
- Each call returns **up to 20 businesses and counts as one billable event**,
  and pagination pages count as separate calls.
- The **first 1,000 calls per month are free**; the tool prints the total
  number of API calls it made at the end of every run so you can track your
  usage against that quota.
- Rough sizing: a 20-mile radius with the default 5 km grid is ~140 cells.
  Most cells return few or no results (1 call each); dense cells paginate up
  to 3 calls, so a typical default run costs roughly 150–300 calls — a few
  full-size runs per month fit in the free tier. Use a smaller `--radius` or
  a larger `--cell-km` to spend fewer calls.

## How it works

1. Converts the radius to meters and builds a bounding box around the center.
2. Tiles the box into square cells of `--cell-km`, **skipping cells whose
   center is farther than the radius from the center point** — approximating
   the circle so no calls are wasted on the box corners.
3. Runs a Text Search restricted to each cell's rectangle, following
   pagination up to 3 pages (60 results) per cell. Grid cells exist because
   a single Text Search returns at most 60 results — tiling recovers
   businesses that a single wide search would silently drop.
4. Dedupes results across cells by place ID.
5. Classifies each business: no `websiteUri` → **No website**; a
   social/placeholder domain (facebook.com, instagram.com, linktr.ee,
   nextdoor.com, yelp.com, business.site, ...) → **Social only**; anything
   else has a real website and is excluded.
6. Writes the sorted CSV and prints a summary with call counts.

The tool sleeps ~200 ms between calls and retries automatically with
exponential backoff on rate-limit (429) and server (5xx) errors.
