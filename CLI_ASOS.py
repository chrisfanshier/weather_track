"""
cli_vs_asos.py

For each Kalshi city, pulls the last 14 NWS CLI reports and compares
the official max temp against the ASOS station daily max observations.

This tells you definitively:
  - Which ASOS station the CLI report draws from
  - How closely ASOS obs match CLI settlement values
  - Whether a city is safe for afternoon obs plays

Usage:
    pip install requests
    python cli_vs_asos.py
"""

import requests
import re
import time
from datetime import datetime, timedelta, timezone

NWS_HEADERS = {"User-Agent": "(cli-vs-asos-checker, weather_research)"}

# City config: name, NWS CLI location code, ASOS ICAO to test, timezone
CITIES = [
    ("Atlanta",       "ATL", "KATL", "America/New_York"),
    ("Austin",        "AUS", "KAUS", "America/Chicago"),
    ("Boston",        "BOS", "KBOS", "America/New_York"),
    ("Chicago",       "MDW", "KMDW", "America/Chicago"),
    ("Dallas",        "DFW", "KDFW", "America/Chicago"),
    ("Denver",        "DEN", "KDEN", "America/Denver"),
    ("Houston",       "HOU", "KIAH", "America/Chicago"),
    ("Las Vegas",     "LAS", "KLAS", "America/Los_Angeles"),
    ("Los Angeles",   "LAX", "KLAX", "America/Los_Angeles"),
    ("Miami",         "MIA", "KMIA", "America/New_York"),
    ("Minneapolis",   "MSP", "KMSP", "America/Chicago"),
    ("New Orleans",   "MSY", "KMSY", "America/Chicago"),
    ("New York",      "NYC", "KNYC", "America/New_York"),
    ("Oklahoma City", "OKC", "KOKC", "America/Chicago"),
    ("Philadelphia",  "PHL", "KPHL", "America/New_York"),
    ("Phoenix",       "PHX", "KPHX", "America/Phoenix"),
    ("San Antonio",   "SAT", "KSAT", "America/Chicago"),
    ("San Francisco", "SFO", "KSFO", "America/Los_Angeles"),
    ("Seattle",       "SEA", "KSEA", "America/Los_Angeles"),
    ("Washington DC", "DCA", "KDCA", "America/New_York"),
]

# ── 1. Fetch CLI product list ─────────────────────────────────────────────────
def fetch_cli_products(location_code: str, limit: int = 14) -> list[dict]:
    """Return list of recent CLI product metadata for a location."""
    url = "https://api.weather.gov/products"
    r = requests.get(url, headers=NWS_HEADERS,
                     params={"type": "CLI", "location": location_code, "limit": limit},
                     timeout=15)
    r.raise_for_status()
    return r.json().get("@graph", [])

# ── 2. Fetch and parse a single CLI product ───────────────────────────────────
def fetch_cli_text(product_url: str) -> str:
    r = requests.get(product_url, headers=NWS_HEADERS, timeout=15)
    r.raise_for_status()
    return r.json().get("productText", "")

def parse_cli_max(text: str) -> tuple[str | None, float | None, str | None]:
    """
    Extract date, max temp, and station name from CLI product text.
    Returns (date_iso, max_f, station_line).
    """
    # Station name is usually in the first few lines
    station_line = None
    for line in text.split("\n")[:20]:
        line = line.strip()
        if any(x in line.upper() for x in ["AIRPORT", "INTL", "STATION", "CLIMATE"]):
            if len(line) > 5:
                station_line = line
                break

    # Date: look for patterns like "MAY 26 2026" or "05/26/2026"
    date_iso = None
    date_patterns = [
        r'(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\s+(\d{1,2})\s+(\d{4})',
        r'(\d{1,2})/(\d{1,2})/(\d{4})',
    ]
    month_map = {m: i+1 for i, m in enumerate(
        ["JAN","FEB","MAR","APR","MAY","JUN","JUL","AUG","SEP","OCT","NOV","DEC"]
    )}
    for pattern in date_patterns:
        m = re.search(pattern, text[:500])
        if m:
            try:
                if "/" in pattern:
                    mo, day, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
                else:
                    mo = month_map[m.group(1)]
                    day, yr = int(m.group(2)), int(m.group(3))
                date_iso = f"{yr:04d}-{mo:02d}-{day:02d}"
                break
            except Exception:
                pass

    # Max temp: look for "MAXIMUM" or "MAX" followed by a temperature
    max_f = None
    max_patterns = [
        r'MAXIMUM\s+(\d{1,3})',
        r'MAX(?:IMUM)?\s+TEMP(?:ERATURE)?\s*[:\.]?\s*(\d{1,3})',
        r'HIGHEST\s+TEMP\s*[:\.]?\s*(\d{1,3})',
        r'MAX\s*\n\s*(\d{1,3})',
    ]
    for pattern in max_patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
            if 0 < val < 140:  # sanity check
                max_f = val
                break

    return date_iso, max_f, station_line

# ── 3. Fetch ASOS daily max for a date range ──────────────────────────────────
def fetch_asos_daily_max(icao: str, dates: list[str]) -> dict[str, float]:
    """
    For each date in dates, fetch observations and compute daily max.
    Returns {date_iso: max_f}.
    """
    results = {}
    for date_iso in dates:
        # Fetch observations for the full day
        start = f"{date_iso}T00:00:00Z"
        end   = f"{date_iso}T23:59:59Z"
        url   = f"https://api.weather.gov/stations/{icao}/observations"
        try:
            r = requests.get(url, headers=NWS_HEADERS,
                             params={"start": start, "end": end, "limit": 100},
                             timeout=15)
            r.raise_for_status()
            features = r.json().get("features", [])
            temps = []
            for f in features:
                val = f["properties"]["temperature"].get("value")
                if val is not None:
                    temps.append(val * 9/5 + 32)
            if temps:
                results[date_iso] = round(max(temps), 1)
        except Exception:
            pass
        time.sleep(0.15)
    return results

# ── 4. Compare and report ─────────────────────────────────────────────────────
def analyze_city(name: str, cli_loc: str, icao: str) -> dict:
    print(f"  {name} ({cli_loc} / {icao})...", end=" ", flush=True)

    # Fetch CLI products
    try:
        products = fetch_cli_products(cli_loc, limit=14)
    except Exception as e:
        print(f"CLI fetch failed: {e}")
        return {"city": name, "icao": icao, "error": str(e)}

    if not products:
        print("no CLI products found")
        return {"city": name, "icao": icao, "error": "no products"}

    # Parse each CLI
    cli_data = {}
    station_names = set()
    for p in products[:14]:
        try:
            text = fetch_cli_text(p["@id"])
            date_iso, max_f, station_line = parse_cli_max(text)
            if date_iso and max_f:
                cli_data[date_iso] = max_f
            if station_line:
                station_names.add(station_line.strip())
            time.sleep(0.1)
        except Exception:
            continue

    if not cli_data:
        print("could not parse CLI temps")
        return {"city": name, "icao": icao, "error": "parse failed"}

    # Fetch ASOS daily max for same dates
    asos_data = fetch_asos_daily_max(icao, sorted(cli_data.keys()))

    # Compare
    diffs = []
    rows  = []
    for date in sorted(cli_data.keys()):
        cli_t  = cli_data[date]
        asos_t = asos_data.get(date)
        diff   = round(abs(cli_t - asos_t), 1) if asos_t is not None else None
        if diff is not None:
            diffs.append(diff)
        rows.append({
            "date":   date,
            "cli_f":  cli_t,
            "asos_f": asos_t,
            "diff_f": diff,
        })

    n           = len(diffs)
    mean_diff   = round(sum(diffs) / n, 2) if n else None
    max_diff    = round(max(diffs), 1)     if n else None
    pct_within1 = round(sum(1 for d in diffs if d <= 1.0) / n * 100) if n else None
    pct_exact   = round(sum(1 for d in diffs if d == 0.0) / n * 100) if n else None
    reliable    = pct_within1 is not None and pct_within1 >= 90

    print(f"n={n}  mean_diff={mean_diff}°F  within1°F={pct_within1}%  "
          f"{'✓ RELIABLE' if reliable else '✗ CAUTION'}")

    return {
        "city":         name,
        "icao":         icao,
        "cli_location": cli_loc,
        "station_names": list(station_names),
        "n":            n,
        "mean_diff_f":  mean_diff,
        "max_diff_f":   max_diff,
        "pct_within1f": pct_within1,
        "pct_exact":    pct_exact,
        "reliable":     reliable,
        "rows":         rows,
    }

# ── 5. Main ───────────────────────────────────────────────────────────────────
def main():
    print("CLI vs ASOS Comparison — Kalshi Settlement Stations")
    print("=" * 65)
    print("Fetching last 14 days of CLI reports + ASOS observations...\n")

    results = []
    for name, cli_loc, icao, tz in CITIES:
        result = analyze_city(name, cli_loc, icao)
        results.append(result)
        time.sleep(0.3)

    # Summary table
    print("\n" + "=" * 75)
    print("SUMMARY — CLI vs ASOS Agreement")
    print("=" * 75)
    print(f"{'City':<16} {'ICAO':<6} {'N':>3} {'MeanDiff':>9} {'MaxDiff':>8} "
          f"{'Within1°F':>10} {'Exact':>6}  Verdict")
    print("-" * 75)

    reliable   = []
    unreliable = []

    for r in results:
        if "error" in r:
            print(f"{r['city']:<16} {r['icao']:<6}  —   ERROR: {r['error']}")
            continue
        verdict = "✓ USE ASOS" if r["reliable"] else "✗ CAUTION"
        print(f"{r['city']:<16} {r['icao']:<6} {r['n']:>3} "
              f"{str(r['mean_diff_f'])+'°F':>9} "
              f"{str(r['max_diff_f'])+'°F':>8} "
              f"{str(r['pct_within1f'])+'%':>10} "
              f"{str(r['pct_exact'])+'%':>6}  {verdict}")
        if r["reliable"]:
            reliable.append(r["city"])
        else:
            unreliable.append(r["city"])

        # Show detected station names
        if r.get("station_names"):
            for s in r["station_names"]:
                print(f"  → Station: {s}")

    print("\n" + "=" * 75)
    print(f"RELIABLE for afternoon obs plays ({len(reliable)}):")
    print(f"  {', '.join(reliable) if reliable else 'none'}")
    print(f"\nCAUTION — CLI may differ from ASOS ({len(unreliable)}):")
    print(f"  {', '.join(unreliable) if unreliable else 'none'}")
    print("=" * 75)

if __name__ == "__main__":
    main()