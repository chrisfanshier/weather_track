"""
miami_ev.py — Miami high temp market EV analyzer

Kalshi series: KXHIGHMIA
NWS source:    Official hourly forecast via points API
               (same product as weather.gov — forecaster-adjusted)

Usage:
    pip install requests pandas scipy
    python miami_ev.py
"""

import requests
import pandas as pd
import re
from datetime import datetime, timedelta
from scipy.stats import norm

# ── Config ────────────────────────────────────────────────────────────────────
KALSHI_BASE     = "https://external-api.kalshi.com/trade-api/v2"
SERIES_TICKERS  = ["KXHIGHMIA", "KXHIGHMI"]
NWS_POINT_URL   = "https://api.weather.gov/points/25.7959,-80.3187"
NWS_HEADERS     = {"User-Agent": "(miami-ev-tool, weather_arbitrage)"}

KALSHI_FEE_RATE = 0.07
MIN_EV_FLAG     = 10.0
DAY_HOUR_START  = 7
DAY_HOUR_END    = 19
MIAMI_SIGMA     = 2.5   # °F fixed prior

# ── 1. Kalshi ─────────────────────────────────────────────────────────────────
def fetch_kalshi_markets(target_date_iso):
    date_tag = datetime.strptime(target_date_iso, "%Y-%m-%d").strftime("%d%b%y").upper()

    for series in SERIES_TICKERS:
        url = f"{KALSHI_BASE}/markets"
        params = {"series_ticker": series, "status": "open"}
        try:
            r = requests.get(url, params=params, timeout=15)
            r.raise_for_status()
            raw = r.json().get("markets", [])
        except Exception as e:
            print(f"  {series}: {e}")
            continue

        dated = [m for m in raw if date_tag in m.get("event_ticker", "")]
        if not dated:
            dated = raw

        if dated:
            print(f"  Series {series}: {len(dated)} markets for {date_tag}")
            buckets = [parse_bucket(m) for m in dated]
            buckets = [b for b in buckets if b is not None]
            buckets.sort(key=lambda b: b["low"])
            return buckets

    return []

def parse_bucket(m):
    subtitle = m.get("subtitle") or m.get("yes_sub_title") or m.get("title") or ""

    def to_cents(field):
        val = m.get(field)
        if val is None: return 0
        try: return round(float(val) * 100)
        except: return 0

    yes_bid = to_cents("yes_bid_dollars")
    no_bid  = to_cents("no_bid_dollars")
    yes_ask = to_cents("yes_ask_dollars") or (100 - no_bid)
    no_ask  = to_cents("no_ask_dollars")  or (100 - yes_bid)
    volume  = float(m.get("volume_fp") or 0)
    ticker  = m.get("ticker", "")

    strike_type  = m.get("strike_type", "")
    floor_strike = m.get("floor_strike")

    if strike_type == "greater" and floor_strike is not None:
        low, high = float(floor_strike) + 1, 999.0
    elif strike_type == "less" and floor_strike is not None:
        low, high = -999.0, float(floor_strike) - 1
    else:
        low, high = parse_temp_range(subtitle)
        if low == 0 and high == 0:
            return None

    return {
        "ticker":  ticker,
        "label":   subtitle.strip(),
        "low":     low,
        "high":    high,
        "yes_bid": yes_bid,
        "no_bid":  no_bid,
        "yes_ask": yes_ask,
        "no_ask":  no_ask,
        "mkt_yes": yes_bid / 100,
        "mkt_no":  no_bid  / 100,
        "volume":  volume,
    }

def parse_temp_range(text):
    t = text.lower()
    m = re.search(r'(\d+)[°\s]*or[°\s]*below', t)
    if m: return (-999.0, float(m.group(1)))
    m = re.search(r'(\d+)[°\s]*or[°\s]*above', t)
    if m: return (float(m.group(1)), 999.0)
    m = re.search(r'(\d+)[°\s]*(?:to|-)[°\s]*(\d+)', t)
    if m: return (float(m.group(1)), float(m.group(2)))
    nums = [float(x) for x in re.findall(r'\d+\.?\d*', t)]
    if len(nums) >= 2: return (nums[0], nums[1])
    return (0, 0)

# ── 2. NWS official hourly forecast ──────────────────────────────────────────
def fetch_nws_hourly(target_date_iso):
    """
    Two-step NWS points lookup — same product as weather.gov hourly tab.
    Forecaster-adjusted, not raw model output.
    Returns (forecast_high, day_temps, forecast_office, last_update).
    """
    print(f"  Fetching NWS point metadata...")
    r = requests.get(NWS_POINT_URL, headers=NWS_HEADERS, timeout=15)
    r.raise_for_status()
    props       = r.json()["properties"]
    hourly_url  = props["forecastHourly"]
    office      = props.get("cwa", "?")
    grid        = f"{props.get('gridX','?')},{props.get('gridY','?')}"

    print(f"  Fetching hourly forecast ({office} grid {grid})...")
    r2 = requests.get(hourly_url, headers=NWS_HEADERS, timeout=15)
    r2.raise_for_status()
    data       = r2.json()
    periods    = data["properties"]["periods"]
    updated    = data["properties"].get("updateTime", "unknown")

    # Extract daytime periods for target date
    day_temps = []
    for p in periods:
        start = datetime.fromisoformat(p["startTime"].replace("Z", "+00:00"))
        # Convert to Eastern (EDT = UTC-4)
        local_dt   = start.astimezone()
        local_date = local_dt.date().isoformat()
        local_hour = local_dt.hour

        if local_date != target_date_iso:
            continue

        temp = float(p["temperature"])
        if p["temperatureUnit"] == "C":
            temp = temp * 9/5 + 32

        day_temps.append((local_hour, temp))

    day_temps.sort(key=lambda x: x[0])

    # Daytime high
    daytime = [(h, t) for h, t in day_temps if DAY_HOUR_START <= h <= DAY_HOUR_END]
    temps_to_use = daytime if daytime else day_temps

    if not temps_to_use:
        return None, [], office, updated

    forecast_high = max(t for _, t in temps_to_use)
    return forecast_high, daytime, office, updated

# ── 3. Probability model ──────────────────────────────────────────────────────
def bucket_prob(low, high, mu, sigma):
    lo = -999 if low  == -999 else low  - 0.5
    hi =  999 if high ==  999 else high + 0.5
    return norm.cdf(hi, mu, sigma) - norm.cdf(lo, mu, sigma)

def model_probs(buckets, forecast_high, sigma):
    raw   = {b["label"]: bucket_prob(b["low"], b["high"], forecast_high, sigma)
             for b in buckets}
    total = sum(raw.values())
    return {k: v / total for k, v in raw.items()} if total > 0 else raw

# ── 4. EV calculation ─────────────────────────────────────────────────────────
def fee(price_cents):
    p = price_cents / 100
    return KALSHI_FEE_RATE * p * (1 - p) * 100

def ev_pct(price_cents, true_prob):
    if price_cents <= 0: return -100.0
    profit = (100 - price_cents) - fee(price_cents)
    loss   = price_cents
    ev     = true_prob * profit - (1 - true_prob) * loss
    return ev / price_cents * 100

def evaluate(buckets, probs):
    results = []
    for b in buckets:
        mp = probs.get(b["label"])
        if mp is None: continue

        ev_y = ev_pct(b["yes_ask"], mp)
        ev_n = ev_pct(b["no_ask"],  1 - mp)
        gap  = mp * 100 - b["mkt_yes"] * 100

        if   ev_y >= ev_n and ev_y >= MIN_EV_FLAG:  best, best_ev = "BUY YES", ev_y
        elif ev_n >  ev_y and ev_n >= MIN_EV_FLAG:  best, best_ev = "BUY NO",  ev_n
        else:                                         best, best_ev = "—",       max(ev_y, ev_n)

        results.append({**b,
            "model_prob": mp,
            "gap_pts":    gap,
            "ev_yes":     ev_y,
            "ev_no":      ev_n,
            "best":       best,
            "best_ev":    best_ev,
        })
    return results

# ── 5. Display ────────────────────────────────────────────────────────────────
def display(results, forecast_high, sigma, day_temps, target_date, office, updated):
    now = datetime.now().strftime("%H:%M PT")
    # Parse update time to local
    try:
        upd_dt  = datetime.fromisoformat(updated.replace("Z", "+00:00")).astimezone()
        upd_str = upd_dt.strftime("%I:%M %p %Z")
    except Exception:
        upd_str = updated

    print(f"\n{'═'*72}")
    print(f"  MIAMI HIGH TEMP — {target_date}  (run at {now})")
    print(f"  NWS {office} forecast high: {forecast_high:.0f}°F  σ={sigma:.1f}°F")
    print(f"  Forecast last updated: {upd_str}")
    if day_temps:
        sample = "  ".join(f"{h:02d}h:{t:.0f}°" for h, t in day_temps[:8])
        print(f"  Hourly: {sample}")
    print(f"{'═'*72}")
    print(f"  {'Bucket':<18} {'MktYes':>6} {'MdlYes':>7} {'Gap':>6} "
          f"{'EV-YES':>7} {'EV-NO':>7} {'Fee':>5}  Trade")
    print(f"  {'─'*70}")

    flagged = []
    for r in results:
        flag = f"★ {r['best']}" if r["best"] != "—" else "  —"
        print(f"  {r['label']:<18} "
              f"{r['mkt_yes']*100:>5.0f}% "
              f"{r['model_prob']*100:>6.0f}% "
              f"{r['gap_pts']:>+5.0f}pt "
              f"{r['ev_yes']:>+6.0f}% "
              f"{r['ev_no']:>+6.0f}% "
              f"{fee(r['yes_ask']):>4.1f}¢  "
              f"{flag}")
        if r["best"] != "—":
            flagged.append(r)

    if flagged:
        print(f"\n  FLAGGED TRADES (EV > {MIN_EV_FLAG}%)")
        print(f"  {'─'*70}")
        for r in sorted(flagged, key=lambda x: -x["best_ev"]):
            price  = r["yes_ask"] if r["best"] == "BUY YES" else r["no_ask"]
            true_p = r["model_prob"] if r["best"] == "BUY YES" else 1 - r["model_prob"]
            net    = (100 - price) - fee(price)
            print(f"\n  {r['best']} — {r['label']}")
            print(f"    Price {price}¢  |  model {true_p*100:.1f}%  "
                  f"market {(r['mkt_yes'] if r['best']=='BUY YES' else r['mkt_no'])*100:.1f}%")
            print(f"    Fee {fee(price):.2f}¢  |  net profit if right: {net:.1f}¢  "
                  f"|  EV {r['best_ev']:+.1f}%")
    else:
        print(f"\n  No trades exceed {MIN_EV_FLAG}% EV threshold.")

    print(f"\n{'═'*72}\n")

# ── 6. Main ───────────────────────────────────────────────────────────────────
def main():
    tomorrow   = datetime.now() + timedelta(days=1)
    today      = datetime.now()
    target_iso = tomorrow.strftime("%Y-%m-%d")

    print(f"\nMiami EV Analyzer — target: {target_iso}")
    print("─" * 40)

    print("Fetching Kalshi markets...")
    buckets = fetch_kalshi_markets(target_iso)
    if not buckets:
        print(f"  No tomorrow markets, trying today...")
        target_iso = today.strftime("%Y-%m-%d")
        buckets = fetch_kalshi_markets(target_iso)

    if not buckets:
        print("No open markets found.")
        return

    print(f"  {len(buckets)} buckets parsed")

    forecast_high, day_temps, office, updated = fetch_nws_hourly(target_iso)
    if forecast_high is None:
        print("NWS fetch failed.")
        return

    probs   = model_probs(buckets, forecast_high, MIAMI_SIGMA)
    results = evaluate(buckets, probs)
    display(results, forecast_high, MIAMI_SIGMA, day_temps, target_iso, office, updated)

if __name__ == "__main__":
    main()