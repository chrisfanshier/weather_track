import requests
import pandas as pd
import math
import re
from datetime import datetime, timedelta
from scipy.stats import norm

def get_dynamic_kalshi_arbitrage():
    # 1. Fetch Tomorrow's Active Markets dynamically by series
    series_ticker = "KXHIGHMIA"
    url = "https://external-api.kalshi.com/trade-api/v2/markets"
    params = {"series_ticker": series_ticker, "status": "open"}
    
    print(f"Querying Kalshi for series: {series_ticker}...")
    try:
        response = requests.get(url, params=params)
        response.raise_for_status()
        # Filter for tomorrow's date in the titles
        tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%b %d, %Y")
        markets = [m for m in response.json().get('markets', []) if tomorrow_date in m.get('title', '')]
    except Exception as e:
        print(f"API Error: {e}")
        return

    if not markets:
        print(f"No active markets found for {tomorrow_date}.")
        return

    # 2. Pull live forecast data from NWS Grid
    nws_url = "https://api.weather.gov/gridpoints/MFL/98,78"
    headers = {'User-Agent': '(weather_arbitrage, contact: data_pipeline@example.com)'}
    
    nws_res = requests.get(nws_url, headers=headers).json()
    tomorrow_iso = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    temp_values = nws_res['properties']['temperature']['values']
    tomorrow_temps = [(t['value'] * 9/5) + 32 for t in temp_values if tomorrow_iso in t['validTime']]
            
    if not tomorrow_temps:
        print("Could not isolate hourly grid windows.")
        return
        
    median_high = pd.Series(tomorrow_temps).max() 
    std_dev = 2.5 

    # 3. Dynamic Parsing and Calculation
    analysis = []
    for m in markets:
        title = m.get('title', '')
        yes_price = m.get('yes_price', 0)
        
        # Regex extracts all numbers to identify bounds
        nums = [float(x) for x in re.findall(r'\d+\.?\d*', title)]
        low, high = -100.0, 300.0
        
        if "<" in title: high = nums[0]
        elif ">" in title: low = nums[0]
        elif len(nums) >= 2: low, high = nums[0], nums[1]
        
        # Model Probability
        prob = (norm.cdf(high, median_high, std_dev) - norm.cdf(low, median_high, std_dev)) * 100
        ev_yes = prob - yes_price
        
        analysis.append({
            "Tranche": title,
            "Price": f"{yes_price}¢",
            "Model Prob": f"{prob:.1f}%",
            "EV (YES)": f"{ev_yes:+.1f}¢",
            "sort_key": low
        })

    df = pd.DataFrame(analysis).sort_values("sort_key").drop(columns=["sort_key"])
    
    print(f"\nTarget Date: {tomorrow_iso} | NWS Peak High: {median_high:.1f}°F")
    print("=========================================================================")
    print(df.to_string(index=False))

if __name__ == "__main__":
    get_dynamic_kalshi_arbitrage()