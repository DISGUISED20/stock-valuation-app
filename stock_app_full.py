import requests
from flask import Flask, render_template_string, request, jsonify
import yfinance as yf
from bs4 import BeautifulSoup
import re
import time
from datetime import datetime, timedelta

app = Flask(__name__)

# ---------- CACHE ----------
CACHE = {}

def cache_get(key):
    item = CACHE.get(key)
    if not item:
        return None
    value, expires = item
    if datetime.utcnow() > expires:
        CACHE.pop(key, None)
        return None
    return value

def cache_set(key, value, ttl=120):
    CACHE[key] = (value, datetime.utcnow() + timedelta(seconds=ttl))

def to_float(v):
    """Convert string/number to float safely."""
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").strip())
    except:
        return None


# ==============================
# 1️⃣  SCREENER FALLBACK (robust)
# ==============================
def fetch_screener(ticker):
    key = f"sc:{ticker}"
    cached = cache_get(key)
    if cached:
        return cached

    symbol = ticker.upper().replace(".NS", "")
    url = f"https://www.screener.in/company/{symbol}/"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120 Safari/537.36",
        "Accept-Language": "en-US,en;q=0.9"
    }

    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            out = {'eps': None, 'pe': None}
            cache_set(key, out, ttl=60)
            return out

        soup = BeautifulSoup(r.text, "lxml")

        eps_val = None
        pe_val = None

        # Scrape snapshot table
        table = soup.find("table", class_="snapshot")
        if table:
            for tr in table.find_all("tr"):
                tds = tr.find_all("td")
                if len(tds) >= 2:
                    k = tds[0].get_text(strip=True).upper()
                    v = tds[1].get_text(strip=True)

                    if "EPS" in k and eps_val is None:
                        eps_val = v
                    if ("P/E" in k) and pe_val is None:
                        pe_val = v

        # Secondary regex fallback
        if not pe_val:
            txt = soup.get_text(" ")
            m = re.search(r"P/?E\s*[:\-]?\s*([0-9]+\.?[0-9]*)", txt, re.I)
            if m:
                pe_val = m.group(1)

        out = {
            "eps": to_float(eps_val),
            "pe": to_float(pe_val)
        }

        cache_set(key, out, ttl=120)
        return out

    except:
        return {"eps": None, "pe": None}



# ===============================
# 2️⃣  NSE PRICE → yfinance fallback
# ===============================
def fetch_nse_price(ticker):
    """
    Try NSE API → If fails, yfinance fallback is used in fetch_price_with_fallback()
    """
    url = f"https://www.nseindia.com/api/quote-equity?symbol={ticker.replace('.NS','')}"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "en-US,en;q=0.9"
    }
    try:
        r = requests.get(url, headers=headers, timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()

        price = data.get("priceInfo", {}).get("lastPrice")
        open_price = data.get("priceInfo", {}).get("open")
        high = data.get("priceInfo", {}).get("intraDayHighLow", {}).get("max")
        low = data.get("priceInfo", {}).get("intraDayHighLow", {}).get("min")
        prev_close = data.get("priceInfo", {}).get("previousClose")

        return {
            "market_price": to_float(price),
            "open": to_float(open_price),
            "dayHigh": to_float(high),
            "dayLow": to_float(low),
            "previousClose": to_float(prev_close),
        }
    except:
        return None


def fetch_price_with_fallback(ticker):
    """Try NSE → if None, fallback to yfinance"""
    data = fetch_nse_price(ticker)

    if data and data.get("market_price"):
        return data

    # yfinance fallback
    try:
        yf_t = yf.Ticker(ticker)
        info = yf_t.info
        return {
            "market_price": to_float(info.get("regularMarketPrice") or info.get("previousClose")),
            "open": to_float(info.get("open")),
            "dayHigh": to_float(info.get("dayHigh")),
            "dayLow": to_float(info.get("dayLow")),
            "previousClose": to_float(info.get("previousClose"))
        }
    except:
        return {
            "market_price": None,
            "open": None,
            "dayHigh": None,
            "dayLow": None,
            "previousClose": None
        }



# ================================
# 3️⃣  Yfinance fundamental fetch
# ================================
def fetch_yf_fundamentals(ticker):
    t = yf.Ticker(ticker)
    info = t.info

    eps = to_float(info.get("earningsPerShare"))
    pe = to_float(info.get("trailingPE"))
    forward_eps = to_float(info.get("forwardEps"))
    industry_pe = to_float(info.get("industryPE"))

    return {
        "eps": eps,
        "pe": pe,
        "forward_eps": forward_eps,
        "industry_pe": industry_pe
    }



# ===============================
# 4️⃣  LOAD NSE TICKER LIST
# ===============================
def load_ticker_list():
    try:
        with open("nse_list.txt", "r") as f:
            return [x.strip() for x in f.readlines() if x.strip()]
    except:
        return []

TICKERS = load_ticker_list()



# ===============================
# 5️⃣  AUTOCOMPLETE API
# ===============================
@app.route("/search_api")
def search_api():
    q = request.args.get("query", "").upper().strip()
    if not q:
        return jsonify([])

    matches = [t for t in TICKERS if t.startswith(q)]
    return jsonify(matches[:15])



# ===============================
# 6️⃣  MAIN SEARCH / VALUATION ROUTE
# ===============================
@app.route("/", methods=["GET"])
@app.route("/query", methods=["GET"])
def query():
    ticker = request.args.get("ticker", "").upper().strip()
    if ticker == "":
        return render_template_string(HTML, result=None)

    # PRICE (NSE → yfinance fallback)
    price_data = fetch_price_with_fallback(ticker)
    price = price_data["market_price"]

    # FUNDAMENTALS (yfinance → screener fallback)
    yf_data = fetch_yf_fundamentals(ticker)

    eps = yf_data["eps"]
    pe = yf_data["pe"]

    # If missing → screener.in fallback
    if eps is None or pe is None:
        sc = fetch_screener(ticker)
        if eps is None:
            eps = sc["eps"]
        if pe is None:
            pe = sc["pe"]

    # Intrinsic value calculation
    fair_pe = 18
    intrinsic_value = None
    if eps:
        intrinsic_value = eps * fair_pe

    buy_price = intrinsic_value * 0.7 if intrinsic_value else None
    sell_price = intrinsic_value * 1.3 if intrinsic_value else None

    decision = "UNKNOWN"
    if price and buy_price:
        if price < buy_price:
            decision = "BUY"
        elif price > sell_price:
            decision = "SELL"
        else:
            decision = "HOLD"

    result = {
        "price_data": price_data,
        "eps": eps,
        "pe": pe,
        "fair_pe": fair_pe,
        "intrinsic_value": intrinsic_value,
        "buy_price": buy_price,
        "sell_price": sell_price,
        "decision": decision
    }

    return render_template_string(HTML, ticker=ticker, result=result)



# ===============================
# 7️⃣  HTML FRONTEND (clean + centered + autocomplete)
# ===============================
HTML = """
<!DOCTYPE html>
<html>
<head>
<title>Stock Valuation Tool (NSE)</title>
<style>
body { background:#0e0e0e; color:white; font-family: Arial; text-align:center; }
input { width:300px; padding:10px; border-radius:6px; border:1px solid #666; background:#222; color:white; }
button { padding:10px 20px; background:green; border:none; color:white; border-radius:6px; }
.tablebox { width:800px; margin:20px auto; background:#111; padding:20px; border-radius:10px; }
table { width:100%; border-collapse:collapse; margin-top:15px; }
td { border:1px solid #333; padding:10px; }
.suggest-box { background:#222; width:300px; margin:auto; text-align:left; border:1px solid #333; display:none; position:absolute; left:50%; transform:translateX(-50%); z-index:10; }
.suggest-item { padding:8px; cursor:pointer; }
.suggest-item:hover { background:#444; }
</style>

<script>
function autocomplete(){
    let q=document.getElementById("ticker").value;
    if(q.length===0){
        document.getElementById("sug").style.display="none";
        return;
    }
    fetch("/search_api?query="+q)
    .then(r=>r.json())
    .then(data=>{
        let box=document.getElementById("sug");
        box.innerHTML="";
        if(data.length===0){ box.style.display="none"; return; }
        box.style.display="block";
        data.forEach(t=>{
            let d=document.createElement("div");
            d.className="suggest-item";
            d.innerText=t;
            d.onclick=function(){
                document.getElementById("ticker").value=t;
                box.style.display="none";
            };
            box.appendChild(d);
        });
    });
}
</script>

</head>
<body>

<h1>Stock Valuation Tool (NSE India)</h1>

<form>
    <input id="ticker" name="ticker" placeholder="TCS.NS" onkeyup="autocomplete()">
    <button>Search</button>
</form>

<div id="sug" class="suggest-box"></div>

{% if result %}
<div class="tablebox">
<h2>Valuation</h2>

<table>
<tr><td>Market Price</td><td>{{ result.price_data.market_price }}</td></tr>
<tr><td>EPS (TTM)</td><td>{{ result.eps }}</td></tr>
<tr><td>Trailing PE</td><td>{{ result.pe }}</td></tr>
<tr><td>Fair PE</td><td>{{ result.fair_pe }}</td></tr>
<tr><td>Intrinsic Value</td><td>{{ result.intrinsic_value }}</td></tr>
<tr><td>Buy Price</td><td>{{ result.buy_price }}</td></tr>
<tr><td>Sell Price</td><td>{{ result.sell_price }}</td></tr>
<tr><td>Decision</td><td><b>{{ result.decision }}</b></td></tr>
</table>
</div>
{% endif %}

</body>
</html>
"""


# Run the app (for local development)
if __name__ == "__main__":
    app.run(debug=True)
