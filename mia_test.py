import requests
import pandas as pd
import math
from datetime import datetime, timedelta

def get_miami_tomorrow_probabilities():
    # KMIA Grid Location (Miami Office, Grid X: 98, Grid Y: 78)
    # Using the raw grid endpoint allows us to access the specific weather element arrays
    url = "https://api.weather.gov/gridpoints/MFL/98,78"
    
    headers = {
        'User-Agent': '(weather_track_probability, contact@example.com)'
    }
    
    print("Connecting to NWS Grid Database...")
    response = requests.get(url, headers=headers)
    response.raise_for_status()
    data = response.json()
    
    # Calculate the target date string for tomorrow
    tomorrow_str = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Extract the foundational hourly temperature grid array
    # This array holds all the forecasted hourly fluctuations
    temp_values = data['properties']['temperature']['values']
    
    tomorrow_temps_f = []
    
    for entry in temp_values:
        # Check if the timestamp belongs to tomorrow
        if tomorrow_str in entry['validTime']:
            # NWS stores values in Celsius; convert to Fahrenheit
            temp_c = entry['value']
            temp_f = (temp_c * 9/5) + 32
            tomorrow_temps_f.append(temp_f)
            
    if not tomorrow_temps_f:
        print(f"Could not isolate hourly grid windows for {tomorrow_str}.")
        return
        
    # Convert tomorrow's hourly curve into a Pandas Series to extract the distribution
    temp_series = pd.Series(tomorrow_temps_f)
    
    # Calculate standard distribution bounds used by NOAA for confidence intervals
    median_high = temp_series.max() # The expected deterministic peak
    
    # In lieu of parsing a 5GB GRIB ensemble file, we approximate the local daily variance bounds:
    std_dev = 2.5 # Miami's standard climatological summer/spring variance margin in Fahrenheit

    # -------------------------------------------------------------------------
    # DYNAMIC TRANCHE GENERATION AND PROBABILITY CALCULATOR
    # -------------------------------------------------------------------------
    # Align the baseline to the nearest whole integer to keep betting brackets clean
    baseline_high = math.floor(median_high)
    
    # Define a normal Cumulative Distribution Function (CDF)
    def cdf(x):
        return 0.5 * (1.0 + math.erf((x - median_high) / (std_dev * math.sqrt(2.0))))

    tranche_results = []
    
    # Generate sequential 2-degree brackets from -10°F to +10°F around the baseline high
    # Range is -10 to +10 step 2, which gives exactly 10 tranches
    for offset in range(-10, 10, 2):
        lower_bound = baseline_high + offset
        upper_bound = lower_bound + 2
        
        # Calculate the area under the normal curve for this specific 2-degree window
        prob = cdf(upper_bound) - cdf(lower_bound)
        
        tranche_results.append({
            "Betting Tranche": f"{lower_bound}° to {upper_bound}°F",
            "True Model Probability": f"{prob * 100:.2f}%",
            "Fair Value (Cents)": f"{prob * 100:.1f}¢"
        })
        
    df_tranches = pd.DataFrame(tranche_results)

    print("\n=======================================================")
    print(f"       KMIA PROBABILITY DISTRIBUTION FOR TOMORROW")
    print("=======================================================")
    print(f"Target Date             : {tomorrow_str}")
    print(f"Most Likely High (50%): {median_high:.1f}°F (Using {baseline_high}°F baseline)")
    print(f"Assumed Model Spread SD : {std_dev}°F")
    print("=======================================================")
    print(df_tranches.to_string(index=False))
    print("=======================================================")

if __name__ == "__main__":
    get_miami_tomorrow_probabilities()