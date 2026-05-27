"""
Deutsche Bahn Real-Time Delay Scraper
Uses the official DB Timetables API (developers.deutschebahn.com)

HOW TO GET CREDENTIALS:
  1. Register at https://developers.deutschebahn.com
  2. Create an app at https://developers.deutschebahn.com/db-api-marketplace/apis/application/new
  3. Go to https://developers.deutschebahn.com/db-api-marketplace/apis/product
  4. Select "Timetables" → click Subscribe → choose your app
  5. Copy your Client ID and Client Secret below (or set as env vars)

HOW IT WORKS:
  The API works in two steps per station:
    1. GET /plan/{evaNo}/{date}/{hour}  → planned timetable (static, per hour)
    2. GET /fchg/{evaNo}               → full changes (delays, cancellations, platform changes)
  We merge them by trip ID to compute actual delays.

USAGE:
  pip install requests
  python db_delay_scraper.py                           # scrape current hour
  python db_delay_scraper.py --loop 2                  # repeat every 2 minutes
  python db_delay_scraper.py --stations 8000261 8000068 --hours 2
  python db_delay_scraper.py --search "Augsburg"       # find evaNo by name
"""

import os
import re
import csv
import time
import argparse
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()  # reads .env if present, falls back to real env vars

# ── Credentials ────────────────────────────────────────────────────────────────
CLIENT_ID     = os.getenv("DB_CLIENT_ID")
CLIENT_SECRET = os.getenv("DB_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    raise EnvironmentError(
        "Missing credentials. Copy .env.example → .env and fill in your keys."
    )
BASE_URL      = "https://apis.deutschebahn.com/db-api-marketplace/apis/timetables/v1"

# ── Well-known stations (evaNo → name) ────────────────────────────────────────
DEFAULT_STATIONS = {
    "8000261": "München Hbf",
    "8000105": "Frankfurt(Main)Hbf",
    "8011160": "Berlin Hbf",
    "8000080": "Düsseldorf Hbf",
    "8000096": "Hamburg Hbf",
    "8000068": "Augsburg Hbf",
}

CSV_FIELDS = [
    "scraped_at", "station_eva", "station_name",
    "trip_id", "train_name", "train_type",
    "planned_departure", "actual_departure", "dep_delay_min",
    "planned_arrival",   "actual_arrival",   "arr_delay_min",
    "planned_platform", "actual_platform",
    "planned_path", "changed_path",
    "cancelled", "messages",
]

# ── Auth session ───────────────────────────────────────────────────────────────
def make_session():
    s = requests.Session()
    s.headers.update({
        "DB-Client-Id":     CLIENT_ID,
        "DB-Api-Key":       CLIENT_SECRET,   # some versions use this header
        "Accept":           "application/xml",
    })
    return s

SESSION = make_session()

# ── Helpers ────────────────────────────────────────────────────────────────────
def parse_pt(val: str | None) -> str | None:
    """Convert DB time format YYMMDDHHmm → ISO-like string, or None."""
    if not val or len(val) < 10:
        return val
    try:
        dt = datetime.strptime(val, "%y%m%d%H%M")
        return dt.strftime("%Y-%m-%dT%H:%M")
    except ValueError:
        return val

def delay_minutes(planned: str | None, actual: str | None) -> float | None:
    """Return delay in minutes between two YYMMDDHHmm strings."""
    if not planned or not actual:
        return None
    try:
        fmt = "%y%m%d%H%M"
        p = datetime.strptime(planned, fmt)
        a = datetime.strptime(actual, fmt)
        return round((a - p).total_seconds() / 60, 1)
    except ValueError:
        return None

def get_xml(url: str) -> ET.Element | None:
    try:
        r = SESSION.get(url, timeout=15)
        r.raise_for_status()
        return ET.fromstring(r.text)
    except requests.HTTPError as e:
        print(f"    HTTP {e.response.status_code}: {url}")
        return None
    except Exception as e:
        print(f"    Error: {e}")
        return None

# ── API calls ─────────────────────────────────────────────────────────────────
def search_stations(pattern: str):
    """Print stations matching a name pattern."""
    url = f"{BASE_URL}/station/{pattern}"
    root = get_xml(url)
    if root is None:
        return
    print(f"\n{'evaNo':<12} Name")
    print("-" * 50)
    for s in root.findall(".//station"):  # or 'station' depending on schema
        print(f"{s.get('eva', s.get('evaNo','?')):<12} {s.get('name','?')}")

def fetch_plan(eva: str, date_str: str, hour: str) -> dict:
    """Fetch planned timetable for one station/hour. Returns {trip_id: stop_el}."""
    url = f"{BASE_URL}/plan/{eva}/{date_str}/{hour}"
    root = get_xml(url)
    plan = {}
    if root is None:
        return plan
    for s in root.findall("s"):   # <s> = stop element
        trip_id = s.get("id", "")
        plan[trip_id] = s
    return plan

def fetch_changes(eva: str) -> dict:
    """Fetch full changes (delays). Returns {trip_id: stop_el}."""
    url = f"{BASE_URL}/fchg/{eva}"
    root = get_xml(url)
    changes = {}
    if root is None:
        return changes
    for s in root.findall("s"):
        trip_id = s.get("id", "")
        changes[trip_id] = s
    return changes

# ── Merge plan + changes into CSV rows ────────────────────────────────────────
def merge_to_rows(eva: str, name: str, plan: dict, changes: dict, scraped_at: str) -> list[dict]:
    rows = []
    for trip_id, stop in plan.items():
        chg = changes.get(trip_id)

        tl = stop.find("tl")   # train line element
        dp = stop.find("dp")   # departure
        ar = stop.find("ar")   # arrival

        # planned values
        p_dep  = dp.get("pt") if dp is not None else None
        p_arr  = ar.get("pt") if ar is not None else None
        p_dplf = dp.get("pp") if dp is not None else None   # planned platform
        p_arplf= ar.get("pp") if ar is not None else None
        p_dpth = dp.get("ppth") if dp is not None else None  # planned path

        # changed values (from fchg)
        c_dep = c_arr = c_dplf = c_arplf = c_dpth = None
        cancelled = False
        messages = []

        if chg is not None:
            c_dp = chg.find("dp")
            c_ar = chg.find("ar")
            if c_dp is not None:
                c_dep  = c_dp.get("ct")  # changed time
                c_dplf = c_dp.get("cp")  # changed platform
                c_dpth = c_dp.get("cpth")
                if c_dp.get("cs") == "c":
                    cancelled = True
            if c_ar is not None:
                c_arr  = c_ar.get("ct")
                c_arplf= c_ar.get("cp")
                if c_ar.get("cs") == "c":
                    cancelled = True
            for m in chg.findall("m"):
                txt = m.get("t") or m.get("c") or ""
                if txt:
                    messages.append(txt)

        # train name: e.g. "ICE 702"
        train_cat  = tl.get("c", "") if tl is not None else ""   # category e.g. ICE
        train_no   = tl.get("n", "") if tl is not None else ""   # number e.g. 702
        train_name = f"{train_cat} {train_no}".strip()

        rows.append({
            "scraped_at":       scraped_at,
            "station_eva":      eva,
            "station_name":     name,
            "trip_id":          trip_id,
            "train_name":       train_name,
            "train_type":       train_cat,
            "planned_departure": parse_pt(p_dep),
            "actual_departure":  parse_pt(c_dep) if c_dep else parse_pt(p_dep),
            "dep_delay_min":     delay_minutes(p_dep, c_dep),
            "planned_arrival":   parse_pt(p_arr),
            "actual_arrival":    parse_pt(c_arr) if c_arr else parse_pt(p_arr),
            "arr_delay_min":     delay_minutes(p_arr, c_arr),
            "planned_platform":  p_dplf or p_arplf or "",
            "actual_platform":   c_dplf or c_arplf or "",
            "planned_path":      p_dpth or "",
            "changed_path":      c_dpth or "",
            "cancelled":         cancelled,
            "messages":          "; ".join(messages),
        })
    return rows

# ── Main scrape loop ──────────────────────────────────────────────────────────
def scrape(station_ids: list, hours_ahead: int, output: str, append: bool):
    os.makedirs(os.path.dirname(output), exist_ok=True)  # ← add this

    now = datetime.now()
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    mode = "a" if append else "w"
    total = 0

    with open(output, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if not append:
            writer.writeheader()

        for eva in station_ids:
            name = DEFAULT_STATIONS.get(eva, eva)
            print(f"  {name} ({eva})")

            # fetch changes once per station (covers all hours)
            changes = fetch_changes(eva)

            # fetch plan for each requested hour
            all_plan = {}
            for h in range(hours_ahead):
                t = now + timedelta(hours=h)
                date_str = t.strftime("%y%m%d")   # YYMMDD
                hour_str = t.strftime("%H")
                plan_slice = fetch_plan(eva, date_str, hour_str)
                all_plan.update(plan_slice)
                time.sleep(0.5)

            rows = merge_to_rows(eva, name, all_plan, changes, scraped_at)
            writer.writerows(rows)
            total += len(rows)
            delayed = sum(1 for r in rows if r["dep_delay_min"] and float(r["dep_delay_min"]) > 0)
            cancelled = sum(1 for r in rows if r["cancelled"])
            print(f"    → {len(rows)} trips, {delayed} delayed, {cancelled} cancelled")
            time.sleep(1)

    return total


def main():
    parser = argparse.ArgumentParser(description="DB Timetables API → CSV delay scraper")
    parser.add_argument("--stations", nargs="+", default=list(DEFAULT_STATIONS.keys()))
    parser.add_argument("--output", default=os.path.join(os.path.dirname(__file__), "data", f"db_delays_{datetime.now().strftime('%d%m%y')}.csv"))
    parser.add_argument("--hours", type=int, default=2,
                        help="How many hours ahead to fetch planned data (default: 2)")
    parser.add_argument("--loop", type=int, default=0, metavar="MINUTES",
                        help="Repeat every N minutes, appending rows (0 = run once)")
    parser.add_argument("--search", metavar="NAME",
                        help="Search for a station evaNo by name and exit")
    args = parser.parse_args()

    if args.search:
        search_stations(args.search)
        return

    print(f"Output: {args.output} | Stations: {len(args.stations)} | Hours ahead: {args.hours}")

    if args.loop:
        print(f"Loop mode: every {args.loop} min. Ctrl+C to stop.\n")
        run = 0
        while True:
            run += 1
            print(f"[Run #{run}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            n = scrape(args.stations, args.hours, args.output, append=(run > 1))
            print(f"  → {n} rows total written\n")
            time.sleep(args.loop * 60)
    else:
        n = scrape(args.stations, args.hours, args.output, append=False)
        print(f"\nDone! {n} rows written to {args.output}")


if __name__ == "__main__":
    main()