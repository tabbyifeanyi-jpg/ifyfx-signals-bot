import time
import requests
from datetime import datetime, timezone, timedelta

# ===== CONFIG =====
BOT_TOKEN = "PASTE_YOUR_NEW_TOKEN_HERE"
CHAT_ID = "-1004375163609"
TWELVEDATA_KEY = "944649b4a9d24074bbb41b2a954639cb"
FMP_KEY = "QmnG8t5DMUrIf8AWB7zHWcYa70kM4OCh"
PAIRS = ["EUR/USD", "USD/JPY", "GBP/USD", "AUD/USD", "USD/CAD", "XAU/USD"]
WAT_OFFSET = 1
SCAN_INTERVAL = 15 * 60
MAX_SIGNALS_PER_DAY = 8
CONFIDENCE_THRESHOLD = 55
DEDUP_HOURS = 4
SUMMARY_HOUR_WAT = 8
SENTIMENT_CACHE_MIN = 120

# ===== SENTIMENT KEYWORDS =====
BULLISH_WORDS = ["rise","rises","rally","rallying","surge","surges","gain","gains",
                 "strong","stronger","upbeat","bullish","boost","higher","climb",
                 "climbs","jump","jumps","soar","soars","upgrade","optimism","record high"]
BEARISH_WORDS = ["fall","falls","drop","drops","plunge","plunges","crash","crashes",
                 "weak","weaker","downbeat","bearish","slump","lower","slide","slides",
                 "sink","sinks","tumble","tumbles","downgrade","pessimism","record low"]

# ===== STATE =====
signal_history = []
daily_signal_count = 0
last_reset_date = None
last_summary_date = None
last_news_warn_time = None
sentiment_cache = {}

# ===== TELEGRAM =====
def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"TG error: {e}")
        return False

# ===== TWELVE DATA (only closed candles) =====
def get_candles(symbol, interval, outputsize=100):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVEDATA_KEY}
    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
        if "values" not in data:
            print(f"    API fail {symbol} {interval}: {data.get('message', 'unknown')}")
            return None
        candles = list(reversed(data["values"]))
        now_utc = datetime.now(timezone.utc)
        interval_min = {"15min": 15, "30min": 30, "1h": 60}.get(interval, 15)
        if candles:
            try:
                last_dt = datetime.fromisoformat(candles[-1]["datetime"]).replace(tzinfo=timezone.utc)
                if (now_utc - last_dt).total_seconds() < interval_min * 60:
                    candles = candles[:-1]
            except:
                pass
        return candles
    except Exception as e:
        print(f"    Candle err {symbol}: {e}")
        return None

# ===== SENTIMENT =====
def fetch_sentiment(currency):
    now = datetime.now(timezone.utc)
    if currency in sentiment_cache:
        score, ts = sentiment_cache[currency]
        if (now - ts) < timedelta(minutes=SENTIMENT_CACHE_MIN):
            return score
    
    url = f"https://financialmodelingprep.com/api/v3/news?apikey={FMP_KEY}&limit=50"
    try:
        r = requests.get(url, timeout=15)
        news = r.json()
        if not isinstance(news, list):
            return 0
    except:
        return 0
    
    score = 0
    count = 0
    for item in news[:50]:
        title = (item.get("title") or "").lower()
        text = (item.get("text") or "").lower()[:500]
        combined = title + " " + text
        currency_keywords = {
            "USD": ["dollar","fed","federal reserve","usd","powell","treasury"],
            "EUR": ["euro","ecb","eur","lagarde"],
            "GBP": ["pound","boe","gbp","bank of england","sterling"],
            "JPY": ["yen","boj","jpy","bank of japan"],
            "AUD": ["aussie","rba","aud","australia"],
            "CAD": ["loonie","boc","cad","canada"],
            "XAU": ["gold","xau","bullion"],
        }
        keys = currency_keywords.get(currency, [currency.lower()])
        if not any(k in combined for k in keys):
            continue
        bull = sum(1 for w in BULLISH_WORDS if w in combined)
        bear = sum(1 for w in BEARISH_WORDS if w in combined)
        score += (bull - bear)
        count += 1
    
    if count > 0:
        score = score / max(count, 5) * 10
    score = max(-10, min(10, score))
    sentiment_cache[currency] = (score, now)
    return score

def get_pair_sentiment(pair):
    if "XAU" in pair:
        return fetch_sentiment("XAU")
    base, quote = pair.split("/")
    return (fetch_sentiment(base) - fetch_sentiment(quote)) / 2

# ===== NEWS =====
def get_news_events():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"https://financialmodelingprep.com/api/v3/economic_calendar?from={today}&to={today}&apikey={FMP_KEY}"
    try:
        r = requests.get(url, timeout=20)
        events = r.json()
        if not isinstance(events, list): return []
        return [{"event": e.get("event",""), "currency": e.get("currency",""),
                 "date": e.get("date",""), "impact": (e.get("impact") or "").lower()}
                for e in events if (e.get("impact") or "").lower() == "high"]
    except:
        return []

def is_news_blackout():
    now = datetime.now(timezone.utc)
    for ev in get_news_events():
        try:
            evt = datetime.fromisoformat(ev["date"].replace("Z","+00:00"))
            dm = (evt - now).total_seconds()/60
            if -60 <= dm <= 60: return ev, dm
        except: continue
    return None, None

def upcoming_news_warning():
    now = datetime.now(timezone.utc)
    for ev in get_news_events():
        try:
            evt = datetime.fromisoformat(ev["date"].replace("Z","+00:00"))
            dm = (evt - now).total_seconds()/60
            if 60 < dm <= 120: return ev, dm
        except: continue
    return None, None

# ===== INDICATORS =====
def calc_ema(vals, p):
    if len(vals) < p: return None
    k = 2/(p+1); ema = sum(vals[:p])/p
    for v in vals[p:]: ema = v*k + ema*(1-k)
    return ema

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
    if len(candles) < p+1: return None
    trs = []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"]); lo = float(candles[i]["low"])
        pc = float(candles[i-1]["close"])
        trs.append(max(h-lo, abs(h-pc), abs(lo-pc)))
    atr = sum(trs[:p])/p
    for i in range(p, len(trs)): atr = (atr*(p-1)+trs[i])/p
    return atr

def find_zones(candles, lb=20):
    highs = [float(c["high"]) for c in candles[-lb:]]
    lows = [float(c["low"]) for c in candles[-lb:]]
    return max(highs), min(lows)

def detect_pattern(c):
    o = float(c["open"]); h = float(c["high"])
    lo = float(c["low"]); cl = float(c["close"])
    body = abs(cl-o); rng = h-lo
    if rng == 0: return None
    uw = h - max(o, cl); lw = min(o, cl) - lo
    if lw > body*2 and uw < body: return "Bullish Pin Bar"
    if uw > body*2 and lw < body: return "Bearish Pin Bar"
    if body < rng*0.1: return "Doji"
    return None

# ===== TIME =====
def is_prime_time():
    h = (datetime.now(timezone.utc).hour + WAT_OFFSET) % 24
    return 13 <= h < 17

def is_weekend():
    return datetime.now(timezone.utc).weekday() >= 5

def is_active_session():
    return 7 <= datetime.now(timezone.utc).hour < 21

# ===== STATE =====
def check_daily_reset():
    global daily_signal_count, last_reset_date
    today = datetime.now(timezone.utc).date()
    if last_reset_date != today:
        daily_signal_count = 0
        last_reset_date = today
        print("[RESET] New day")

def should_send_summary():
    global last_summary_date
    now = datetime.now(timezone.utc)
    wh = (now.hour + WAT_OFFSET) % 24
    if wh == SUMMARY_HOUR_WAT and last_summary_date != now.date():
        last_summary_date = now.date()
        return True
    return False

def is_duplicate(pair, signal):
    now = datetime.now(timezone.utc)
    for h in signal_history:
        if h["pair"] == pair and h["signal"] == signal and (now - h["time"]) < timedelta(hours=DEDUP_HOURS):
            return True
    return False

def usd_correlated_recently():
    now = datetime.now(timezone.utc)
    usd_pairs = ["EUR/USD","GBP/USD","AUD/USD","USD/CAD","USD/JPY"]
    count = sum(1 for h in signal_history if h["pair"] in usd_pairs and (now - h["time"]) < timedelta(minutes=30))
    return count >= 2

# ===== ANALYZE =====
def analyze(pair):
    if is_weekend() and pair != "XAU/USD":
        return {"pair": pair, "skip": "weekend"}
    if not is_active_session():
        return {"pair": pair, "skip": "session"}

    c15 = get_candles(pair, "15min", 100)
    c30 = get_candles(pair, "30min", 100)
    c1h = get_candles(pair, "1h", 100)
    if not c15 or not c30 or not c1h:
        return {"pair": pair, "skip": "no_candles"}
    if len(c15) < 50 or len(c30) < 50 or len(c1h) < 50:
        return {"pair": pair, "skip": "not_enough_data"}

    closes_15 = [float(c["close"]) for c in c15]
    closes_30 = [float(c["close"]) for c in c30]
    closes_1h = [float(c["close"]) for c in c1h]

    rsi_15 = calc_rsi(closes_15, 14)
    atr_15 = calc_atr(c15, 14)
    price = closes_15[-1]
    resistance, support = find_zones(c15, 20)
    pattern = detect_pattern(c15[-1])
    rsi_30 = calc_rsi(closes_30, 14)
    ema20_30 = calc_ema(closes_30, 20)
    ema50_30 = calc_ema(closes_30, 50)
    ema20_1h = calc_ema(closes_1h, 20)
    ema50_1h = calc_ema(closes_1h, 50)

    if None in (rsi_15, atr_15, rsi_30, ema20_30, ema50_30, ema20_1h, ema50_1h):
        return {"pair": pair, "skip": "indicator_calc_failed"}

    trend_bull_1h = ema20_1h > ema50_1h and price > ema20_1h
    trend_bear_1h = ema20_1h < ema50_1h and price < ema20_1h
    trend_bull_30 = ema20_30 > ema50_30
    trend_bear_30 = ema20_30 < ema50_30

    dist_support = abs(price - support) / atr_15 if atr_15 else 999
    dist_resist = abs(price - resistance) / atr_15 if atr_15 else 999

    sentiment = get_pair_sentiment(pair)

    signal = None; confidence = 0; reasons = []

    if trend_bull_1h:
        confidence += 30; reasons.append("1h bullish trend")
        if trend_bull_30:
            confidence += 15; reasons.append("30m confirms bullish")
        if rsi_15 < 40:
            confidence += 20; reasons.append(f"RSI 15m {rsi_15:.1f} oversold")
        if rsi_30 < 45:
            confidence += 10; reasons.append(f"RSI 30m {rsi_30:.1f} supports")
        if dist_support < 1.5:
            confidence += 15; reasons.append("Near support zone")
        if pattern == "Bullish Pin Bar":
            confidence += 10; reasons.append("Bullish pin bar")
        if is_prime_time():
            confidence += 5; reasons.append("Prime time")
        if sentiment >= 3:
            confidence += 15; reasons.append(f"Sentiment +{sentiment:.1f} supports")
        elif sentiment <= -3:
            confidence -= 20; reasons.append(f"⚠️ Sentiment {sentiment:.1f} contradicts")
        if confidence >= CONFIDENCE_THRESHOLD and sentiment > -5:
            signal = "BUY"

    elif trend_bear_1h:
        confidence += 30; reasons.append("1h bearish trend")
        if trend_bear_30:
            confidence += 15; reasons.append("30m confirms bearish")
        if rsi_15 > 60:
            confidence += 20; reasons.append(f"RSI 15m {rsi_15:.1f} overbought")
        if rsi_30 > 55:
            confidence += 10; reasons.append(f"RSI 30m {rsi_30:.1f} supports")
        if dist_resist < 1.5:
            confidence += 15; reasons.append("Near resistance zone")
        if pattern == "Bearish Pin Bar":
            confidence += 10; reasons.append("Bearish pin bar")
        if is_prime_time():
            confidence += 5; reasons.append("Prime time")
        if sentiment <= -3:
            confidence += 15; reasons.append(f"Sentiment {sentiment:.1f} supports")
        elif sentiment >= 3:
            confidence -= 20; reasons.append(f"⚠️ Sentiment +{sentiment:.1f} contradicts")
        if confidence >= CONFIDENCE_THRESHOLD and sentiment < 5:
            signal = "SELL"

    result = {
        "pair": pair, "price": price, "rsi": rsi_15, "rsi_30": rsi_30,
        "trend": "Bullish" if trend_bull_1h else ("Bearish" if trend_bear_1h else "Neutral"),
        "confidence": confidence, "sentiment": sentiment,
    }

    if signal:
        if signal == "BUY":
            sl = price - atr_15 * 1.2
            tp = price + atr_15 * 2.0
        else:
            sl = price + atr_15 * 1.2
            tp = price - atr_15 * 2.0
        result.update({
            "signal": signal, "sl": sl, "tp": tp, "atr": atr_15,
            "reasons": reasons, "pattern": pattern, "prime": is_prime_time(),
        })

    return result

# ===== FORMATTERS =====
def format_signal(s):
    emoji = "🟢" if s["signal"] == "BUY" else "🔴"
    dec = 3 if "JPY" in s["pair"] else (2 if "XAU" in s["pair"] else 5)
    fmt = f".{dec}f"
    prime_tag = " ⭐" if s.get("prime") else ""
    sent_emoji = "😊" if s["sentiment"] > 2 else ("😟" if s["sentiment"] < -2 else "😐")
    
    msg = f"{emoji} <b>{s['signal']} — {s['pair']}</b>{prime_tag}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📍 Entry:       {s['price']:{fmt}}\n"
    msg += f"🛑 Stop Loss:   {s['sl']:{fmt}}\n"
    msg += f"🎯 Take Profit: {s['tp']:{fmt}}\n"
    msg += f"📊 Confidence:  <b>{s['confidence']}%</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Trend (1h): {s['trend']}\n"
    msg += f"📉 RSI 15m: {s['rsi']:.1f}  |  RSI 30m: {s['rsi_30']:.1f}\n"
    msg += f"{sent_emoji} Sentiment: {s['sentiment']:+.1f}/10\n"
    if s.get("pattern"):
        msg += f"🕯 {s['pattern']}\n"
    msg += "\n<b>Why:</b>\n"
    for r in s.get("reasons", []):
        msg += f"  • {r}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<i>Not financial advice</i>"
    return msg

def format_summary(scan_results):
    msg = "<b>☀️ MORNING BRIEF — 8 AM WAT</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    for r in scan_results:
        trend_arrow = "↑" if r["trend"] == "Bullish" else ("↓" if r["trend"] == "Bearish" else "→")
        msg += f"<b>{r['pair']}</b> {trend_arrow} {r['trend']}\n"
        msg += f"  RSI {r['rsi']:.1f}  |  {r['price']:.5f}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "<i>Ifyfx Signals Bot</i>"
    return msg

# ===== MAIN =====
def main():
    global daily_signal_count, last_news_warn_time
    print("Ifyfx Signals Bot v4.1 starting...")
    send_telegram("🤖 <b>Ifyfx Signals Bot v4.1 ONLINE</b>\n\n✅ MTF 15m+30m+1h\n✅ Sentiment\n✅ Debug logging\n✅ Max 8 signals/day")

    while True:
        try:
            check_daily_reset()
            now = datetime.now(timezone.utc)
            wat = (now.hour + WAT_OFFSET) % 24
            print(f"\n[{wat:02d}:{now.minute:02d} WAT] Scanning... (today: {daily_signal_count}/{MAX_SIGNALS_PER_DAY})")

            news, mins = is_news_blackout()
            if news:
                print(f"  🔕 NEWS BLACKOUT: {news['event']}")
                time.sleep(SCAN_INTERVAL); continue

            warn, wm = upcoming_news_warning()
            if warn and (last_news_warn_time is None or (now - last_news_warn_time) > timedelta(hours=2)):
                msg = f"⚠️ <b>NEWS ALERT</b>\n\n📰 {warn['event']} ({warn['currency']})\n🕐 ~{wm:.0f} min\n\n<i>Signals paused 60 min around release</i>"
                send_telegram(msg)
                last_news_warn_time = now

            signals_found = []
            scan_data = []

            for pair in PAIRS:
                result = analyze(pair)
                if not result:
                    print(f"    {pair}: no result")
                elif "skip" in result:
                    print(f"    {pair}: skipped ({result['skip']})")
                else:
                    # Log every pair's status
                    conf = result.get("confidence", 0)
                    sig = result.get("signal")
                    if sig:
                        print(f"    {pair}: 🚨 {sig} (conf {conf}%)")
                        if not is_duplicate(pair, sig):
                            signals_found.append(result)
                        else:
                            print(f"       ↳ duplicate, skipped")
                    else:
                        print(f"    {pair}: no signal (conf {conf}%, RSI {result['rsi']:.1f}, {result['trend']})")
                    scan_data.append(result)
                time.sleep(2)

            if usd_correlated_recently():
                before = len(signals_found)
                signals_found = [s for s in signals_found if "XAU" in s["pair"] or "JPY" in s["pair"]]
                if before != len(signals_found):
                    print(f"  ⚠️ USD correlation filter removed {before - len(signals_found)} signal(s)")

            for s in signals_found:
                if daily_signal_count < MAX_SIGNALS_PER_DAY:
                    send_telegram(format_signal(s))
                    signal_history.append({
                        "pair": s["pair"], "signal": s["signal"], "time": now,
                        "entry": s["price"], "sl": s["sl"], "tp": s["tp"]
                    })
                    daily_signal_count += 1
                    print(f"  ✅ Sent signal #{daily_signal_count}")
                else:
                    print(f"  ⛔ Daily limit reached ({MAX_SIGNALS_PER_DAY})")

            if should_send_summary() and scan_data:
                send_telegram(format_summary(scan_data))
                print(f"  ☀️ Morning summary sent ({len(scan_data)} pairs)")

            for h in signal_history[:]:
                if h.get("checked"): continue
                if (now - h["time"]) > timedelta(hours=6): continue
                c = get_candles(h["pair"], "15min", 2)
                if c:
                    cur = float(c[-1]["close"])
                    hit = None
                    if h["signal"] == "BUY":
                        if cur >= h["tp"]: hit = "TP"
                        elif cur <= h["sl"]: hit = "SL"
                    else:
                        if cur <= h["tp"]: hit = "TP"
                        elif cur >= h["sl"]: hit = "SL"
                    if hit == "TP":
                        send_telegram(f"🎯 <b>TP HIT</b> — {h['pair']} {h['signal']}")
                        h["checked"] = True
                        print(f"  🎯 {h['pair']} TP HIT")
                    elif hit == "SL":
                        send_telegram(f"🛑 <b>SL HIT</b> — {h['pair']} {h['signal']}")
                        h["checked"] = True
                        print(f"  🛑 {h['pair']} SL HIT")
                    time.sleep(1)

            print(f"  ⏳ Sleeping {SCAN_INTERVAL//60} min...")
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
