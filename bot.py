import os
import time
import json
import requests
import threading
import io
import base64
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# Force immediate printing for Render logs
def log(msg):
    print(msg, flush=True)
    sys.stdout.flush()

PAIRS = ["EUR/USD", "USD/JPY", "GBP/USD", "AUD/USD", "USD/CAD", "XAU/USD"]
WAT_OFFSET = 1
SCAN_INTERVAL = 15 * 60
API_DELAY = 8

active_trades = [] 
daily_pair_signals = {} 
last_reset_date = None

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"AI Sniper Vision is alive")

def run_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

def send_telegram(text):
    if not BOT_TOKEN or not CHAT_ID:
        log(f"❌ MISSING VARIABLES: BOT_TOKEN={bool(BOT_TOKEN)}, CHAT_ID={bool(CHAT_ID)}")
        return False
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        res = r.json()
        if not res.get("ok"):
            log(f"❌ TELEGRAM REJECTED: {res}")
        return res.get("ok", False)
    except Exception as e:
        log(f"❌ TELEGRAM CRASH: {e}")
        return False

def send_telegram_photo(image_bytes, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        files = {"photo": ("chart.png", image_bytes, "image/png")}
        data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        r = requests.post(url, files=files, data=data, timeout=30)
        res = r.json()
        if not res.get("ok"):
            log(f"❌ TELEGRAM PHOTO REJECTED: {res}")
        return res.get("ok", False)
    except Exception as e:
        log(f"❌ TELEGRAM PHOTO CRASH: {e}")
        return False

def get_candles(symbol, interval, outputsize=100):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVEDATA_KEY}
    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
        if "values" not in data: return []
        return list(reversed(data["values"]))
    except: return []

def get_current_price(symbol):
    c = get_candles(symbol, "1min", 2)
    if c: return float(c[-1]["close"])
    return None

def calc_rsi(closes, p=14):
    if len(closes) < p+1: return None
    g, l = [], []
    for i in range(1, len(closes)):
        d = closes[i]-closes[i-1]
        g.append(max(d,0)); l.append(max(-d,0))
    ag, al = sum(g[:p])/p, sum(l[:p])/p
    for i in range(p, len(g)):
        ag = (ag*(p-1)+g[i])/p; al = (al*(p-1)+l[i])/p
    if al == 0: return 100.0
    return 100 - 100/(1+ag/al)

def calc_atr(candles, p=14):
    if len(candles) < p+1: return 0
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"]); lo = float(candles[i]["low"])
        pc = float(candles[i-1]["close"])
        trs.append(max(h-lo, abs(h-pc), abs(lo-pc)))
    atr = sum(trs[:p])/p
    for i in range(p, len(trs)): atr = (atr*(p-1)+trs[i])/p
    return atr

def generate_chart(candles):
    if not candles: return None
    last_50 = candles[-50:]
    width, height = 800, 500
    img = Image.new('RGB', (width, height), color='#1a1a1a')
    draw = ImageDraw.Draw(img)
    
    highs = [float(c["high"]) for c in last_50]
    lows = [float(c["low"]) for c in last_50]
    max_p, min_p = max(highs), min(lows)
    price_range = max_p - min_p if max_p != min_p else 1
    
    margin = 50
    chart_h = height - (margin * 2)
    chart_w = width - (margin * 2)
    candle_w = chart_w / len(last_50)
    
    def get_y(price): return height - margin - ((price - min_p) / price_range * chart_h)
    
    for i, c in enumerate(last_50):
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        x = margin + (i * candle_w)
        color = '#26a69a' if cl >= o else '#ef5350'
        draw.line([(x, get_y(h)), (x, get_y(l))], fill=color, width=2)
        body_top = get_y(max(o, cl))
        body_bot = get_y(min(o, cl))
        draw.rectangle([(x - candle_w/3, body_top), (x + candle_w/3, body_bot)], fill=color)
        
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    buf.seek(0)
    return buf.read()

def get_asian_high_low(candles_15m):
    asian_highs, asian_lows = [], []
    for c in candles_15m:
        try:
            dt = datetime.fromisoformat(c["datetime"]).replace(tzinfo=timezone.utc)
            if 0 <= dt.hour < 7:
                asian_highs.append(float(c["high"]))
                asian_lows.append(float(c["low"]))
        except: continue
    if not asian_highs or not asian_lows: return None, None
    return max(asian_highs), min(asian_lows)

def check_sweep(candles_15m, asian_high, asian_low):
    if not asian_high or not asian_low: return None
    for i in range(-4, 0):
        c = candles_15m[i]
        h, l, cl, o = float(c["high"]), float(c["low"]), float(c["close"]), float(c["open"])
        if h > asian_high and cl < asian_high and cl < o: return "BEARISH_SWEEP"
        if l < asian_low and cl > asian_low and cl > o: return "BULLISH_SWEEP"
    return None

def ask_ai_vision(pair, context, sweep_type, image_bytes):
    prompt = f"""
    You are a Senior Institutional Trader. We detected a Liquidity Sweep on {pair}.
    Sweep Type: {sweep_type}
    Current Data: {context}
    
    Look at the attached 15m chart. Do you see a clear rejection wick?
    If the chart is messy, output "NO TRADE".
    If high probability, output ONLY JSON: "signal" (BUY/SELL), "entry", "sl", "tp", "reasoning".
    """
    b64_image = base64.b64encode(image_bytes).decode('utf-8')
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    headers = {'Content-Type': 'application/json'}
    payload = {
        "contents": [{
            "parts": [
                {"text": prompt},
                {"inline_data": {"mime_type": "image/png", "data": b64_image}}
            ]
        }]
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        res = r.json()
        text_response = res['candidates'][0]['content']['parts'][0]['text'].strip()
        return text_response
    except Exception as e:
        log(f"AI Error: {e}")
        return "NO TRADE"

def main_loop():
    global last_reset_date, daily_pair_signals, active_trades
    
    # Print variables to check if they loaded
    log(f"=== BOT STARTED ===")
    log(f"BOT_TOKEN: {'YES' if BOT_TOKEN else 'MISSING'}")
    log(f"CHAT_ID: {'YES' if CHAT_ID else 'MISSING'}")
    log(f"GEMINI_KEY: {'YES' if GEMINI_API_KEY else 'MISSING'}")
    
    send_telegram("👁️ <b>AI VISION SNIPER v13.1 ONLINE</b>\n\nDirect API mode active.")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            wat = (now.hour + WAT_OFFSET) % 24
            if last_reset_date != now.date():
                last_reset_date = now.date()
                daily_pair_signals = {}

            log(f"[{wat:02d}:{now.minute:02d} WAT] Scanning with Vision...")

            for pair in PAIRS:
                if daily_pair_signals.get(pair, 0) >= 1: continue

                c15 = get_candles(pair, "15min", 100); time.sleep(API_DELAY)
                c1h = get_candles(pair, "1h", 100); time.sleep(API_DELAY)
                
                if not c15 or not c1h: continue

                closes_15 = [float(c["close"]) for c in c15]
                closes_1h = [float(c["close"]) for c in c1h]
                atr_15 = calc_atr(c15, 14)
                rsi_1h = calc_rsi(closes_1h, 14)
                price = closes_15[-1]

                asian_high, asian_low = get_asian_high_low(c15)
                sweep = check_sweep(c15, asian_high, asian_low)
                
                if sweep:
                    context = f"Price: {price} | 1h RSI: {rsi_1h:.1f} | ATR: {atr_15:.5f} | Asian High: {asian_high:.5f} | Asian Low: {asian_low:.5f}"
                    
                    log(f"  🎯 {pair} {sweep} DETECTED! Generating chart...")
                    chart_image = generate_chart(c15)
                    
                    if chart_image:
                        ai_response = ask_ai_vision(pair, context, sweep, chart_image)
                        
                        if "NO TRADE" in ai_response:
                            log(f"  ⏭ {pair}: AI visually rejected the setup.")
                        else:
                            try:
                                clean_json = ai_response.replace("```json", "").replace("```", "").strip()
                                trade = json.loads(clean_json)
                                
                                caption = f"🎯 <b>SNIPER ALERT: {trade['signal']} {pair}</b>\n"
                                caption += f"🚨 <i>{sweep.replace('_', ' ').title()}</i>\n"
                                caption += f"📍 Entry: {trade['entry']} | 🛑 SL: {trade['sl']} | 🎯 TP: {trade['tp']}\n"
                                caption += f"━━━━━━━━━━━━━━━━━━━━\n"
                                caption += f"🧠 <b>AI Vision:</b> {trade['reasoning']}"
                                
                                send_telegram_photo(chart_image, caption)
                                log(f"  ✅ SENT: {pair} {trade['signal']}")
                                
                                daily_pair_signals[pair] = 1
                                active_trades.append({
                                    "pair": pair, "signal": trade["signal"], 
                                    "entry": float(trade["entry"]), "sl": float(trade["sl"]), "tp": float(trade["tp"]), "time": now
                                })
                            except Exception as e:
                                log(f"  ⚠️ Parse error: {ai_response}")

            for trade in active_trades[:]:
                cur_price = get_current_price(trade["pair"])
                time.sleep(API_DELAY)
                if not cur_price: continue
                
                hit = None
                if trade["signal"] == "BUY":
                    if cur_price >= trade["tp"]: hit = "TP"
                    elif cur_price <= trade["sl"]: hit = "SL"
                else:
                    if cur_price <= trade["tp"]: hit = "TP"
                    elif cur_price >= trade["sl"]: hit = "SL"
                    
                if hit == "TP":
                    send_telegram(f"🎯 <b>TP HIT!</b> — {trade['pair']} {trade['signal']}\n✅ The market paid us.")
                    active_trades.remove(trade)
                elif hit == "SL":
                    send_telegram(f"🛑 <b>SL HIT</b> — {trade['pair']} {trade['signal']}\n❌ We took a loss.")
                    active_trades.remove(trade)
                elif (now - trade["time"]) > timedelta(hours=24):
                    active_trades.remove(trade)

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            log(f"Loop Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    main_loop()
