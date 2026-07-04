# backtest_weather_offsets.py
import csv
import io
import math
import time
import requests
from datetime import date, timedelta
from zoneinfo import ZoneInfo

CITIES = [
    ("Atlanta",       "ATL", "ATL", "America/New_York"),
    ("Austin",        "AUS", "AUS", "America/Chicago"),
    ("Boston",        "BOS", "BOS", "America/New_York"),
    ("Chicago",       "MDW", "MDW", "America/Chicago"),
    ("Dallas",        "DFW", "DFW", "America/Chicago"),
    ("Denver",        "DEN", "DEN", "America/Denver"),
    ("Houston",       "HOU", "IAH", "America/Chicago"),  # contract may be HOU; your old list used KIAH
    ("Las Vegas",     "LAS", "LAS", "America/Los_Angeles"),
    ("Los Angeles",   "LAX", "LAX", "America/Los_Angeles"),
    ("Miami",         "MIA", "MIA", "America/New_York"),
    ("Minneapolis",   "MSP", "MSP", "America/Chicago"),
    ("New Orleans",   "MSY", "MSY", "America/Chicago"),
    ("New York",      "NYC", "NYC", "America/New_York"),
    ("Oklahoma City", "OKC", "OKC", "America/Chicago"),
    ("Philadelphia",  "PHL", "PHL", "America/New_York"),
    ("Phoenix",       "PHX", "PHX", "America/Phoenix"),
    ("San Antonio",   "SAT", "SAT", "America/Chicago"),
    ("San Francisco", "SFO", "SFO", "America/Los_Angeles"),
    ("Seattle",       "SEA", "SEA", "America/Los_Angeles"),
    ("Washington DC", "DCA", "DCA", "America/New_York"),
]

def iem_asos_url(station, d, tz):
    return (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"
        f"?station={station}"
        "&data=tmpf"
        f"&year1={d.year}&month1={d.month}&day1={d.day}&hour1=0&minute1=0"
        f"&year2={d.year}&month2={d.month}&day2={d.day}&hour2=23&minute2=59"
        f"&tz={tz.replace('/', '%2F')}"
        "&format=onlycomma&latlon=no&elev=no&missing=M&trace=T&direct=no"
        "&report_type=1&report_type=2&report_type=3"
    )

def fetch_visible_max(station, d, tz):
    r = requests.get(iem_asos_url(station, d, tz), timeout=30)
    r.raise_for_status()

    max_tmpf = None
    max_time = None
    count = 0

    reader = csv.DictReader(io.StringIO(r.text))
    for row in reader:
        val = row.get("tmpf")
        if not val or val == "M":
            continue
        try:
            tmpf = float(val)
        except ValueError:
            continue

        count += 1
        if max_tmpf is None or tmpf > max_tmpf:
            max_tmpf = tmpf
            max_time = row.get("valid")

    return max_tmpf, max_time, count

def fetch_iem_daily_max(station, d):
    # IEM daily summary endpoint. This is a fast proxy for CLI-style final max.
    url = (
        "https://mesonet.agron.iastate.edu/cgi-bin/request/daily.py"
        f"?station={station}"
        f"&year1={d.year}&month1={d.month}&day1={d.day}"
        f"&year2={d.year}&month2={d.month}&day2={d.day}"
        "&format=csv"
    )
    r = requests.get(url, timeout=30)
    r.raise_for_status()

    text = r.text.strip()
    if not text or "station" not in text.lower():
        return None

    reader = csv.DictReader(io.StringIO(text))
    for row in reader:
        # Try common max temp column names.
        for key in ("max_tmpf", "max_temp_f", "max_temp", "high"):
            if key in row and row[key] not in ("", "M", None):
                try:
                    return float(row[key])
                except ValueError:
                    pass

        # Fallback: print keys once if unknown
        return ("UNKNOWN_COLUMNS", list(row.keys()), row)

    return None

def main():
    today = date.today()
    days = [today - timedelta(days=i) for i in range(1, 8)]

    print("city,date,station,visible_max_f,visible_max_time,daily_max_f,offset_f,visible_count")

    for city, cli, station, tz in CITIES:
        for d in days:
            try:
                visible_max, visible_time, n = fetch_visible_max(station, d, tz)
                daily = fetch_iem_daily_max(station, d)

                if isinstance(daily, tuple):
                    print(f"# {city} {station} daily summary unknown columns:", daily)
                    daily_max = None
                else:
                    daily_max = daily

                offset = None
                if visible_max is not None and daily_max is not None:
                    offset = daily_max - visible_max

                print(
                    f"{city},{d},{station},"
                    f"{'' if visible_max is None else round(visible_max,2)},"
                    f"{'' if visible_time is None else visible_time},"
                    f"{'' if daily_max is None else round(daily_max,2)},"
                    f"{'' if offset is None else round(offset,2)},"
                    f"{n}"
                )

                time.sleep(0.25)

            except Exception as e:
                print(f"{city},{d},{station},ERROR,{e}")

if __name__ == "__main__":
    main()