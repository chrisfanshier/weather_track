from __future__ import annotations

import math
import statistics
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import requests

from tracker import STATIONS, fetch_kalshi_markets


# =============================
# BASIC HELPERS
# =============================

def fmt(x, digits=1):
    if x is None:
        return "NA"
    if isinstance(x, float) and math.isnan(x):
        return "NA"
    if x == math.inf:
        return "inf"
    if x == -math.inf:
        return "-inf"
    return f"{x:.{digits}f}"


def fmt_interval(low, high):
    if low == -math.inf:
        return f"<{fmt(high)}"
    if high == math.inf:
        return f">={fmt(low)}"
    return f"{fmt(low)}-{fmt(high)}"


def percentile(vals, p):
    vals = sorted(vals)
    if not vals:
        return math.nan
    if len(vals) == 1:
        return vals[0]

    k = (len(vals) - 1) * p
    lo = math.floor(k)
    hi = math.ceil(k)

    if lo == hi:
        return vals[int(k)]

    return vals[lo] * (hi - k) + vals[hi] * (k - lo)


def parse_tgroup_f(raw):
    import re

    m = re.search(r"T(\d{4})(\d{4})", raw or "")
    if not m:
        return None

    block = m.group(1)

    if block.startswith("1"):
        c = -1 * (int(block[1:]) / 10)
    else:
        c = int(block[1:]) / 10

    return c * 9 / 5 + 32


def raw_interval_for_cli_bucket(low, high):
    """
    Convert integer Kalshi/CLI outcome bucket into raw-temperature interval.

    Examples:
      54-55      => 53.5 <= raw < 55.5
      52-53      => 51.5 <= raw < 53.5
      <=47       => raw < 47.5
      >=56       => raw >= 55.5
    """
    raw_low = -math.inf if low == -999 else low - 0.5
    raw_high = math.inf if high == 999 else high + 0.5
    return raw_low, raw_high


def distance_from_bucket(value, low, high):
    """
    Distance from raw value to the CLI-rounded bucket interval.
    """
    if value is None:
        return math.nan

    raw_low, raw_high = raw_interval_for_cli_bucket(low, high)

    if raw_low <= value < raw_high:
        return 0.0

    if value < raw_low:
        return raw_low - value

    return value - raw_high


def bucket_prob(member_vals, low, high):
    """
    Ensemble probability using CLI-rounded bucket intervals.
    """
    n = len(member_vals)

    if not n:
        return math.nan, math.nan, 0

    raw_low, raw_high = raw_interval_for_cli_bucket(low, high)

    inside = [x for x in member_vals if raw_low <= x < raw_high]
    p_yes = len(inside) / n
    p_no = 1 - p_yes

    return p_yes, p_no, len(inside)


# =============================
# STATION RESOLUTION
# =============================

def resolve_station(query: str):
    q = query.strip().upper()

    if q in STATIONS:
        return q, STATIONS[q]

    if not q.startswith("K") and ("K" + q) in STATIONS:
        return "K" + q, STATIONS["K" + q]

    matches = []

    for icao, info in STATIONS.items():
        name = info.get("name", "")
        if q in icao.upper() or q in name.upper():
            matches.append((icao, info))

    if len(matches) == 1:
        return matches[0]

    if not matches:
        raise SystemExit(f"No station match for: {query}")

    print("Multiple matches:")
    for icao, info in matches:
        print(f"  {icao} {info.get('name')}")

    raise SystemExit("Use a more specific ICAO/name.")


# =============================
# KALSHI
# =============================

def infer_event_key_from_ticker(ticker: str) -> str:
    """
    Infer the Kalshi event key from a market ticker.

    Most Kalshi temperature market tickers look like:
      EVENT_TICKER-CONTRACT_SUFFIX

    This strips only the final contract suffix so contracts from the same
    event group together.
    """
    if not ticker:
        return ""

    parts = ticker.split("-")

    if len(parts) <= 1:
        return ticker

    return "-".join(parts[:-1])


def bucket_sort_key(row: dict) -> tuple[float, float, str]:
    return (
        float(row["low"]),
        float(row["high"]),
        row.get("label", ""),
    )


def fetch_kalshi(icao, info, market_type, target_date):
    """
    Pull Kalshi buckets using your existing tracker normalization, but avoid
    mixing multiple event layouts for the same station/date.

    If fetch_kalshi_markets returns more than one inferred event group, this
    chooses the group with the most contracts and prints a diagnostic.
    """
    key = "kalshi_high" if market_type == "high" else "kalshi_low"
    series = info.get(key, [])

    if not series:
        return []

    contracts = fetch_kalshi_markets(series, [target_date])

    rows = []

    for c in contracts:
        if c.get("target_date") != target_date:
            continue

        try:
            ticker = c.get("ticker", "")
            event_key = c.get("event_ticker") or infer_event_key_from_ticker(ticker)

            low = float(c["low"])
            high = float(c["high"])
            raw_low, raw_high = raw_interval_for_cli_bucket(low, high)

            rows.append(
                {
                    "event_key": event_key,
                    "ticker": ticker,
                    "label": c["label"],
                    "low": low,
                    "high": high,
                    "raw_low": raw_low,
                    "raw_high": raw_high,
                    "yes": float(c["yes_ask"]),
                    "no": float(c["no_ask"]),
                }
            )
        except Exception:
            continue

    if not rows:
        return []

    groups = {}

    for row in rows:
        groups.setdefault(row["event_key"], []).append(row)

    clean_groups = {}

    for event_key, group_rows in groups.items():
        seen = set()
        deduped = []

        for row in group_rows:
            ticker = row.get("ticker", "")

            if ticker in seen:
                continue

            seen.add(ticker)
            deduped.append(row)

        clean_groups[event_key] = deduped

    best_event_key, best_rows = max(
        clean_groups.items(),
        key=lambda kv: len(kv[1]),
    )

    if len(clean_groups) > 1:
        print()
        print("Kalshi returned multiple event groups; using the largest one:")
        for event_key, group_rows in sorted(
            clean_groups.items(),
            key=lambda kv: len(kv[1]),
            reverse=True,
        ):
            print(f"  {event_key}: {len(group_rows)} contracts")
        print(f"Selected: {best_event_key}")
        print()

    best_rows.sort(key=bucket_sort_key)

    return best_rows


# =============================
# OPEN-METEO
# =============================

def fetch_openmeteo_det(info):
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": info["lat"],
        "longitude": info["lon"],
        "timezone": info["tz"],
        "temperature_unit": "fahrenheit",
        "forecast_days": 3,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


def daily_value_from_det(payload, target_date, market_type):
    daily = payload["daily"]
    key = "temperature_2m_max" if market_type == "high" else "temperature_2m_min"

    for d, v in zip(daily["time"], daily[key]):
        if d == target_date:
            return float(v)

    return None


def hourly_curve_from_det(payload, target_date):
    hourly = payload["hourly"]

    out = []

    for t, v in zip(hourly["time"], hourly["temperature_2m"]):
        if v is not None and str(t).startswith(target_date):
            out.append((t, float(v)))

    return out


def hourly_extreme_from_curve(curve, market_type):
    """
    Returns (value, time).
    """
    if not curve:
        return None, None

    if market_type == "high":
        t, v = max(curve, key=lambda x: x[1])
    else:
        t, v = min(curve, key=lambda x: x[1])

    return v, t


def nearest_forecast_value(curve, obs_dt, tz_name):
    """
    Find Open-Meteo hourly value closest to observation timestamp.
    Open-Meteo times are local naive strings like 2026-05-28T14:00.
    """
    if not curve or obs_dt is None:
        return None, None

    tz = ZoneInfo(tz_name)
    best = None

    for t, v in curve:
        try:
            dt = datetime.fromisoformat(t).replace(tzinfo=tz)
        except Exception:
            continue

        diff = abs((dt - obs_dt.astimezone(tz)).total_seconds())

        if best is None or diff < best[0]:
            best = (diff, t, v)

    if best is None:
        return None, None

    return best[2], best[1]


def fetch_openmeteo_ens(info):
    url = "https://ensemble-api.open-meteo.com/v1/ensemble"

    params = {
        "latitude": info["lat"],
        "longitude": info["lon"],
        "timezone": info["tz"],
        "temperature_unit": "fahrenheit",
        "forecast_days": 3,
        "hourly": "temperature_2m",
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    return r.json()


def ensemble_member_extremes(payload, target_date, market_type):
    hourly = payload["hourly"]
    times = hourly["time"]
    keys = [k for k in hourly.keys() if k.startswith("temperature_2m_member")]

    out = []

    for k in keys:
        vals = [
            float(v)
            for t, v in zip(times, hourly[k])
            if v is not None and str(t).startswith(target_date)
        ]

        if vals:
            out.append(max(vals) if market_type == "high" else min(vals))

    return out


# =============================
# NWS
# =============================

def fetch_nws(info, market_type, target_date):
    points = requests.get(
        f"https://api.weather.gov/points/{info['lat']},{info['lon']}",
        headers={"User-Agent": "station-dig/1.0"},
        timeout=30,
    )
    points.raise_for_status()
    props = points.json()["properties"]

    hourly = requests.get(
        props["forecastHourly"],
        headers={"User-Agent": "station-dig/1.0"},
        timeout=30,
    )
    hourly.raise_for_status()

    hvals = []

    for p in hourly.json()["properties"]["periods"]:
        start = p["startTime"]

        local_date = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(
            ZoneInfo(info["tz"])
        ).date().isoformat()

        if local_date == target_date:
            hvals.append((float(p["temperature"]), start))

    forecast = requests.get(
        props["forecast"],
        headers={"User-Agent": "station-dig/1.0"},
        timeout=30,
    )
    forecast.raise_for_status()

    pvals = []

    for p in forecast.json()["properties"]["periods"]:
        start = p["startTime"]

        local_date = datetime.fromisoformat(start.replace("Z", "+00:00")).astimezone(
            ZoneInfo(info["tz"])
        ).date().isoformat()

        if local_date != target_date:
            continue

        if market_type == "high" and p.get("isDaytime"):
            pvals.append(float(p["temperature"]))
        elif market_type == "low" and not p.get("isDaytime"):
            pvals.append(float(p["temperature"]))

    if hvals:
        hourly_extreme = (
            max(hvals, key=lambda x: x[0])
            if market_type == "high"
            else min(hvals, key=lambda x: x[0])
        )
    else:
        hourly_extreme = (None, None)

    period_extreme = None

    if pvals:
        period_extreme = max(pvals) if market_type == "high" else min(pvals)

    return hourly_extreme, period_extreme, hvals


def nearest_nws_value(nws_curve, obs_dt):
    """
    NWS startTime values include timezone offsets.
    """
    if not nws_curve or obs_dt is None:
        return None, None

    best = None

    for val, start in nws_curve:
        try:
            dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except Exception:
            continue

        diff = abs((dt - obs_dt).total_seconds())

        if best is None or diff < best[0]:
            best = (diff, start, val)

    if best is None:
        return None, None

    return best[2], best[1]


# =============================
# OBSERVATIONS
# =============================

def fetch_recent_metar(icao):
    url = f"https://aviationweather.gov/api/data/metar?ids={icao}&format=json&hours=8"

    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"METAR fetch failed for {icao}: {e}")
        return []


def fetch_recent_hf_asos(icao, info):
    station = icao[1:] if icao.startswith("K") else icao

    now = datetime.now(ZoneInfo(info["tz"]))
    start = now - timedelta(hours=8)

    url = "https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py"

    params = {
        "station": station,
        "data": ["tmpf", "metar"],
        "year1": start.year,
        "month1": start.month,
        "day1": start.day,
        "hour1": start.hour,
        "minute1": start.minute,
        "year2": now.year,
        "month2": now.month,
        "day2": now.day,
        "hour2": now.hour,
        "minute2": now.minute,
        "tz": info["tz"],
        "format": "onlycomma",
        "latlon": "no",
        "elev": "no",
        "missing": "M",
        "trace": "T",
        "direct": "no",
        "report_type": ["1", "2", "3"],
    }

    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()

    return r.text


def parse_hf_rows(txt, info):
    rows = []
    tz = ZoneInfo(info["tz"])

    for line in txt.splitlines():
        if not line or line.startswith("station"):
            continue

        parts = line.split(",", 3)

        if len(parts) < 4:
            continue

        station, valid, tmpf, raw = parts
        tf = parse_tgroup_f(raw)

        val = tf

        if val is None and tmpf != "M":
            try:
                val = float(tmpf)
            except Exception:
                val = None

        try:
            dt = datetime.strptime(valid, "%Y-%m-%d %H:%M").replace(tzinfo=tz)
        except Exception:
            dt = None

        if val is not None:
            rows.append(
                {
                    "valid": valid,
                    "dt": dt,
                    "temp_f": val,
                    "tmpf": tmpf,
                    "raw": raw,
                }
            )

    return rows


def obs_extreme(rows, market_type):
    if not rows:
        return None

    if market_type == "high":
        return max(rows, key=lambda x: x["temp_f"])

    return min(rows, key=lambda x: x["temp_f"])


def latest_obs(rows):
    if not rows:
        return None

    dated = [r for r in rows if r["dt"] is not None]

    if dated:
        return max(dated, key=lambda x: x["dt"])

    return rows[-1]


# =============================
# INTERPRETATION
# =============================

def classify_tracking(delta_nws, delta_om):
    vals = [
        x
        for x in [delta_nws, delta_om]
        if x is not None and not (isinstance(x, float) and math.isnan(x))
    ]

    if not vals:
        return "tracking unknown"

    avg = sum(vals) / len(vals)

    if avg >= 2.0:
        return "running hot vs forecast"
    if avg >= 0.75:
        return "running slightly hot vs forecast"
    if avg <= -2.0:
        return "running cold vs forecast"
    if avg <= -0.75:
        return "running slightly cold vs forecast"

    return "tracking near forecast"


# =============================
# MAIN
# =============================

def main():
    city = input("City / ICAO: ").strip()
    market_type = input("Market type high/low [low]: ").strip().lower() or "low"

    if market_type not in ("high", "low"):
        raise SystemExit("market_type must be high or low")

    icao, info = resolve_station(city)
    target_date = datetime.now(ZoneInfo(info["tz"])).date().isoformat()

    print()
    print(f"run_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"{icao} {info['name']} target_date={target_date} market_type={market_type}")
    print()

    print("Pulling forecasts and observations...")

    det = fetch_openmeteo_det(info)
    ens = fetch_openmeteo_ens(info)
    nws_hourly_extreme, nws_period, nws_curve = fetch_nws(info, market_type, target_date)
    hf_text = fetch_recent_hf_asos(icao, info)
    hf_rows = parse_hf_rows(hf_text, info)

    om_daily = daily_value_from_det(det, target_date, market_type)
    om_curve = hourly_curve_from_det(det, target_date)
    om_hourly_extreme, om_hourly_extreme_time = hourly_extreme_from_curve(
        om_curve,
        market_type,
    )
    member_vals = ensemble_member_extremes(ens, target_date, market_type)

    obs_now = latest_obs(hf_rows)
    obs_ext = obs_extreme(hf_rows, market_type)

    nws_now = None
    nws_now_time = None
    om_now = None
    om_now_time = None

    if obs_now and obs_now["dt"]:
        nws_now, nws_now_time = nearest_nws_value(nws_curve, obs_now["dt"])
        om_now, om_now_time = nearest_forecast_value(
            om_curve,
            obs_now["dt"],
            info["tz"],
        )

    delta_nws = obs_now["temp_f"] - nws_now if obs_now and nws_now is not None else math.nan
    delta_om = obs_now["temp_f"] - om_now if obs_now and om_now is not None else math.nan

    status = classify_tracking(delta_nws, delta_om)

    ens_mean = statistics.mean(member_vals) if member_vals else math.nan
    ens_sd = statistics.pstdev(member_vals) if len(member_vals) > 1 else math.nan
    ens_p10 = percentile(member_vals, 0.10)
    ens_p90 = percentile(member_vals, 0.90)

    print()
    print("=== TRACKING SUMMARY ===")

    if obs_now:
        print(
            f"Latest obs:       {fmt(obs_now['temp_f'])}F at {obs_now['valid']} "
            f"(raw TMPF={obs_now['tmpf']})"
        )

    if obs_ext:
        print(
            f"Observed {market_type} so far: {fmt(obs_ext['temp_f'])}F at {obs_ext['valid']}"
        )

    print(f"NWS now:          {fmt(nws_now)}F at {nws_now_time or 'NA'}")
    print(f"OM now:           {fmt(om_now)}F at {om_now_time or 'NA'}")
    print(f"Delta vs NWS now: {fmt(delta_nws, 2)}F")
    print(f"Delta vs OM now:  {fmt(delta_om, 2)}F")
    print(f"STATUS:           {status}")

    print()
    print("=== DAILY FORECAST EXTREMES ===")
    print(f"NWS hourly {market_type}:      {fmt(nws_hourly_extreme[0])}F at {nws_hourly_extreme[1]}")
    print(f"NWS period {market_type}:      {fmt(nws_period)}F")
    print(f"OM daily {market_type}:        {fmt(om_daily)}F")
    print(f"OM hourly {market_type}:       {fmt(om_hourly_extreme)}F at {om_hourly_extreme_time}")
    print(f"OM ensemble mean:       {fmt(ens_mean)}F")
    print(f"OM ensemble sd:         {fmt(ens_sd)}F")
    print(f"OM ensemble p10-p90:    {fmt(ens_p10)}-{fmt(ens_p90)}F")

    print()
    print("=== KALSHI BUCKETS + CURRENT TRACKING ===")

    buckets = fetch_kalshi(icao, info, market_type, target_date)

    if not buckets:
        print("No Kalshi buckets found.")
    else:
        print(
            f"{'Bucket':<16} {'RawInt':<15} {'YES':>5} {'NO':>5} "
            f"{'ensYES':>7} {'EV_N':>7} {'obsDist':>8} {'obsExtreme':>10} {'Read'}"
        )
        print("-" * 135)

        for b in buckets:
            p_yes, p_no, n_inside = bucket_prob(member_vals, b["low"], b["high"])
            ev_n = 100 * p_no - b["no"]

            obs_dist = (
                distance_from_bucket(obs_ext["temp_f"], b["low"], b["high"])
                if obs_ext
                else math.nan
            )

            raw_low, raw_high = raw_interval_for_cli_bucket(b["low"], b["high"])
            raw_interval = fmt_interval(raw_low, raw_high)

            if obs_ext and raw_low <= obs_ext["temp_f"] < raw_high:
                read = "OBS currently rounds into bucket"
            elif obs_ext and market_type == "high" and obs_ext["temp_f"] >= raw_high:
                read = "bucket already passed above"
            elif obs_ext and market_type == "low" and obs_ext["temp_f"] < raw_low:
                read = "bucket already passed below"
            else:
                read = status

            print(
                f"{b['label']:<16} {raw_interval:<15} {b['yes']:>5.0f} {b['no']:>5.0f} "
                f"{100 * p_yes:>6.1f}% {ev_n:>+7.1f} "
                f"{obs_dist:>8.1f} {fmt(obs_ext['temp_f']) if obs_ext else 'NA':>10} "
                f"{read} | {b['ticker']}"
            )

    print()
    print("=== Recent METAR ===")

    metars = fetch_recent_metar(icao)

    if not metars:
        print("No METAR data returned from aviationweather.gov; continuing with HF-ASOS/MADISHF.")
    else:
        for m in metars:
            raw = m.get("rawOb", "")
            tf = parse_tgroup_f(raw)

            obs = datetime.fromtimestamp(
                int(m["obsTime"]),
                tz=timezone.utc,
            ).astimezone(ZoneInfo(info["tz"]))

            print(f"{obs} | decodedC={m.get('temp')} | TgroupF={fmt(tf)} | {raw}")

    print()
    print("=== RECENT HF-ASOS / MADISHF LAST 30 ROWS ===")

    for r in hf_rows[-30:]:
        print(f"{r['valid']} | {fmt(r['temp_f'])}F | TMPF={r['tmpf']:<5} | {r['raw']}")


if __name__ == "__main__":
    main()