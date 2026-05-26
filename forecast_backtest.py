"""
forecast_backtest.py

PURPOSE:  Measure GFS forecast skill for Miami daily high temperature.
          Answers: How accurate is the 24h and 48h GFS forecast vs the
          actual measured high? Output RMSE becomes sigma input for mia_ev.py.

GROUND TRUTH:  NCEI GHCND daily TMAX at KMIA (USW00012839)
               Full calendar-day max (midnight to midnight local).
               Same ASOS station source that Kalshi settles on.

FORECAST:      Open-Meteo Previous Runs API (GFS)
               previous_day1[t] = forecast for valid time t, issued 24h before t
               previous_day2[t] = forecast for valid time t, issued 48h before t
               GFS archive back to March 2021

NOTE: NWS human-adjusted forecasts are not archived anywhere.
      GFS is the best available historical proxy.

Usage:
    pip install requests pandas numpy scipy
    python forecast_backtest.py
"""

import requests
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime, timedelta
import time

MIAMI = {
    "ghcnd": "USW00012839",
    "lat":   25.7959,
    "lon":   -80.3187,
    "label": "Miami Intl (KMIA)",
    "tz":    "America/New_York",
}

KALSHI_BUCKET = 2.0
LOOKBACK_DAYS = 180
MAX_NAN_HOURS = 6   # flag a date as unreliable if >6 hourly values are missing

def fetch_ncei_tmax(ghcnd_id, start, end):
    """Full calendar-day TMAX from NCEI GHCND (units=standard => Fahrenheit)."""
    url = "https://www.ncei.noaa.gov/access/services/data/v1"
    params = {
        "dataset":   "daily-summaries",
        "stations":  ghcnd_id,
        "startDate": start,
        "endDate":   end,
        "dataTypes": "TMAX",
        "format":    "json",
        "units":     "standard",
    }
    r = requests.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data:
        return pd.Series(dtype=float)
    df = pd.DataFrame(data)
    df["date"]   = pd.to_datetime(df["DATE"]).dt.date
    df["tmax_f"] = pd.to_numeric(df["TMAX"], errors="coerce")
    return df.set_index("date")["tmax_f"].dropna()

def fetch_previous_runs(lat, lon, tz, start, end):
    """
    GFS hourly temps at exactly 24h and 48h fixed lead offsets.
    previous_day1[t] = forecast for valid time t, issued exactly 24h before t.
    previous_day2[t] = forecast for valid time t, issued exactly 48h before t.
    """
    url = "https://previous-runs-api.open-meteo.com/v1/forecast"
    params = {
        "latitude":         lat,
        "longitude":        lon,
        "hourly":           "temperature_2m_previous_day1,temperature_2m_previous_day2",
        "temperature_unit": "fahrenheit",
        "timezone":         tz,
        "start_date":       start,
        "end_date":         end,
    }
    r = requests.get(url, params=params, timeout=45)
    r.raise_for_status()
    return r.json()

def daily_forecast_high(times, temps, target_dates):
    """
    Full-day (0-23h) max temperature per calendar date.
    Matches NCEI TMAX definition: full calendar-day max, midnight to midnight.
    Returns a Series indexed by date; dates with >MAX_NAN_HOURS missing values
    are dropped as unreliable.
    """
    df = pd.DataFrame({
        "time": pd.to_datetime(times),
        "temp": pd.to_numeric(temps, errors="coerce"),
    })
    df["date"] = df["time"].dt.date
    result = {}
    for d in target_dates:
        day_df    = df[df["date"] == d]
        nan_count = day_df["temp"].isna().sum()
        if nan_count > MAX_NAN_HOURS:
            continue  # too many missing hours — skip this date
        valid = day_df["temp"].dropna()
        if not valid.empty:
            result[d] = valid.max()
    return pd.Series(result, dtype=float)

def print_verification_sample(actuals, d1_highs, d2_highs, n=10):
    """Print evenly-spaced sample rows to sanity-check alignment and values."""
    common = sorted(set(actuals.index) & set(d1_highs.index) & set(d2_highs.index))
    if not common:
        print("   No common dates for verification sample.")
        return
    step         = max(1, len(common) // n)
    sample_dates = common[::step][:n]
    print(f"\n   Verification sample ({len(common)} common dates):")
    print(f"   {'Date':<12} {'Actual':>8} {'D-2 Fcst':>10} {'D-1 Fcst':>10} {'D-2 Err':>9} {'D-1 Err':>9}")
    print(f"   {'─'*62}")
    for d in sample_dates:
        actual = actuals[d]
        d2     = d2_highs.get(d, np.nan)
        d1     = d1_highs.get(d, np.nan)
        d2_err = d2 - actual if not np.isnan(d2) else float("nan")
        d1_err = d1 - actual if not np.isnan(d1) else float("nan")
        print(f"   {str(d):<12} {actual:>7.1f}°F "
              f"{d2:>9.1f}°F "
              f"{d1:>9.1f}°F "
              f"{d2_err:>+8.1f}° "
              f"{d1_err:>+8.1f}°")


def print_error_distribution(errors, label):
    """Histogram + normality check for a set of forecast errors."""
    errors = errors.dropna()
    skewness = stats.skew(errors)
    kurtosis = stats.kurtosis(errors)
    sample   = errors[:50] if len(errors) > 50 else errors
    _, p_norm = stats.shapiro(sample)
    gaussian  = "(Gaussian OK)" if p_norm > 0.05 else "(non-Gaussian)"

    print(f"\n   {label} — error distribution (n={len(errors)}):")
    print(f"   Skewness {skewness:+.2f}  Kurtosis {kurtosis:+.2f}  "
          f"Shapiro-Wilk p={p_norm:.3f} {gaussian}")
    bins   = list(range(-7, 9))
    counts, _ = np.histogram(errors, bins=bins)
    print("   Histogram (°F bins):")
    for i, c in enumerate(counts):
        lo, hi = bins[i], bins[i + 1]
        bar = "█" * min(c, 40)
        print(f"     {lo:+d} to {hi:+d}  {bar} ({c})")


def compute_skill(info, start_str, end_str):
    print(f"\n── Miami ({info['label']})")
    print(f"   Source : GFS via Open-Meteo Previous Runs API")
    print(f"   Note   : NWS human-adjusted forecasts are not archived; GFS is best available proxy")

    print(f"\n   Fetching NCEI actuals ({info['ghcnd']})...")
    try:
        actuals = fetch_ncei_tmax(info["ghcnd"], start_str, end_str)
        print(f"   Got {len(actuals)} actual daily highs")
    except Exception as e:
        print(f"   NCEI failed: {e}"); return
    time.sleep(0.4)

    print(f"   Fetching GFS Previous Runs (day1=24h lead, day2=48h lead)...")
    try:
        data = fetch_previous_runs(info["lat"], info["lon"], info["tz"], start_str, end_str)
        time.sleep(0.4)
    except Exception as e:
        print(f"   Previous Runs API failed: {e}"); return

    times        = data["hourly"]["time"]
    target_dates = sorted(actuals.index)
    key1         = "temperature_2m_previous_day1"
    key2         = "temperature_2m_previous_day2"

    if key1 not in data["hourly"] or key2 not in data["hourly"]:
        print("   Error: expected variables not in API response")
        return

    d1_highs = daily_forecast_high(times, data["hourly"][key1], target_dates)
    d2_highs = daily_forecast_high(times, data["hourly"][key2], target_dates)

    print_verification_sample(actuals, d1_highs, d2_highs)

    # Per-lead stats
    lead_rows = []
    for label, fc_highs in [("D-2 (48h lead)", d2_highs), ("D-1 (24h lead)", d1_highs)]:
        common = sorted(set(fc_highs.index) & set(actuals.index))
        if len(common) < 10:
            print(f"   {label}: only {len(common)} common dates, skipping")
            continue
        fc     = fc_highs[common]
        ac     = actuals[common]
        errors = fc - ac
        mae    = errors.abs().mean()
        rmse   = float(np.sqrt((errors ** 2).mean()))
        bias   = errors.mean()
        e_std  = float(errors.std(ddof=1))
        w1f    = (errors.abs() <= 1.0).mean() * 100
        w2f    = (errors.abs() <= KALSHI_BUCKET).mean() * 100
        lead_rows.append({
            "label":  label,
            "n":      len(common),
            "mae":    mae,
            "rmse":   rmse,
            "bias":   bias,
            "e_std":  e_std,
            "w1f":    w1f,
            "w2f":    w2f,
            "errors": errors,
        })

    if not lead_rows:
        print("   No valid results.")
        return

    print(f"\n   {'Lead':<16} {'MAE':>7} {'RMSE':>7} {'Bias':>7} {'ErrStd':>8} {'±1°F':>6} {'±2°F':>6}  N")
    print(f"   {'─'*66}")
    for r in lead_rows:
        print(f"   {r['label']:<16} "
              f"{r['mae']:>6.2f}°F "
              f"{r['rmse']:>6.2f}°F "
              f"{r['bias']:>+6.2f}°F "
              f"{r['e_std']:>7.2f}°F "
              f"{r['w1f']:>5.0f}% "
              f"{r['w2f']:>5.0f}%  "
              f"{r['n']}")

    # D-2 -> D-1 improvement
    if len(lead_rows) == 2:
        d2r, d1r   = lead_rows[0], lead_rows[1]
        mae_delta  = d2r["mae"] - d1r["mae"]
        mae_pct    = mae_delta / d2r["mae"] * 100
        bias_delta = d1r["bias"] - d2r["bias"]
        print(f"\n   D-2 → D-1 improvement:  MAE {mae_delta:+.2f}°F ({mae_pct:.0f}%)  "
              f"bias shift {bias_delta:+.2f}°F")
        if d1r["mae"] > d2r["mae"]:
            print("   *** WARNING: D-1 MAE > D-2 MAE — data alignment issue likely ***")

    # Error distributions
    for r in lead_rows:
        print_error_distribution(r["errors"], r["label"])

    # Recommended sigma
    d1r = next((r for r in lead_rows if "24h" in r["label"]), None)
    if d1r:
        print(f"\n{'═'*60}")
        print(f"  Recommended sigma for mia_ev.py : {d1r['rmse']:.2f}°F  (D-1 RMSE)")
        print(f"  GFS cold bias at KMIA           : {d1r['bias']:+.2f}°F")
        if abs(d1r["bias"]) > 0.5:
            correction = -d1r["bias"]
            print(f"  Suggested correction in mia_ev.py: add {correction:+.2f}°F to forecast_high")
        print(f"{'═'*60}")

def main():
    # End 3 days ago so NCEI data has time to be finalized
    end_dt    = datetime.now() - timedelta(days=3)
    start_dt  = end_dt - timedelta(days=LOOKBACK_DAYS)
    start_str = start_dt.strftime("%Y-%m-%d")
    end_str   = end_dt.strftime("%Y-%m-%d")

    print(f"Miami GFS Forecast Skill Backtest")
    print(f"Period  : {start_str} → {end_str} ({LOOKBACK_DAYS} days)")
    print(f"Actuals : NCEI GHCND TMAX — full calendar-day max (midnight to midnight)")
    print(f"Forecast: GFS via Open-Meteo Previous Runs API — clean 24h / 48h lead offsets")
    print(f"Bucket  : {KALSHI_BUCKET}°F")
    print("=" * 60)

    compute_skill(MIAMI, start_str, end_str)
    print("\nDone.")

if __name__ == "__main__":
    main()