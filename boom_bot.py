import os
import time
import json
import requests
import threading
import websocket
import io
import base64
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageDraw

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN") 
CHAT_ID = os.getenv("CHAT_ID", "-1004375163609")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

PAIRS = ["BOOM1000", "CRASH1000"] 
WAT_OFFSET = 1
SCAN_INTERVAL = 15 * 60
API_DELAY = 3

active_trades = []
last_update_id = 0

def log(msg):
    print(msg, flush=True)
    sys.stdout.flush()

class SimpleHTTPRequestHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Boom/Crash Bot is alive")

def run_web_server():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(('0.0.0.0', port), SimpleHTTPRequestHandler)
    server.serve_forever()

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        return r.json().get("ok", False)
    except: return False

def send_telegram_photo(image_bytes, caption):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto"
    try:
        files = {"photo": ("chart.png", image_bytes, "image/png")}
        data = {"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"}
        r = requests.post(url, files=files, data=data, timeout=30)
        return r.json().get("ok", False)
    except: return False

# ===== DERIV API (WEBSOCKET) WITH BROWSER HEADER =====
def get_deriv_candles(symbol, count=50, granularity=3600): # 3600 = 1 Hour
    url = "wss://ws.derivws.com/websockets/v3?app_id=1089"
    try:
        # This header tricks Cloudflare into thinking we are a real browser
        header = ["User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"]
        ws = websocket.create_connection(url, timeout=10, header=header)
        
        req = {
            "ticks_history": symbol,
            "adjust_start_time": 1,
            "count": count,
            "end": "latest",
            "start": 1,
            "style": "candles",
            "granularity": granularity
        }
        ws.send(json.dumps(req))
        result = ws.recv()
        data = json.loads(result)
        ws.close()
        
        if "candles" in data:
            return data["candles"]
        return []
    except Exception as e:
        log(f"Deriv WS Error for {symbol}: {e}")
        return []

def generate_chart(candles, pair):
    if not candles: return None
    width, height = 800, 500
    img = Image.new('RGB', (width, height), color='#1a1a1a')
    draw = ImageDraw.Draw(img)
    
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    max_p, min_p = max(highs), min(lows)
    price_range = max_p - min_p if max_p != min_p else 1
    
    margin = 50
    chart_h = height - (margin * 2)
    chart_w = width - (margin * 2)
    candle_w = chart_w / len(candles)
    
    def get_y(price): return height - margin - ((price - min_p) / price_range * chart_h)
    
    for i, c in enumerate(candles):
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

def ask_ai_vision(pair, image_bytes):
    prompt = f"""
    You are an expert trader on Deriv Synthetic Indices.
    {pair} has a unique mechanic: it drifts slowly in one direction, then spikes violently.
    - BOOM1000 drifts DOWN slowly, then spikes UP.
    - CRASH1000 drifts UP slowly, then crashes DOWN.
    
    Look at the 1-Hour chart attached. 
    Is the price exhausted? Is it consolidating in a tight range after a long drift?
    If you see a high probability that a spike is imminent, output a JSON object:
    "signal" (BUY for Boom, SELL for Crash), "entry" (current price), "sl" (stop loss), "tp" (take profit), "reasoning" (explain what you see).
    If the chart is messy or the trend is still strong, output ONLY "NO TRADE".
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
        return res['candidates'][0]['content']['parts'][0]['text'].strip()
    except Exception as e:
        log(f"AI Error: {e}")
        return "NO TRADE"

def telegram_listener():
    global last_update_id
    log("Telegram Command Listener Started...")
    while True:
        try:
            url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates?offset={last_update_id + 1}&timeout=10"
            r = requests.get(url, timeout=15)
            data = r.json()
            if data.get("ok") and data.get("result"):
                for update in data["result"]:
                    last_update_id = update["update_id"]
                    message = update.get("message", {})
                    text = message.get("text", "").lower()
                    chat_id = str(message.get("chat", {}).get("id", ""))
                    if chat_id == CHAT_ID:
                        if text.startswith("/status"):
                            send_telegram("🔥 <b>Boom/Crash Spike Hunter is Online!</b>\n\nWatching BOOM1000 & CRASH1000.")
        except: pass
        time.sleep(2)

def main_loop():
    log("=== BOOM/CRASH BOT STARTED ===")
    send_telegram("🔥 <b>BOOM & CRASH SPIKE HUNTER ONLINE</b>\n\nUsing AI Vision to hunt the Overextension Trap.")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            wat = (now.hour + WAT_OFFSET) % 24
            log(f"[{wat:02d}:{now.minute:02d} WAT] Scanning Synthetics...")

            for pair in PAIRS:
                candles = get_deriv_candles(pair, 50, 3600)
                time.sleep(API_DELAY)
                if not candles: continue

                log(f"  🎯 {pair} checking chart...")
                chart_image = generate_chart(candles, pair)
                
                if chart_image:
                    ai_response = ask_ai_vision(pair, chart_image)
                    
                    if "NO TRADE" in ai_response:
                        log(f"  ⏭ {pair}: AI rejected setup.")
                    else:
                        try:
                            clean_json = ai_response.replace("```json", "").replace("```", "").strip()
                            trade = json.loads(clean_json)
                            
                            caption = f"🔥 <b>SPIKE ALERT: {trade['signal']} {pair}</b>\n"
                            caption += f"📍 Entry: {trade['entry']} | 🛑 SL: {trade['sl']} | 🎯 TP: {trade['tp']}\n"
                            caption += f"━━━━━━━━━━━━━━━━━━━━\n"
                            caption += f"🧠 <b>AI Vision:</b> {trade['reasoning']}"
                            
                            send_telegram_photo(chart_image, caption)
                            log(f"  ✅ SENT: {pair} {trade['signal']}")
                        except Exception as e:
                            log(f"  ⚠️ Parse error: {ai_response}")

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            log(f"Loop Error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=telegram_listener, daemon=True).start()
    main_loop()
