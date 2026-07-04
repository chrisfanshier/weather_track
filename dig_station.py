from __future__ import annotations

import math
import re
import statistics
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import time

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
    if value is None:
        return math.nan

    raw_low, raw_high = raw_interval_for_cli_bucket(low, high)

    if raw_low <= value < raw_high:
        return 0.0

    if value < raw_low:
        return raw_low - value

    return value - raw_high


def bucket_prob(member_vals, low, high):
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

MONTHS = {
    "JAN": 1,
    "FEB": 2,
    "MAR": 3,
    "APR": 4,
    "MAY": 5,
    "JUN": 6,
    "JUL": 7,
    "AUG": 8,
    "SEP": 9,
    "OCT": 10,
    "NOV": 11,
    "DEC": 12,
}


def parse_kalshi_date_from_text(text: str) -> str | None:
    """
    Parse Kalshi ticker dates like:
      KXHIGHPHIL-26MAY28
      KXHIGHPHIL-26MAY28-B80.5

    Returns YYYY-MM-DD.
    """
    if not text:
        return None

    m = re.search(r"(\d{2})([A-Z]{3})(\d{2})", text.upper())
    if not m:
        return None

    yy = int(m.group(1))
    mon = MONTHS.get(m.group(2))
    dd = int(m.group(3))

    if mon is None:
        return None

    return f"{2000 + yy:04d}-{mon:02d}-{dd:02d}"


def infer_event_key_from_ticker(ticker: str) -> str:
    """
    Example:
      KXHIGHPHIL-26MAY28-B80.5
    becomes:
      KXHIGHPHIL-26MAY28
    """
    if not ticker:
        return ""

    m = re.search(r"^(.*?\d{2}[A-Z]{3}\d{2})", ticker.upper())
    if m:
        return m.group(1)

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
    Pull Kalshi buckets using tracker.fetch_kalshi_markets(), but avoid
    mixing today/tomorrow or multiple event layouts.

    Selects the event group whose Kalshi ticker date matches target_date.
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
            event_date = (
                parse_kalshi_date_from_text(event_key)
                or parse_kalshi_date_from_text(ticker)
            )

            low = float(c["low"])
            high = float(c["high"])
            raw_low, raw_high = raw_interval_for_cli_bucket(low, high)

            rows.append(
                {
                    "event_key": event_key,
                    "event_date": event_date,
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

    matching_groups = {
        event_key: group_rows
        for event_key, group_rows in clean_groups.items()
        if any(row.get("event_date") == target_date for row in group_rows)
    }

    if matching_groups:
        best_event_key, best_rows = max(
            matching_groups.items(),
            key=lambda kv: len(kv[1]),
        )
    else:
        best_event_key, best_rows = max(
            clean_groups.items(),
            key=lambda kv: len(kv[1]),
        )

    if len(clean_groups) > 1:
        print()
        print("Kalshi returned multiple event groups:")
        for event_key, group_rows in sorted(
            clean_groups.items(),
            key=lambda kv: len(kv[1]),
            reverse=True,
        ):
            dates = sorted(set(str(r.get("event_date")) for r in group_rows))
            marker = " <-- selected" if event_key == best_event_key else ""
            print(f"  {event_key}: {len(group_rows)} contracts dates={dates}{marker}")
        print()

    best_rows.sort(key=bucket_sort_key)

    return best_rows


# =============================
# OPEN-METEO
# =============================

def _get_with_retry(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = 30,
    max_retries: int = 3,
    backoff: float = 2.0,
) -> requests.Response:
    """GET with exponential-backoff retry on transient 5xx errors (e.g. 502)."""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            r = requests.get(url, params=params, headers=headers, timeout=timeout)
            if r.status_code < 500:
                r.raise_for_status()
                return r
            last_exc = requests.HTTPError(f"HTTP {r.status_code}", response=r)
        except requests.exceptions.Timeout as exc:
            last_exc = exc
        if attempt < max_retries:
            time.sleep(backoff * (2 ** attempt))
    raise last_exc  # type: ignore[misc]


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

    r = _get_with_retry(url, params=params, timeout=30)
    return r.json()


def daily_value_from_det(payload, target_date, market_type):
    if payload is None:
        return None
    daily = payload["daily"]
    key = "temperature_2m_max" if market_type == "high" else "temperature_2m_min"

    for d, v in zip(daily["time"], daily[key]):
        if d == target_date:
            return float(v)

    return None


def hourly_curve_from_det(payload, target_date):
    if payload is None:
        return []
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

    r = _get_with_retry(url, params=params, timeout=30)
    return r.json()


def ensemble_member_extremes(payload, target_date, market_type):
    if payload is None:
        return []
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
# OPEN-METEO NAMED MODEL FAMILIES
# =============================

MODEL_FAMILIES = [
    "gfs_seamless",
    "icon_global",
    "gem_global",
    "ecmwf_ifs025",
    "ukmo_global_deterministic_10km",
]

MODEL_SHORT_NAMES = {
    "gfs_seamless": "GFS",
    "icon_global": "ICON",
    "gem_global": "GEM",
    "ecmwf_ifs025": "ECMWF",
    "ukmo_global_deterministic_10km": "UKMO",
}


def fetch_model_family_payload(info):
    """
    Pull named deterministic model-family forecasts from Open-Meteo:
    GFS, ICON, GEM, ECMWF, UKMO.

    Includes both daily highs/lows and hourly temperature curves.
    """
    url = "https://api.open-meteo.com/v1/forecast"

    params = {
        "latitude": info["lat"],
        "longitude": info["lon"],
        "timezone": info["tz"],
        "temperature_unit": "fahrenheit",
        "forecast_days": 3,
        "daily": "temperature_2m_max,temperature_2m_min",
        "hourly": "temperature_2m",
        "models": ",".join(MODEL_FAMILIES),
    }

    r = _get_with_retry(url, params=params, timeout=30)
    return r.json()


def model_family_daily_values(payload, target_date, market_type):
    """
    Return daily high/low values for each named model family.

    Shape:
      {"gfs_seamless": 82.1, "icon_global": 81.9, ...}
    """
    if payload is None:
        return {}
    daily = payload.get("daily", {})
    dates = daily.get("time", [])

    out = {}

    for model in MODEL_FAMILIES:
        key = (
            f"temperature_2m_max_{model}"
            if market_type == "high"
            else f"temperature_2m_min_{model}"
        )

        vals = daily.get(key)
        if not vals:
            continue

        for d, v in zip(dates, vals):
            if d == target_date and v is not None:
                out[model] = float(v)
                break

    return out


def find_model_hourly_key(hourly, model):
    exact = f"temperature_2m_{model}"

    if exact in hourly:
        return exact

    for key in hourly.keys():
        if key.startswith("temperature_2m") and model in key:
            return key

    return None


def model_family_hourly_curves(payload):
    """
    Convert Open-Meteo model-family hourly payload to:

      {
        "gfs_seamless": [("2026-05-29T09:00", 78.2), ...],
        ...
      }
    """
    if payload is None:
        return {}
    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])

    out = {}

    for model in MODEL_FAMILIES:
        key = find_model_hourly_key(hourly, model)
        if not key:
            continue

        vals = hourly.get(key, [])
        curve = []

        for t, v in zip(times, vals):
            if v is not None:
                curve.append((t, float(v)))

        if curve:
            out[model] = curve

    return out


def nearest_model_family_values(curves, obs_dt, tz_name):
    """
    Return nearest model-family hourly values at observation time.
    """
    if obs_dt is None:
        return {}

    tz = ZoneInfo(tz_name)
    out = {}

    for model, curve in curves.items():
        best = None

        for t, v in curve:
            try:
                dt = datetime.fromisoformat(t).replace(tzinfo=tz)
            except Exception:
                continue

            diff = abs((dt - obs_dt.astimezone(tz)).total_seconds())

            if best is None or diff < best[0]:
                best = (diff, t, v)

        if best is not None:
            out[model] = best[2]

    return out


def summarize_family_values(values):
    if not values:
        return {
            "n": 0,
            "mean": math.nan,
            "min": math.nan,
            "max": math.nan,
            "spread": math.nan,
            "compact": "",
        }

    vals = list(values.values())

    compact = " ".join(
        f"{MODEL_SHORT_NAMES.get(model, model)}:{val:.1f}"
        for model, val in values.items()
    )

    return {
        "n": len(vals),
        "mean": sum(vals) / len(vals),
        "min": min(vals),
        "max": max(vals),
        "spread": max(vals) - min(vals),
        "compact": compact,
    }


def family_bucket_inside(values, low, high):
    """
    Count named model-family values inside the CLI-rounded bucket.
    """
    raw_low, raw_high = raw_interval_for_cli_bucket(low, high)

    inside = {
        model: val
        for model, val in values.items()
        if raw_low <= val < raw_high
    }

    return {
        "n_inside": len(inside),
        "inside_names": ",".join(
            MODEL_SHORT_NAMES.get(model, model)
            for model in inside.keys()
        ),
    }


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

def classify_tracking(delta_nws, delta_om, delta_family=math.nan):
    vals = [
        x
        for x in [delta_nws, delta_om, delta_family]
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
    day_choice = input("Target day today/tomorrow [today]: ").strip().lower() or "today"

    if market_type not in ("high", "low"):
        raise SystemExit("market_type must be high or low")

    if day_choice not in ("today", "tomorrow", "t", "tmr", "tom"):
        raise SystemExit("target day must be today or tomorrow")

    icao, info = resolve_station(city)

    local_today = datetime.now(ZoneInfo(info["tz"])).date()

    if day_choice in ("today", "t"):
        target_date = local_today.isoformat()
    else:
        target_date = (local_today + timedelta(days=1)).isoformat()

    print()
    print(f"run_at={datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    print(f"{icao} {info['name']} target_date={target_date} market_type={market_type}")
    print()

    print("Pulling forecasts and observations...")

    det = None
    try:
        det = fetch_openmeteo_det(info)
    except Exception as e:
        print(f"  OM deterministic unavailable: {e}")

    ens = None
    try:
        ens = fetch_openmeteo_ens(info)
    except Exception as e:
        print(f"  OM ensemble unavailable: {e}")

    family_payload = None
    try:
        family_payload = fetch_model_family_payload(info)
    except Exception as e:
        print(f"  OM model families unavailable: {e}")

    nws_hourly_extreme, nws_period, nws_curve = fetch_nws(info, market_type, target_date)

    is_today = target_date == local_today.isoformat()

    hf_rows = []

    if is_today:
        hf_text = fetch_recent_hf_asos(icao, info)
        hf_rows = parse_hf_rows(hf_text, info)

    om_daily = daily_value_from_det(det, target_date, market_type)
    om_curve = hourly_curve_from_det(det, target_date)
    om_hourly_extreme, om_hourly_extreme_time = hourly_extreme_from_curve(
        om_curve,
        market_type,
    )

    member_vals = ensemble_member_extremes(ens, target_date, market_type)

    family_daily_values = model_family_daily_values(
        family_payload,
        target_date,
        market_type,
    )
    family_daily = summarize_family_values(family_daily_values)
    family_curves = model_family_hourly_curves(family_payload)

    obs_now = latest_obs(hf_rows) if is_today else None
    obs_ext = obs_extreme(hf_rows, market_type) if is_today else None

    nws_now = None
    nws_now_time = None
    om_now = None
    om_now_time = None

    family_now_values = {}
    family_now = {
        "n": 0,
        "mean": math.nan,
        "min": math.nan,
        "max": math.nan,
        "spread": math.nan,
        "compact": "",
    }

    if obs_now and obs_now["dt"]:
        nws_now, nws_now_time = nearest_nws_value(nws_curve, obs_now["dt"])
        om_now, om_now_time = nearest_forecast_value(
            om_curve,
            obs_now["dt"],
            info["tz"],
        )

        family_now_values = nearest_model_family_values(
            family_curves,
            obs_now["dt"],
            info["tz"],
        )
        family_now = summarize_family_values(family_now_values)

    delta_nws = obs_now["temp_f"] - nws_now if obs_now and nws_now is not None else math.nan
    delta_om = obs_now["temp_f"] - om_now if obs_now and om_now is not None else math.nan
    delta_family = (
        obs_now["temp_f"] - family_now["mean"]
        if obs_now and family_now["n"] > 0 and not math.isnan(family_now["mean"])
        else math.nan
    )

    status = classify_tracking(delta_nws, delta_om, delta_family)

    ens_mean = statistics.mean(member_vals) if member_vals else math.nan
    ens_sd = statistics.pstdev(member_vals) if len(member_vals) > 1 else math.nan
    ens_p10 = percentile(member_vals, 0.10)
    ens_p90 = percentile(member_vals, 0.90)

    print()
    print("=== TRACKING SUMMARY ===")

    if not is_today:
        print("Target is tomorrow; live observation tracking skipped.")
    else:
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
        print(
            f"Family now:       {fmt(family_now['mean'])}F "
            f"range {fmt(family_now['min'])}-{fmt(family_now['max'])}F"
        )
        print(f"Delta vs NWS now: {fmt(delta_nws, 2)}F")
        print(f"Delta vs OM now:  {fmt(delta_om, 2)}F")
        print(f"Delta vs family:  {fmt(delta_family, 2)}F")
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
    print(f"Family mean:            {fmt(family_daily['mean'])}F")
    print(f"Family min-max:         {fmt(family_daily['min'])}-{fmt(family_daily['max'])}F")
    print(f"Family spread:          {fmt(family_daily['spread'])}F")
    print(f"Family models:          {family_daily['compact'] or 'NA'}")

    print()
    print("=== KALSHI BUCKETS + CURRENT TRACKING ===")

    buckets = fetch_kalshi(icao, info, market_type, target_date)

    if not buckets:
        print("No Kalshi buckets found.")
    else:
        print(
            f"{'Bucket':<16} {'RawInt':<15} {'YES':>5} {'NO':>5} "
            f"{'ensYES':>7} {'FamIn':>5} {'FamMean':>7} {'FamSpr':>7} "
            f"{'EV_N':>7} {'obsDist':>8} {'obsExtreme':>10} {'Read'}"
        )
        print("-" * 165)

        for b in buckets:
            p_yes, p_no, n_inside = bucket_prob(member_vals, b["low"], b["high"])
            ev_n = 100 * p_no - b["no"]

            family_bucket = family_bucket_inside(
                family_daily_values,
                b["low"],
                b["high"],
            )

            raw_low, raw_high = raw_interval_for_cli_bucket(b["low"], b["high"])
            raw_interval = fmt_interval(raw_low, raw_high)

            obs_dist = (
                distance_from_bucket(obs_ext["temp_f"], b["low"], b["high"])
                if obs_ext
                else math.nan
            )

            if not is_today or not obs_ext:
                read = "tomorrow/no live obs"
                obs_extreme_str = "NA"
            elif raw_low <= obs_ext["temp_f"] < raw_high:
                read = "OBS currently rounds into bucket"
                obs_extreme_str = fmt(obs_ext["temp_f"])
            elif market_type == "high" and obs_ext["temp_f"] >= raw_high:
                read = "bucket already passed above"
                obs_extreme_str = fmt(obs_ext["temp_f"])
            elif market_type == "low" and obs_ext["temp_f"] < raw_low:
                read = "bucket already passed below"
                obs_extreme_str = fmt(obs_ext["temp_f"])
            else:
                read = status
                obs_extreme_str = fmt(obs_ext["temp_f"])

            print(
                f"{b['label']:<16} {raw_interval:<15} {b['yes']:>5.0f} {b['no']:>5.0f} "
                f"{100 * p_yes:>6.1f}% {family_bucket['n_inside']:>5} "
                f"{fmt(family_daily['mean']):>7} {fmt(family_daily['spread']):>7} "
                f"{ev_n:>+7.1f} "
                f"{obs_dist:>8.1f} {obs_extreme_str:>10} "
                f"{read} | {b['ticker']}"
            )

    if is_today:
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