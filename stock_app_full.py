from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import yfinance as yf
import time
from datetime import datetime, timedelta
import re
import os

app = Flask(__name__)
CORS(app)


# -------------------- cache --------------------
CACHE = {}
CACHE_TTL = 60  # seconds

def cache_get(key):
    row = CACHE.get(key)
    if not row:
        return None
    val, expires = row
    if datetime.utcnow() > expires:
        del CACHE[key]
        return None
    return val

def cache_set(key, value, ttl=CACHE_TTL):
    CACHE[key] = (value, datetime.utcnow() + timedelta(seconds=ttl))

# -------------------- helpers --------------------
def to_float(x):
    if x is None:
        return None
    try:
        s = str(x).replace(',', '').replace('₹', '').replace('Rs.', '').strip()
        token = re.split(r'\s+', s)[0]
        return float(token)
    except Exception:
        return None

# -------------------- load nse list --------------------
def load_nse_list(fname='nse_list.txt'):
    if not os.path.isfile(fname):
        return []
    with open(fname, 'r', encoding='utf-8') as f:
        lines = [ln.strip().upper() for ln in f if ln.strip()]
    return lines

NSE_TICKERS = load_nse_list()

# -------------------- autocomplete endpoint --------------------
@app.route('/search_api')
def search_api():
    q = request.args.get('query', '').upper().strip()
    if not q:
        return jsonify([])
    matches = [s for s in NSE_TICKERS if s.startswith(q)]
    return jsonify(matches[:25])

# -------------------- NSE price fetch --------------------
def fetch_nse_price(ticker):
    key = f'nse_price:{ticker}'
    cached = cache_get(key)
    if cached:
        return cached

    symbol = ticker.upper().replace('.NS', '')
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0',
        'Accept': 'application/json',
        'Referer': f'https://www.nseindia.com/get-quotes/equity?symbol={symbol}'
    }

    try:
        session.get("https://www.nseindia.com", headers=headers, timeout=6)
        time.sleep(0.25)

        url = f"https://www.nseindia.com/api/quote-equity?symbol={symbol}"
        r = session.get(url, headers=headers, timeout=8)

        if r.status_code != 200:
            out = {"error": f"NSE status {r.status_code}"}
            cache_set(key, out, ttl=10)
            return out

        data = r.json().get("priceInfo", {})
        out = {
            "market_price": data.get("lastPrice"),
            "open": data.get("open"),
            "dayHigh": data.get("intraDayHighLow", {}).get("max"),
            "dayLow": data.get("intraDayHighLow", {}).get("min"),
            "previousClose": data.get("previousClose"),
        }

        cache_set(key, out)
        return out
    except Exception as e:
        return {"error": str(e)}

# -------------------- yfinance fundamentals --------------------
def fetch_yf(ticker):
    key = f"yf:{ticker}"
    cached = cache_get(key)
    if cached:
        return cached

    try:
        t = yf.Ticker(ticker)
        info = t.info or {}

        out = {
            "eps": info.get("trailingEps"),
            "pe": info.get("trailingPE"),
            "forward_eps": info.get("forwardEps"),
            "industry_pe": info.get("industryPE"),
        }

        cache_set(key, out)
        return out
    except:
        out = {"eps": None, "pe": None, "forward_eps": None, "industry_pe": None}
        cache_set(key, out)
        return out

# -------------------- Screener fallback --------------------
def fetch_screener(ticker):
    symbol = ticker.upper().replace('.NS', '')
    url = f"https://www.screener.in/company/{symbol}/"

    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=6)
        if r.status_code != 200:
            return {"eps": None, "pe": None}

        soup = BeautifulSoup(r.text, "lxml")

        snap = soup.find("table", class_="snapshot")

        eps_val = pe_val = None

        if snap:
            for tr in snap.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    k = tds[0].get_text(strip=True).upper()
                    v = tds[1].get_text(strip=True)
                    if "EPS" in k and not eps_val:
                        eps_val = v
                    if "P/E" in k and not pe_val:
                        pe_val = v

        return {
            "eps": to_float(eps_val),
            "pe": to_float(pe_val),
        }

    except:
        return {"eps": None, "pe": None}

# -------------------- valuation engine --------------------
def calculate_valuation(ticker):
    nse = fetch_nse_price(ticker)
    yf_data = fetch_yf(ticker)
    sc_data = fetch_screener(ticker)

    eps = yf_data["eps"] or sc_data["eps"]
    pe = yf_data["pe"] or sc_data["pe"]
    industry_pe = yf_data["industry_pe"]

    market_price = to_float(nse.get("market_price"))

    expected_growth = 0.10
    fair_pe = 18  # simplified

    if eps:
        forward_eps = eps * (1 + expected_growth)
        intrinsic_value = forward_eps * fair_pe
        mos = 0.30
        buy_price = intrinsic_value * (1 - mos)
        sell_price = intrinsic_value * (1 + mos)

        if market_price:
            if market_price <= buy_price:
                decision = "BUY"
            elif market_price >= sell_price:
                decision = "SELL"
            else:
                decision = "HOLD"
        else:
            decision = "UNKNOWN"
    else:
        forward_eps = intrinsic_value = buy_price = sell_price = None
        decision = "UNKNOWN"

    return {
        "ticker": ticker,
        "market_price": market_price,
        "eps": eps,
        "pe": pe,
        "fair_pe": fair_pe,
        "forward_eps": forward_eps,
        "intrinsic_value": intrinsic_value,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "decision": decision,
        "nse_error": nse.get("error")
    }

# -------------------- API OUTPUT --------------------
@app.route("/api/valuation")
def api_valuation():
    ticker = request.args.get("ticker", "").upper().strip()
    if not ticker:
        return jsonify({"error": "No ticker provided"}), 400

    return jsonify(calculate_valuation(ticker))


@app.route("/")
def root():
    return jsonify({
        "message": "Stock Valuation API is running.",
        "endpoints": {
            "/api/valuation?ticker=RELIANCE.NS": "Valuation JSON",
            "/search_api?query=REL": "Autocomplete"
        }
    })

# --------------------
if __name__ == "__main__":
    app.run(debug=True)
