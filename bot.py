import time
import requests
import json
from datetime import datetime, timezone, timedelta

# ===== CONFIG =====
BOT_TOKEN = "8574344733:AAEQkHHjrMQ4cq_KWpKW0IL2RiTRjlVD2BI"
CHAT_ID = "-1004375163609"
TWELVEDATA_KEY = "944649b4a9d24074bbb41b2a954639cb"
FMP_KEY = "QmnG8t5DMUrIf8AWB7zHWcYa70kM4OCh"
PAIRS = ["EUR/USD", "USD/JPY", "GBP/USD", "AUD/USD", "USD/CAD", "XAU/USD"]
WAT_OFFSET = 1
SCAN_INTERVAL = 15 * 60
MAX_SIGNALS_PER_DAY = 4
CONFIDENCE_THRESHOLD = 75
DEDUP_HOURS = 4
SUMMARY_HOUR_WAT = 8  # 8 AM WAT daily summary

# ===== STATE =====
signal_history = []
daily_signal_count = 0
last_reset_date = None
last_summary_date = None
last_news_warn_time = None

# ===== TELEGRAM =====
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"TG error: {e}")
        return False

# ===== TWELVE DATA =====
def get_candles(symbol, interval, outputsize=100):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVEDATA_KEY}
    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
        if "values" in data:
            return list(reversed(data["values"]))
        return None
    except Exception as e:
        print(f"Candle err {symbol}: {e}")
        return None

# ===== NEWS (FMP) =====
def get_news_events():
    """Fetch today's high-impact news events from FMP."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"https://financialmodelingprep.com/api/v3/economic_calendar?from={today}&to={today}&apikey={FMP_KEY}"
    try:
        r = requests.get(url, timeout=20)
        events = r.json()
        if not isinstance(events, list):
            return []
        high = []
        for e in events:
            impact = (e.get("impact") or "").lower()
            if impact in ("high", "medium"):
                high.append({
                    "event": e.get("event", "Unknown"),
                    "currency": e.get("currency", ""),
                    "date": e.get("date", ""),
                    "impact": impact,
                })
        return high
    except Exception as e:
        print(f"News err: {e}")
        return []

def is_news_blackout():
    """True if high-impact news is within 60 min before/after."""
    now = datetime.now(timezone.utc)
    events = get_news_events()
    for ev in events:
        try:
            evt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            delta_min = (evt - now).total_seconds() / 60
            if -60 <= delta_min <= 60:
                return ev, delta_min
        except:
            continue
    return None, None

def upcoming_news_warning():
    """True if a high-impact event is 60-120 min away."""
    now = datetime.now(timezone.utc)
    events = get_news_events()
    for ev in events:
        try:
            evt = datetime.fromisoformat(ev["date"].replace("Z", "+00:00"))
            delta_min = (evt - now).total_seconds() / 60
            if 60 < delta_min <= 120:
                return ev, delta_min
        except:
            continue
    return None, None

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

def find_zones(candles, lookback=20):
    highs = [float(c["high"]) for c in candles[-lookback:]]
    lows = [float(c["low"]) for c in candles[-lookback:]]
    return max(highs), min(lows)

def detect_pattern(candle):
    o = float(candle["open"]); h = float(candle["high"])
    l = float(candle["low"]); c = float(candle["close"])
    body = abs(c - o); range_ = h - l
    if range_ == 0: return None
    upper_wick = h - max(o, c)
    lower_wick = min(o, c) - l
    if lower_wick > body * 2 and upper_wick < body:
        return "Bullish Pin Bar"
    if upper_wick > body * 2 and lower_wick < body:
        return "Bearish Pin Bar"
    if body < range_ * 0.1:
        return "Doji"
    return None

# ===== TIME HELPERS =====
def is_prime_time():
    now = datetime.now(timezone.utc)
    wat_hour = (now.hour + WAT_OFFSET) % 24
    return 13 <= wat_hour < 17

def is_weekend():
    return datetime.now(timezone.utc).weekday() >= 5  # Sat=5, Sun=6

def is_active_session():
    """London (7-16 UTC), NY (12-21 UTC), Tokyo (0-9 UTC)."""
    h = datetime.now(timezone.utc).hour
    return (7 <= h < 21)

# ===== STATE RESET =====
def check_daily_reset():
    global daily_signal_count, last_reset_date, last_summary_date
    today = datetime.now(timezone.utc).date()
    if last_reset_date != today:
        daily_signal_count = 0
        last_reset_date = today
        print("[RESET] New day")

def should_send_summary():
    global last_summary_date
    now = datetime.now(timezone.utc)
    wat_hour = (now.hour + WAT_OFFSET) % 24
    today = now.date()
    if wat_hour == SUMMARY_HOUR_WAT and last_summary_date != today:
        last_summary_date = today
        return True
    return False

# ===== DEDUP + CORRELATION =====
def is_duplicate(pair, signal):
    now = datetime.now(timezone.utc)
    for h in signal_history:
        if h["pair"] == pair and h["signal"] == signal:
            if (now - h["time"]) < timedelta(hours=DEDUP_HOURS):
                return True
    return False

def usd_correlated_recently():
    """True if 2+ USD-pairs signaled in last 30 min."""
    now = datetime.now(timezone.utc)
    usd_pairs = ["EUR/USD", "GBP/USD", "AUD/USD", "USD/CAD", "USD/JPY"]
    count = 0
    for h in signal_history:
        if h["pair"] in usd_pairs and (now - h["time"]) < timedelta(minutes=30):
            count += 1
    return count >= 2

# ===== ANALYZE =====
def analyze(pair):
    if is_weekend() and pair != "XAU/USD":
        return None
    if not is_active_session():
        return None
    
    c15 = get_candles(pair, "15min", 100)
    c1h = get_candles(pair, "1h", 100)
    if not c15 or not c1h: return None
    
    closes_15 = [float(c["close"]) for c in c15]
    closes_1h = [float(c["close"]) for c in c1h]
    
    rsi_15 = calc_rsi(closes_15, 14)
    ema20_1h = calc_ema(closes_1h, 20)
    ema50_1h = calc_ema(closes_1h, 50)
    atr_15 = calc_atr(c15, 14)
    price = closes_15[-1]
    resistance, support = find_zones(c15, 20)
    pattern = detect_pattern(c15[-1])
    
    if None in (rsi_15, ema20_1h, ema50_1h, atr_15): return None
    
    trend_bull = ema20_1h > ema50_1h and price > ema20_1h
    trend_bear = ema20_1h < ema50_1h and price < ema20_1h
    
    dist_support = abs(price - support) / atr_15 if atr_15 else 999
    dist_resistance = abs(price - resistance) / atr_15 if atr_15 else 999
    
    signal = None; confidence = 0; reasons = []
    
    if trend_bull:
        confidence += 30; reasons.append("1h bullish")
        if rsi_15 < 40:
            confidence += 25; reasons.append(f"RSI {rsi_15:.1f} oversold")
        if dist_support < 1.5:
            confidence += 20; reasons.append("Near support")
        if pattern == "Bullish Pin Bar":
            confidence += 15; reasons.append("Bullish pin bar")
        if is_prime_time():
            confidence += 10; reasons.append("Prime time")
        if confidence >= CONFIDENCE_THRESHOLD:
            signal = "BUY"
    elif trend_bear:
        confidence += 30; reasons.append("1h bearish")
        if rsi_15 > 60:
            confidence += 25; reasons.append(f"RSI {rsi_15:.1f} overbought")
        if dist_resistance < 1.5:
            confidence += 20; reasons.append("Near resistance")
        if pattern == "Bearish Pin Bar":
            confidence += 15; reasons.append("Bearish pin bar")
        if is_prime_time():
            confidence += 10; reasons.append("Prime time")
        if confidence >= CONFIDENCE_THRESHOLD:
            signal = "SELL"
    
    if not signal: return None
    
    if signal == "BUY":
        sl = price - (atr_15 * 1.2)
        tp = price + (atr_15 * 2.0)
    else:
        sl = price + (atr_15 * 1.2)
        tp = price - (atr_15 * 2.0)
    
    return {
        "pair": pair, "signal": signal, "price": price,
        "sl": sl, "tp": tp, "atr": atr_15, "rsi": rsi_15,
        "confidence": confidence, "reasons": reasons, "pattern": pattern,
        "trend": "Bullish" if trend_bull else "Bearish",
        "prime": is_prime_time(),
    }

# ===== FORMATTERS =====
def format_signal(s):
    emoji = "🟢" if s["signal"] == "BUY" else "🔴"
    decimals = 3 if "JPY" in s["pair"] else (2 if "XAU" in s["pair"] else 5)
    fmt = f".{decimals}f"
    prime_tag = " ⭐" if s["prime"] else ""
    msg = f"{emoji} <b>{s['signal']} — {s['pair']}</b>{prime_tag}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📍 Entry:       {s['price']:{fmt}}\n"
    msg += f"🛑 Stop Loss:   {s['sl']:{fmt}}\n"
    msg += f"🎯 Take Profit: {s['tp']:{fmt}}\n"
    msg += f"📊 Confidence:  <b>{s['confidence']}%</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Trend: {s['trend']} | 📉 RSI: {s['rsi']:.1f}\n"
    if s["pattern"]:
        msg += f"🕯 {s['pattern']}\n"
    msg += "\n<b>Why:</b>\n"
    for r in s["reasons"]:
        msg += f"  • {r}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<i>Not financial advice</i>"
    return msg

def format_summary(results):
    msg = "<b>☀️ MORNING BRIEF — 8 AM WAT</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    for r in results:
        msg += f"<b>{r['pair']}</b> {r['trend']}\n"
        msg += f"  RSI {r['rsi']:.1f}  |  {r['price']:.5f}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<i>Signals today: 0 so far</i>"
    return msg

# ===== MAIN =====
def main():
    global daily_signal_count, last_news_warn_time
    print("Ifyfx Signals Bot v3 starting...")
    send_telegram("🤖 <b>Ifyfx Signals Bot v3 ONLINE</b>\n\nFull feature engine active ✅")
    
    while True:
        try:
            check_daily_reset()
            now = datetime.now(timezone.utc)
            wat = (now.hour + WAT_OFFSET) % 24
            print(f"\n[{wat:02d}:{now.minute:02d} WAT] Scanning...")
            
            # ===== NEWS BLACKOUT CHECK =====
            news, mins = is_news_blackout()
            if news:
                print(f"  🔕 NEWS BLACKOUT: {news['event']} ({mins:.0f}min)")
                time.sleep(SCAN_INTERVAL)
                continue
            
            # ===== NEWS WARNING =====
            warn, warn_mins = upcoming_news_warning()
            if warn and (last_news_warn_time is None or 
                        (now - last_news_warn_time) > timedelta(hours=2)):
                warn_msg = f"⚠️ <b>NEWS ALERT</b>\n\n"
                warn_msg += f"📰 {warn['event']} ({warn['currency']})\n"
                warn_msg += f"🕐 In ~{warn_mins:.0f} minutes\n\n"
                warn_msg += "<i>Signals paused 60 min around release</i>"
                send_telegram(warn_msg)
                last_news_warn_time = now
                print(f"  ⚠️ News warning sent")
            
            # ===== SCAN =====
            scan_results = []
            signals_found = []
            
            for pair in PAIRS:
                result = analyze(pair)
                if result and "signal" in result:
                    if not is_duplicate(result["pair"], result["signal"]):
                        if result["confidence"] >= CONFIDENCE_THRESHOLD:
                            signals_found.append(result)
                else:
                    # gather scan data for summary
                    c15 = get_candles(pair, "15min", 30)
                    if c15:
                        closes = [float(c["close"]) for c in c15]
                        scan_results.append({
                            "pair": pair,
                            "rsi": calc_rsi(closes, 14) or 50,
                            "price": closes[-1],
                            "trend": "—",
                        })
                time.sleep(2)
            
            # ===== CORRELATION CHECK =====
            if usd_correlated_recently():
                signals_found = [s for s in signals_found if "XAU" in s["pair"] or "JPY" in s["pair"]]
                print(f"  ⚠️ USD correlation filter applied")
            
            # ===== SEND SIGNALS =====
            for s in signals_found:
                if daily_signal_count < MAX_SIGNALS_PER_DAY:
                    send_telegram(format_signal(s))
                    signal_history.append({"pair": s["pair"], "signal": s["signal"],
                                            "time": datetime.now(timezone.utc),
                                            "entry": s["price"], "sl": s["sl"], "tp": s["tp"]})
                    daily_signal_count += 1
                    print(f"  ✅ Signal #{daily_signal_count}: {s['pair']} {s['signal']}")
                else:
                    print(f"  ⛔ Daily limit reached")
            
            # ===== MORNING SUMMARY =====
            if should_send_summary() and scan_results:
                send_telegram(format_summary(scan_results))
                print(f"  ☀️ Morning summary sent")
            
            # ===== FOLLOW-UP CHECK (TP/SL hits) =====
            for h in signal_history[:]:
                if "checked" in h: continue
                if (now - h["time"]) > timedelta(hours=6): continue
                # fetch current price
                c = get_candles(h["pair"], "15min", 2)
                if c:
                    cur = float(c[-1]["close"])
                    if h["signal"] == "BUY":
                        if cur >= h["tp"]:
                            send_telegram(f"🎯 <b>TP HIT</b> — {h['pair']} BUY\n<i>+{abs(cur-h['entry']):.5f} pips</i>")
                            h["checked"] = True
                        elif cur <= h["sl"]:
                            send_telegram(f"🛑 <b>SL HIT</b> — {h['pair']} BUY\n<i>-{abs(h['entry']-cur):.5f} pips</i>")
                            h["checked"] = True
                    else:
                        if cur <= h["tp"]:
                            send_telegram(f"🎯 <b>TP HIT</b> — {h['pair']} SELL")
                            h["checked"] = True
                        elif cur >= h["sl"]:
                            send_telegram(f"🛑 <b>SL HIT</b> — {h['pair']} SELL")
                            h["checked"] = True
                    time.sleep(1)
            
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
