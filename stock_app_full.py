"""
stock_app_full.py

Final combined single-file app (Option A + Screener fallback).
- Autocomplete reads nse_list.txt (one ticker per line, e.g. RELIANCE.NS)
- Primary fundamentals source: yfinance
- Fallback fundamentals source: Screener.in scraping (used only if yfinance fields missing)
- Prices from NSE API (best-effort; NSE may block sometimes)
- Search bar always at top; no "Search Again" button
- Simple in-memory cache
- Dark centered layout (750px card)
- Key fundamentals table removed per request

Run:
1) python -m venv venv
2) venv\Scripts\activate
3) pip install -r requirements.txt
4) python stock_app_full.py
5) Open http://127.0.0.1:5000
"""

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

# -------------------- NSE price fetch (best-effort) --------------------
def fetch_nse_price(ticker):
    key = f'nse_price:{ticker}'
    cached = cache_get(key)
    if cached:
        return cached

    symbol = ticker.upper().replace('.NS', '')
    session = requests.Session()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)',
        'Accept': 'application/json, text/plain, */*',
        'Referer': f'https://www.nseindia.com/get-quotes/equity?symbol={symbol}'
    }

    try:
        # prime cookies
        session.get('https://www.nseindia.com', headers=headers, timeout=6)
        time.sleep(0.25)
        url = f'https://www.nseindia.com/api/quote-equity?symbol={symbol}'
        r = session.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            time.sleep(0.3)
            r = session.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            out = {'error': f'NSE status {r.status_code}'}
            cache_set(key, out, ttl=15)
            return out
        data = r.json()
        p = data.get('priceInfo', {}) or {}
        ihl = p.get('intraDayHighLow', {}) or {}
        out = {
            'market_price': p.get('lastPrice'),
            'open': p.get('open'),
            'dayHigh': ihl.get('max'),
            'dayLow': ihl.get('min'),
            'previousClose': p.get('previousClose'),
            'raw': data
        }
        cache_set(key, out, ttl=20)
        return out
    except Exception as e:
        return {'error': str(e)}

# -------------------- yfinance fundamentals (primary) --------------------
def fetch_yf(ticker):
    key = f'yf:{ticker}'
    cached = cache_get(key)
    if cached:
        return cached
    try:
        t = yf.Ticker(ticker)
        info = t.info or {}
        out = {
            'eps': info.get('trailingEps'),
            'pe': info.get('trailingPE'),
            'forward_eps': info.get('forwardEps'),
            'industry_pe': info.get('industryPE'),
            'raw_present': bool(info)
        }
        cache_set(key, out, ttl=60)
        return out
    except Exception:
        out = {'eps': None, 'pe': None, 'forward_eps': None, 'industry_pe': None, 'raw_present': False}
        cache_set(key, out, ttl=30)
        return out

# -------------------- Screener.in fallback (if yfinance missing) --------------------
def fetch_screener(ticker):
    key = f'sc:{ticker}'
    cached = cache_get(key)
    if cached:
        return cached

    symbol = ticker.upper().replace('.NS', '')
    url = f'https://www.screener.in/company/{symbol}/'
    headers = {'User-Agent': 'Mozilla/5.0', 'Accept-Language': 'en-US'}

    try:
        r = requests.get(url, headers=headers, timeout=8)
        if r.status_code != 200:
            cache_set(key, {'eps': None, 'pe': None, 'industry_pe': None}, ttl=60)
            return {'eps': None, 'pe': None, 'industry_pe': None}
        soup = BeautifulSoup(r.text, 'lxml')

        eps_val = None
        pe_val = None
        # snapshot table parsing
        snap = soup.find('table', {'class': 'snapshot'})
        if snap:
            for tr in snap.find_all('tr'):
                tds = tr.find_all('td')
                if len(tds) >= 2:
                    k = tds[0].get_text(strip=True).upper()
                    v = tds[1].get_text(strip=True)
                    if 'EPS' in k and eps_val is None:
                        eps_val = v
                    if 'P/E' in k and pe_val is None:
                        pe_val = v

        # fallback search in page text for PE if missing
        if not pe_val:
            txt = soup.get_text(' ')
            m = re.search(r'P/?E\\s*[:\\-]?\\s*([0-9]+\\.?[0-9]*)', txt, re.I)
            if m:
                pe_val = m.group(1)

        out = {'eps': to_float(eps_val), 'pe': to_float(pe_val), 'industry_pe': None}
        cache_set(key, out, ttl=120)
        return out
    except Exception:
        return {'eps': None, 'pe': None, 'industry_pe': None}

# -------------------- valuation helper --------------------
def determine_fair_pe(industry_pe, hist_pe, growth):
    if growth is None:
        growth = 0.08
    try:
        hist_val = hist_pe if hist_pe else 0
    except:
        hist_val = 0
    if growth < 0.05:
        san = 2
    elif growth < 0.15:
        san = 3
    else:
        san = 4
    base = hist_val + san
    chosen = min(industry_pe if industry_pe else base, base)
    if growth < 0.05:
        cap = 12
    elif growth < 0.15:
        cap = 18
    else:
        cap = 25
    fair = min(chosen, cap)
    try:
        return round(float(fair), 2)
    except:
        return cap

# -------------------- HTML templates (small functions) --------------------
def top_search_html(ticker_value=''):
    # HTML for the top search bar + suggestion box
    return f'''
    <div class="top">
      <h1>Stock Valuation Tool (NSE India)</h1>
      <div class="searchbar">
          <input id="ticker" type="text" placeholder="RELIANCE.NS" value="{ticker_value}" onkeyup="doSuggest()" autocomplete="off">
          <button onclick="goSearch()">Search</button>
          <div id="suggestions"></div>
      </div>
    </div>
    <script>
    async function doSuggest() {{
        const q = document.getElementById('ticker').value.trim();
        const box = document.getElementById('suggestions');
        box.innerHTML = '';
        if (!q) {{ box.style.display='none'; return; }}
        try {{
            const res = await fetch('/search_api?query=' + encodeURIComponent(q));
            const data = await res.json();
            if (!data || data.length == 0) {{ box.style.display='none'; return; }}
            data.forEach(item => {{
                const d = document.createElement('div');
                d.textContent = item;
                d.onclick = () => {{ document.getElementById('ticker').value = item; box.style.display='none'; }};
                d.onmouseover = () => d.style.background = '#222';
                d.onmouseout = () => d.style.background = '#111';
                box.appendChild(d);
            }});
            box.style.display = 'block';
        }} catch(err) {{ console.error(err); box.style.display='none'; }}
    }}
    function goSearch() {{
        const t = document.getElementById('ticker').value.trim();
        if (!t) return alert('Enter ticker (e.g. RELIANCE.NS)');
        window.location = '/query?ticker=' + encodeURIComponent(t);
    }}
    document.addEventListener('click', function(e) {{
        const box = document.getElementById('suggestions');
        const inp = document.getElementById('ticker');
        if (!box.contains(e.target) && e.target !== inp) box.style.display = 'none';
    }});
    </script>
    '''

# -------------------- routes --------------------
@app.route('/')
def home():
    html = '''
    <html>
    <head>
      <meta charset="utf-8">
      <title>Stock Valuator</title>
      <style>
        body { background:black; color:white; font-family:Arial, sans-serif; }
        .top { width:100%; text-align:center; padding-top:18px; }
        .searchbar { margin:auto; width:750px; position:relative; }
        input[type=text] { padding:8px; width:420px; border-radius:4px; border:none; background:#222; color:#fff; }
        button { padding:8px 12px; border-radius:4px; border:none; background:#2e8b57; color:white; cursor:pointer; margin-left:8px; }
        .card { width:750px; margin:20px auto; background:#111; padding:20px; border-radius:10px; }
        #suggestions { position:absolute; background:#111; border:1px solid #333; width:420px; max-height:240px; overflow-y:auto; display:none; left:50%; transform:translateX(-50%); margin-top:6px; z-index:1000; }
        #suggestions div { padding:8px; cursor:pointer; border-bottom:1px solid #222; }
        #suggestions div:hover { background:#222; }
        table { width:100%; border-collapse:collapse; }
        th, td { padding:8px; border:1px solid #333; text-align:left; }
        h2 { margin-top:0; text-align:center; }
      </style>
    </head>
    <body>
    ''' + top_search_html('') + '''
    <div class="card">
      <p style="color:#bbb; text-align:center;">Type a ticker above and press Search — results and valuation will appear here.</p>
    </div>
    </body>
    </html>
    '''
    return html

@app.route('/query')
def query():
    ticker = request.args.get('ticker', '').upper().strip()
    if not ticker:
        return 'No ticker provided. Go back and enter a ticker like RELIANCE.NS.'

    # Fetch price from NSE
    nse = fetch_nse_price(ticker)
    market_price = to_float(nse.get('market_price')) if isinstance(nse, dict) else None
    nse_err = nse.get('error') if isinstance(nse, dict) and 'error' in nse else None

    # Primary: yfinance
    yf = fetch_yf(ticker)
    eps_yf = yf.get('eps')
    pe_yf = yf.get('pe')
    ind_pe_yf = yf.get('industry_pe')

    # Fallback: screener
    sc = fetch_screener(ticker)
    eps_sc = sc.get('eps') if sc else None
    pe_sc = sc.get('pe') if sc else None
    ind_pe_sc = sc.get('industry_pe') if sc else None

    # Choose values - prefer yfinance, else screener
    eps = eps_yf if eps_yf is not None else eps_sc
    pe = pe_yf if pe_yf is not None else pe_sc
    industry_pe = ind_pe_yf if ind_pe_yf is not None else ind_pe_sc

    # Valuation
    expected_growth = 0.10
    fair_pe = determine_fair_pe(industry_pe, pe, expected_growth)

    if eps is not None:
        forward_eps = eps * (1 + expected_growth)
        intrinsic_value = forward_eps * fair_pe
        mos = 0.30
        buy_price = intrinsic_value * (1 - mos)
        sell_price = intrinsic_value * (1 + mos)
        if market_price is not None:
            if market_price <= buy_price:
                decision = 'BUY'
            elif market_price >= sell_price:
                decision = 'SELL'
            else:
                decision = 'HOLD'
        else:
            decision = 'UNKNOWN'
    else:
        forward_eps = intrinsic_value = buy_price = sell_price = None
        decision = 'UNKNOWN'

    # Build HTML result (searchbar remains at top)
    page = f'''
    <html>
    <head>
      <meta charset="utf-8">
      <title>Valuation: {ticker}</title>
      <style>
        body {{ background:black; color:white; font-family:Arial, sans-serif; }}
        .top {{ width:100%; text-align:center; padding-top:18px; }}
        .searchbar {{ margin:auto; width:750px; position:relative; }}
        input[type=text] {{ padding:8px; width:420px; border-radius:4px; border:none; background:#222; color:#fff; }}
        button {{ padding:8px 12px; border-radius:4px; border:none; background:#2e8b57; color:white; cursor:pointer; margin-left:8px; }}
        .card {{ width:750px; margin:20px auto; background:#111; padding:20px; border-radius:10px; }}
        #suggestions {{ position:absolute; background:#111; border:1px solid #333; width:420px; max-height:240px; overflow-y:auto; display:none; left:50%; transform:translateX(-50%); margin-top:6px; z-index:1000; }}
        #suggestions div {{ padding:8px; cursor:pointer; border-bottom:1px solid #222; }}
        #suggestions div:hover {{ background:#222; }}
        table {{ width:100%; border-collapse:collapse; }}
        th, td {{ padding:8px; border:1px solid #333; text-align:left; }}
        h2 {{ text-align:center; }}
        .note {{ color:#bbb; text-align:center; margin-top:10px; }}
      </style>
    </head>
    <body>
    ''' + top_search_html('') + f'''
    <div class="card">
      <h2>Valuation</h2>
      <table>
        <tr><th>Metric</th><th>Value</th></tr>
        <tr><td>Market Price</td><td>{market_price}</td></tr>
        <tr><td>EPS (TTM)</td><td>{eps}</td></tr>
        <tr><td>Trailing PE</td><td>{pe}</td></tr>
        <tr><td>Fair PE</td><td>{fair_pe}</td></tr>
        <tr><td>Forward EPS</td><td>{forward_eps}</td></tr>
        <tr><td>Intrinsic Value (IV)</td><td>{intrinsic_value}</td></tr>
        <tr><td>Buy Price</td><td>{buy_price}</td></tr>
        <tr><td>Sell Price</td><td>{sell_price}</td></tr>
        <tr><td>Decision</td><td><b>{decision}</b></td></tr>
      </table>
      <p class="note">Data sources: NSE (prices) + yfinance (primary). Screener.in used as fallback when yfinance misses data. NSE error: {nse_err}</p>
    </div>
    </body>
    </html>
    '''
    return page

if __name__ == '__main__':
    app.run(debug=True)
