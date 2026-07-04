"""openmeteo_test.py — Standalone Open-Meteo daily forecast validation

Fetches 7-day daily high/low forecasts (°F) for a sample of cities using the
same lat/lon/tz values that tracker.py uses.  No DB, no side effects.

API: https://open-meteo.com/en/docs  (free, no key required)

Usage:
    python openmeteo_test.py
"""

import requests

OPEN_METEO_BASE = "https://api.open-meteo.com/v1/forecast"
FORECAST_DAYS   = 7

# Sample of cities covering different timezones (subset of tracker.py STATIONS)
SAMPLE = {
    "KMIA": {"name": "Miami",         "lat": 25.7959,  "lon": -80.2870,  "tz": "America/New_York"},
    "KMDW": {"name": "Chicago",       "lat": 41.7860,  "lon": -87.7522,  "tz": "America/Chicago"},
    "KDEN": {"name": "Denver",        "lat": 39.8561,  "lon": -104.6737, "tz": "America/Denver"},
    "KPHX": {"name": "Phoenix",       "lat": 33.4373,  "lon": -112.0078, "tz": "America/Phoenix"},
    "KLAX": {"name": "Los Angeles",   "lat": 33.9425,  "lon": -118.4081, "tz": "America/Los_Angeles"},
    "KSEA": {"name": "Seattle",       "lat": 47.4502,  "lon": -122.3088, "tz": "America/Los_Angeles"},
}


def fetch_openmeteo(lat: float, lon: float, tz: str) -> list[dict]:
    """Return list of {date, high_f, low_f} for the next FORECAST_DAYS days."""
    params = {
        "latitude":             lat,
        "longitude":            lon,
        "daily":                "temperature_2m_max,temperature_2m_min",
        "temperature_unit":     "fahrenheit",
        "timezone":             tz,
        "forecast_days":        FORECAST_DAYS,
    }
    resp = requests.get(OPEN_METEO_BASE, params=params, timeout=10)
    resp.raise_for_status()
    data = resp.json()

    daily  = data["daily"]
    dates  = daily["time"]
    highs  = daily["temperature_2m_max"]
    lows   = daily["temperature_2m_min"]

    return [
        {"date": d, "high_f": h, "low_f": l}
        for d, h, l in zip(dates, highs, lows)
    ]


def main() -> None:
    print(f"Open-Meteo daily forecast — {FORECAST_DAYS} days\n")
    print(f"{'ICAO':<6}  {'City':<15}  {'Date':<12}  {'High':>6}  {'Low':>6}")
    print("─" * 52)

    for icao, info in SAMPLE.items():
        try:
            rows = fetch_openmeteo(info["lat"], info["lon"], info["tz"])
            for r in rows:
                high = f"{r['high_f']:.1f}°" if r["high_f"] is not None else "  N/A"
                low  = f"{r['low_f']:.1f}°"  if r["low_f"]  is not None else "  N/A"
                print(f"{icao:<6}  {info['name']:<15}  {r['date']:<12}  {high:>6}  {low:>6}")
            print()
        except Exception as e:
            print(f"{icao:<6}  {info['name']:<15}  ERROR: {e}\n")


if __name__ == "__main__":
    main()
