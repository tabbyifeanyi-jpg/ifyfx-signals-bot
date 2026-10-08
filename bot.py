
import os
import json
import time
import requests
from datetime import datetime, timezone

# ===== CONFIG =====
BOT_TOKEN = "8574344733:AAEQkHHjrMQ4cq_KWpKW0IL2RiTRjlVD2BI"
CHAT_ID = "-1004375163609"
TWELVEDATA_KEY = "944649b4a9d24074bbb41b2a954639cb"
FMP_KEY = "QmnG8t5DMUrIf8AWB7zHWcYa70kM4OCh"
PAIRS = ["EUR/USD", "USD/JPY", "GBP/USD", "AUD/USD", "USD/CAD", "XAU/USD"]
WAT_OFFSET = 1
SCAN_INTERVAL = 15 * 60  # 15 minutes

# ===== TELEGRAM =====
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"Telegram error: {e}")
        return False

# ===== FETCH CANDLES =====
def get_candles(symbol, interval, outputsize=100):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVEDATA_KEY}
    try:
        r = requests.get(url, params=params, timeout=15)
        data = r.json()
        if "values" in data:
            return list(reversed(data["values"]))
        return None
    except Exception as e:
        print(f"Candle error {symbol}: {e}")
        return None

# ===== INDICATORS =====
def calc_ema(values, period):
    if len(values) < period: return None
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema

def calc_rsi(closes, period=14):
    if len(closes) < period + 1: return None
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag, al = sum(gains[:period])/period, sum(losses[:period])/period
    for i in range(period, len(gains)):
        ag = (ag*(period-1)+gains[i])/period
        al = (al*(period-1)+losses[i])/period
    if al == 0: return 100.0
    return 100 - (100 / (1 + ag/al))

def calc_atr(candles, period=14):
    if len(candles) < period + 1: return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"]); l = float(candles[i]["low"])
        pc = float(candles[i-1]["close"])
        trs.append(max(h-l, abs(h-pc), abs(l-pc)))
    atr = sum(trs[:period])/period
    for i in range(period, len(trs)):
        atr = (atr*(period-1)+trs[i])/period
    return atr

# ===== ANALYZE =====
def analyze(symbol):
    c15 = get_candles(symbol, "15min", 100)
    c1h = get_candles(symbol, "1h", 100)
    if not c15 or not c1h: return None
    
    closes_15 = [float(c["close"]) for c in c15]
    closes_1h = [float(c["close"]) for c in c1h]
    
    rsi_15 = calc_rsi(closes_15, 14)
    ema20_1h = calc_ema(closes_1h, 20)
    ema50_1h = calc_ema(closes_1h, 50)
    atr_15 = calc_atr(c15, 14)
    price = closes_15[-1]
    
    if None in (rsi_15, ema20_1h, ema50_1h, atr_15): return None
    
    if ema20_1h > ema50_1h and price > ema20_1h:
        trend = "↑ Bullish"
    elif ema20_1h < ema50_1h and price < ema20_1h:
        trend = "↓ Bearish"
    else:
        trend = "→ Neutral"
    
    return {"symbol": symbol, "price": price, "rsi": rsi_15, "trend": trend, "atr": atr_15}

# ===== MAIN LOOP =====
def main():
    print("Ifyfx Signals Bot starting...")
    send_telegram("🤖 <b>Ifyfx Signals Bot is ONLINE</b>\n\nScanning markets every 15 minutes...")
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            wat = now.hour + WAT_OFFSET
            print(f"Scanning at {wat:02d}:{now.minute:02d} WAT...")
            
            results = []
            for pair in PAIRS:
                r = analyze(pair)
                if r:
                    results.append(r)
                    print(f"  {pair}: RSI {r['rsi']:.1f} {r['trend']}")
                time.sleep(2)  # Rate limit
            
            if results:
                msg = "<b>📊 MARKET SCAN</b>\n━━━━━━━━━━━━━━━━━━━━\n"
                for r in results:
                    msg += f"<b>{r['symbol']}</b> {r['trend']}\n"
                    msg += f"  RSI: {r['rsi']:.1f}  |  {r['price']:.5f}\n"
                msg += "━━━━━━━━━━━━━━━━━━━━\n<i>Ifyfx Signals Bot</i>"
                send_telegram(msg)
            
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
