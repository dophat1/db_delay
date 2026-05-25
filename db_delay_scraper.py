"""
Deutsche Bahn Real-Time Delay Scraper
Uses v5.db.transport.rest (no API key needed)

Usage:
  python db_delay_scraper.py                        # scrape once, save to db_delays.csv
  python db_delay_scraper.py --loop 5               # scrape every 5 minutes continuously
  python db_delay_scraper.py --stations 8000261 8000105   # custom stations
  python db_delay_scraper.py --search "Augsburg"    # find station IDs by name
"""

import requests
import csv
import time
import argparse
from datetime import datetime, timezone

BASE_URL = "https://v5.db.transport.rest"

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "db-delay-scraper/1.0 (personal research)",
    "Accept": "application/json",
})

# Some well-known station IDs to get you started
DEFAULT_STATIONS = {
    "8000261": "München Hbf",
    "8000105": "Frankfurt(Main)Hbf",
    "8011160": "Berlin Hbf",
    "8000080": "Düsseldorf Hbf",
    "8000096": "Hamburg Hbf",
    "8000068": "Augsburg Hbf",
}

CSV_FIELDS = [
    "scraped_at",
    "station_id",
    "station_name",
    "train_id",
    "line_name",
    "product",        # nationalExpress (ICE), national (IC), regional, suburban (S-Bahn)
    "direction",
    "planned_when",
    "actual_when",
    "delay_seconds",
    "delay_minutes",
    "platform",
    "planned_platform",
    "cancelled",
    "remarks",
]


def search_stations(query: str):
    """Search for stations by name and print their IDs."""
    print(f"\nSearching for stations matching '{query}'...")
    resp = SESSION.get(
        f"{BASE_URL}/locations",
        params={"query": query, "results": 10, "stops": "true", "addresses": "false", "poi": "false"},
        timeout=10,
    )
    resp.raise_for_status()
    results = resp.json()
    if not results:
        print("No stations found.")
        return
    print(f"{'ID':<12} {'Name'}")
    print("-" * 50)
    for r in results:
        if r.get("type") in ("stop", "station"):
            print(f"{r['id']:<12} {r['name']}")


def fetch_departures(station_id: str, duration: int = 60, results: int = 100) -> list[dict]:
    """Fetch departures for a station. Returns list of raw departure dicts."""
    resp = SESSION.get(
        f"{BASE_URL}/stops/{station_id}/departures",
        params={
            "duration": duration,   # minutes window to look ahead
            "results": results,
            "language": "en",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()


def parse_departure(dep: dict, station_id: str, station_name: str, scraped_at: str) -> dict:
    """Flatten a departure JSON object into a CSV row."""
    delay_sec = dep.get("delay")  # delay in seconds (None if unknown)
    delay_min = round(delay_sec / 60, 1) if delay_sec is not None else None

    remarks = "; ".join(
        r.get("text", "") or r.get("summary", "")
        for r in (dep.get("remarks") or [])
        if r.get("type") in ("warning", "status", "hint")
    )

    line = dep.get("line") or {}

    return {
        "scraped_at": scraped_at,
        "station_id": station_id,
        "station_name": station_name,
        "train_id": dep.get("tripId", ""),
        "line_name": line.get("name", ""),
        "product": line.get("product", ""),
        "direction": dep.get("direction", ""),
        "planned_when": dep.get("plannedWhen", ""),
        "actual_when": dep.get("when", ""),          # None if cancelled
        "delay_seconds": delay_sec,
        "delay_minutes": delay_min,
        "platform": dep.get("platform", ""),
        "planned_platform": dep.get("plannedPlatform", ""),
        "cancelled": dep.get("cancelled", False),
        "remarks": remarks,
    }


def scrape_to_csv(
    station_ids: list[str],
    output_file: str = "db_delays.csv",
    append: bool = False,
):
    """Scrape all given stations and write results to CSV."""
    mode = "a" if append else "w"
    write_header = not append

    rows_written = 0
    scraped_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    with open(output_file, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        if write_header:
            writer.writeheader()

        for station_id in station_ids:
            station_name = DEFAULT_STATIONS.get(station_id, station_id)
            print(f"  Fetching {station_name} ({station_id})...", end=" ")
            try:
                departures = fetch_departures(station_id)
                for dep in departures:
                    row = parse_departure(dep, station_id, station_name, scraped_at)
                    writer.writerow(row)
                    rows_written += 1
                print(f"{len(departures)} departures")
            except requests.HTTPError as e:
                print(f"HTTP error: {e}")
            except requests.RequestException as e:
                print(f"Request error: {e}")
            time.sleep(0.5)  # be polite between stations

    return rows_written


def main():
    parser = argparse.ArgumentParser(description="Scrape Deutsche Bahn real-time delays to CSV")
    parser.add_argument("--stations", nargs="+", default=list(DEFAULT_STATIONS.keys()),
                        help="Station IDs to scrape (default: 6 major Hbfs)")
    parser.add_argument("--output", default="db_delays.csv", help="Output CSV file")
    parser.add_argument("--loop", type=int, default=0, metavar="MINUTES",
                        help="Run in loop every N minutes (0 = run once)")
    parser.add_argument("--search", metavar="QUERY",
                        help="Search for station names/IDs and exit")
    args = parser.parse_args()

    if args.search:
        search_stations(args.search)
        return

    print(f"Stations: {', '.join(args.stations)}")
    print(f"Output:   {args.output}")

    if args.loop:
        print(f"Loop mode: every {args.loop} minutes. Press Ctrl+C to stop.\n")
        run = 0
        while True:
            run += 1
            print(f"[Run #{run}] {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
            n = scrape_to_csv(args.stations, args.output, append=(run > 1))
            print(f"  → {n} rows written to {args.output}\n")
            time.sleep(args.loop * 60)
    else:
        print()
        n = scrape_to_csv(args.stations, args.output)
        print(f"\nDone! {n} rows written to {args.output}")


if __name__ == "__main__":
    main()