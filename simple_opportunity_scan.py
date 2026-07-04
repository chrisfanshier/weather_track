"""simple_opportunity_scan.py - Lightweight Kalshi weather divergence scanner.

Purpose:
- Pull current Kalshi weather bucket prices
- Pull Open-Meteo and NWS forecasts
- Rank contracts where market pricing diverges from model expectations,
  with extra weight on fat-tail contracts far from model center.

This is intentionally simple and non-executing (no order placement).

Usage:
    python simple_opportunity_scan.py
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tracker import STATIONS, fetch_kalshi_markets, fetch_nws_hourly, fetch_openmeteo

TOP_N = 60
DAY_HOUR_START = 7
DAY_HOUR_END = 19


def normal_cdf(x: float, mu: float, sigma: float) -> float:
    if sigma <= 0:
        return 0.5 if x >= mu else 0.0
    z = (x - mu) / (sigma * math.sqrt(2.0))
    return 0.5 * (1.0 + math.erf(z))


def bucket_probability_proxy(low: float, high: float, mu: float, sigma: float) -> float:
    lo = -999.0 if low == -999 else low - 0.5
    hi = 999.0 if high == 999 else high + 0.5
    return max(0.0, min(1.0, normal_cdf(hi, mu, sigma) - normal_cdf(lo, mu, sigma)))


def bucket_center(low: float, high: float) -> float:
    if low == -999:
        return high - 4.0
    if high == 999:
        return low + 4.0
    return (low + high) / 2.0


def nws_day_metrics_for_date(hourly_rows: list[dict], tz_name: str, target_date: str) -> tuple[float | None, float | None]:
    local_tz = ZoneInfo(tz_name)
    daytime = []
    all_day = []
    for r in hourly_rows:
        dt = datetime.fromisoformat(r["valid_time"].replace("Z", "+00:00")).astimezone(local_tz)
        if dt.date().isoformat() != target_date:
            continue
        if r.get("temp_f") is None:
            continue
        t = float(r["temp_f"])
        all_day.append(t)
        if DAY_HOUR_START <= dt.hour <= DAY_HOUR_END:
            daytime.append(t)

    day_high = max(daytime) if daytime else (max(all_day) if all_day else None)
    day_low = min(all_day) if all_day else None
    return day_high, day_low


def scan_once() -> list[dict]:
    run_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    target_dates = [today, tomorrow]

    out = []

    for icao, info in STATIONS.items():
        try:
            hourly = fetch_nws_hourly(info["nws_grid"])
        except Exception:
            hourly = []

        try:
            om_rows = fetch_openmeteo(info["lat"], info["lon"], info["tz"])
        except Exception:
            om_rows = []

        om_by_date = {r["forecast_date"]: r for r in om_rows}

        for market_type, key in (("high", "kalshi_high"), ("low", "kalshi_low")):
            series = info.get(key, [])
            if not series:
                continue
            try:
                contracts = fetch_kalshi_markets(series, target_dates)
            except Exception:
                contracts = []

            for c in contracts:
                tdate = c.get("target_date", today)
                om = om_by_date.get(tdate)
                nws_high, nws_low = nws_day_metrics_for_date(hourly, info["tz"], tdate)

                if om is None and nws_high is None and nws_low is None:
                    continue

                om_high = float(om["high_f"]) if om and om.get("high_f") is not None else None
                om_low = float(om["low_f"]) if om and om.get("low_f") is not None else None

                if market_type == "high":
                    m1 = om_high
                    m2 = nws_high
                else:
                    m1 = om_low
                    m2 = nws_low

                # Blend two model centers when available; fallback to whichever exists.
                if m1 is not None and m2 is not None:
                    mu = (m1 + m2) / 2.0
                    spread = abs(m1 - m2)
                elif m1 is not None:
                    mu = m1
                    spread = 2.0
                elif m2 is not None:
                    mu = float(m2)
                    spread = 2.0
                else:
                    continue

                # Proxy uncertainty widens when NWS and Open-Meteo disagree.
                sigma = max(1.8, 2.2 + 0.45 * spread)

                mkt_yes = c["yes_ask"] / 100.0
                proxy_yes = bucket_probability_proxy(c["low"], c["high"], mu, sigma)

                ctr = bucket_center(c["low"], c["high"])
                temp_gap = abs(ctr - mu)
                diff_pts = (mkt_yes - proxy_yes) * 100.0

                # Fat-tail emphasis: farther-from-center buckets get extra weight.
                tail_weight = 1.0 + (temp_gap / 6.0)
                score = abs(diff_pts) * tail_weight

                if diff_pts >= 0:
                    bias = "Tail overpriced (lean NO)"
                else:
                    bias = "Tail underpriced (lean YES)"

                out.append(
                    {
                        "run_at": run_at,
                        "icao": icao,
                        "city": info["name"],
                        "target_date": tdate,
                        "market_type": market_type,
                        "ticker": c["ticker"],
                        "label": c["label"],
                        "model_mu": round(mu, 2),
                        "model_sigma": round(sigma, 2),
                        "bucket_center": round(ctr, 2),
                        "temp_gap_f": round(temp_gap, 2),
                        "mkt_yes_pct": round(mkt_yes * 100.0, 2),
                        "proxy_yes_pct": round(proxy_yes * 100.0, 2),
                        "diff_pts": round(diff_pts, 2),
                        "score": round(score, 2),
                        "bias": bias,
                    }
                )

    out.sort(key=lambda r: r["score"], reverse=True)
    return out


def print_top(rows: list[dict], top_n: int = TOP_N) -> None:
    print(f"Simple Opportunity Scan - top {top_n} by divergence score")
    print("=" * 140)
    print(
        f"{'ICAO':<5} {'City':<16} {'Date':<10} {'Type':<4} {'Mkt YES%':>8} {'Proxy%':>8} "
        f"{'Diff':>7} {'GapF':>6} {'Score':>7} {'Bias':<28} Label"
    )
    print("-" * 140)
    for r in rows[:top_n]:
        print(
            f"{r['icao']:<5} {r['city']:<16} {r['target_date']:<10} {r['market_type']:<4} "
            f"{r['mkt_yes_pct']:>8.2f} {r['proxy_yes_pct']:>8.2f} {r['diff_pts']:>7.2f} "
            f"{r['temp_gap_f']:>6.2f} {r['score']:>7.2f} {r['bias']:<28} {r['label']}"
        )


if __name__ == "__main__":
    rows = scan_once()
    print_top(rows, TOP_N)
