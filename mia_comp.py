import requests
import pandas as pd
import math
import re
from datetime import datetime, timedelta

def get_dynamic_kalshi_arbitrage():
    # 1. Fetch Tomorrow's Active Weather Brackets from Kalshi API
    tomorrow_date_str = (datetime.now() + timedelta(days=1)).strftime("%y%b%d").upper()
    series_ticker = "KXHIGHMIA"
    event_ticker = f"{series_ticker}-{tomorrow_date_str}"
    
    # Updated to the correct host and endpoint prefix
    base_url = "https://external-api.kalshi.com/trade-api/v2"
    kalshi_url = f"{base_url}/events/{event_ticker}"
    
    print(f"Querying Kalshi API for event series: {event_ticker}...")
    try:
        response = requests.get(kalshi_url)
        response.raise_for_status()
        markets_data = response.json().get('event', {}).get('markets', [])
    except Exception as e:
        print(f"Error fetching Kalshi data: {e}")
        print("Falling back: Ensure your network is active or check ticker formatting.")
        return

    if not markets_data:
        print(f"No active markets found for ticker {event_ticker}.")
        return

    # 2. Pull live forecast data from NWS Grid Database
    nws_url = "https://api.weather.gov/gridpoints/MFL/98,78"
    headers = {'User-Agent': '(weather_arbitrage, contact: data_pipeline@example.com)'}
    
    print("Connecting to NWS Grid Database...")
    nws_res = requests.get(nws_url, headers=headers)
    nws_res.raise_for_status()
    
    tomorrow_iso = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    temp_values = nws_res.json()['properties']['temperature']['values']
    tomorrow_temps_f = []
    
    for entry in temp_values:
        if tomorrow_iso in entry['validTime']:
            temp_f = (entry['value'] * 9/5) + 32
            tomorrow_temps_f.append(temp_f)
            
    if not tomorrow_temps_f:
        print(f"Could not isolate hourly grid windows for {tomorrow_iso}.")
        return
        
    median_high = pd.Series(tomorrow_temps_f).max() 
    std_dev = 2.5  # Climatological model uncertainty spread
    
    # Mathematical Cumulative Distribution Function (CDF)
    def cdf(x):
        return 0.5 * (1.0 + math.erf((x - median_high) / (std_dev * math.sqrt(2.0))))

    analysis = []

    # 3. Dynamic Parsing Loop
    print("Dynamically structuring tranches and mapping normal curve...")
    for market in markets_data:
        title = market.get('title', '')      # e.g., "86° to 87°"
        yes_price = market.get('yes_price')  # In cents (0-100)
        
        if not title or yes_price is None:
            continue
            
        low_bound, high_bound = -100.0, 300.0
        
        if "or below" in title.lower():
            val = float(re.findall(r'\d+', title)[0])
            high_bound = val + 0.5
        elif "or above" in title.lower():
            val = float(re.findall(r'\d+', title)[0])
            low_bound = val - 0.5
        elif "to" in title.lower():
            vals = [float(x) for x in re.findall(r'\d+', title)]
            if len(vals) == 2:
                low_bound = vals[0] - 0.5
                high_bound = vals[1] + 0.5
        else:
            continue

        # Calculate live model probability
        prob = cdf(high_bound) - cdf(low_bound)
        model_prob_pct = prob * 100
        
        no_price = 100 - yes_price
        ev_yes = model_prob_pct - yes_price
        ev_no = (100 - model_prob_pct) - no_price
        
        if ev_yes > 4.0:
            action = "🟢 BUY YES"
        elif ev_no > 4.0:
            action = "🚨 BUY NO"
        else:
            action = "   --"

        analysis.append({
            "Tranche": title,
            "Live YES": f"{yes_price}¢",
            "Live NO": f"{no_price}¢",
            "Model Prob": f"{model_prob_pct:.1f}%",
            "EV (YES)": f"{ev_yes:+.1f}¢",
            "EV (NO)": f"{ev_no:+.1f}¢",
            "Action": action,
            "sort_key": low_bound
        })

    df = pd.DataFrame(analysis).sort_values("sort_key").drop(columns=["sort_key"])
    
    print("\n=========================================================================")
    print(f"            API-DRIVEN DYNAMIC WEATHER ARBITRAGE (KMIA)")
    print(f" Target Date: {tomorrow_iso}  |  NWS Expected High: {median_high:.1f}°F")
    print("=========================================================================")
    print(df.to_string(index=False))
    print("=========================================================================\n")

if __name__ == "__main__":
    get_dynamic_kalshi_arbitrage()