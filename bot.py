import os
import time
import json
import requests
from datetime import datetime, timezone, timedelta
from collections import defaultdict

# ===== CONFIG =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "-1004375163609")
TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY", "944649b4a9d24074bbb41b2a954639cb")
FMP_KEY = os.getenv("FMP_KEY", "QmnG8t5DMUrIf8AWB7zHWcYa70kM4OCh")

PAIRS = ["EUR/USD", "USD/JPY", "GBP/USD", "AUD/USD", "USD/CAD", "XAU/USD"]
WAT_OFFSET = 1
SCAN_INTERVAL = 15 * 60
MAX_SIGNALS_PER_DAY = 4
CONFIDENCE_THRESHOLD = 75
DEDUP_HOURS = 4
WATCH_DEDUP_MIN = 60
SUMMARY_HOUR_WAT = 8
BIAS_HOUR_WAT = 7
SENTIMENT_CACHE_MIN = 120
SIGNAL_VALIDITY_MIN = 30
GRADE_A_PLUS = 85
GRADE_A = 75
JOURNAL_FILE = "signal_journal.json"
API_DELAY = 8
PREFLIGHT_DRIFT_ATR = 0.5

SESSIONS = {"tokyo": (0, 9), "london": (7, 16), "ny": (12, 21)}
PAIR_SESSIONS = {
    "EUR/USD": ["london", "ny"],
    "GBP/USD": ["london", "ny"],
    "USD/JPY": ["tokyo", "ny"],
    "AUD/USD": ["tokyo", "london"],
    "USD/CAD": ["ny"],
    "XAU/USD": ["london", "ny"],
}

BULLISH_WORDS = ["rise","rises","rally","rallying","surge","surges","gain","gains",
                 "strong","stronger","upbeat","bullish","boost","higher","climb",
                 "climbs","jump","jumps","soar","soars","upgrade","optimism","record high"]
BEARISH_WORDS = ["fall","falls","drop","drops","plunge","plunges","crash","crashes",
                 "weak","weaker","downbeat","bearish","slump","lower","slide","slides",
                 "sink","sinks","tumble","tumbles","downgrade","pessimism","record low"]

signal_history = []
daily_signal_count = 0
last_reset_date = None
last_summary_date = None
last_bias_date = None
last_news_warn_time = None
sentiment_cache = {}
watch_dedup = {}
journal = []

if not BOT_TOKEN:
    print("❌ BOT_TOKEN missing")
    exit(1)
print(f"✅ Config loaded. Bot ID: {BOT_TOKEN.split(':')[0]}")

def load_journal():
    global journal
    try:
        with open(JOURNAL_FILE, "r") as f:
            data = json.load(f)
            for d in data: d["time"] = datetime.fromisoformat(d["time"])
            journal = data
    except: journal = []

def save_journal():
    try:
        data = []
        for h in journal:
            d = dict(h); d["time"] = d["time"].isoformat(); data.append(d)
        with open(JOURNAL_FILE, "w") as f: json.dump(data, f)
    except: pass

def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=15)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"TG error: {e}")
        return False

def current_session():
    h = datetime.now(timezone.utc).hour
    return [n for n, (s, e) in SESSIONS.items() if s <= h < e]

def pair_in_session(pair):
    active = current_session()
    return any(s in active for s in PAIR_SESSIONS.get(pair, ["london", "ny"]))

def session_quality():
    h = datetime.now(timezone.utc).hour
    if 12 <= h < 17: return 15
    if 7 <= h < 12: return 10
    if 17 <= h < 21: return 5
    if 0 <= h < 7: return -10
    return -20

def is_weekend():
    return datetime.now(timezone.utc).weekday() >= 5

def is_active_session():
    return bool(current_session())

def get_candles(symbol, interval, outputsize=100):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVEDATA_KEY}
    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
        if "values" not in data: return None
        candles = list(reversed(data["values"]))
        now_utc = datetime.now(timezone.utc)
        im = {"15min": 15, "30min": 30, "1h": 60, "1day": 1440}.get(interval, 15)
        if candles:
            try:
                last_dt = datetime.fromisoformat(candles[-1]["datetime"]).replace(tzinfo=timezone.utc)
                if (now_utc - last_dt).total_seconds() < im * 60:
                    candles = candles[:-1]
            except: pass
        return candles
    except: return None

def get_current_price(symbol):
    c = get_candles(symbol, "1min", 2)
    if c: return float(c[-1]["close"])
    return None

def fetch_sentiment(currency):
    now = datetime.now(timezone.utc)
    if currency in sentiment_cache:
        score, ts = sentiment_cache[currency]
        if (now - ts) < timedelta(minutes=SENTIMENT_CACHE_MIN): return score
    url = f"https://financialmodelingprep.com/api/v3/news?apikey={FMP_KEY}&limit=50"
    try:
        r = requests.get(url, timeout=15)
        news = r.json()
        if not isinstance(news, list): return 0
    except: return 0
    score = 0; count = 0
    for item in news[:50]:
        title = (item.get("title") or "").lower()
        text = (item.get("text") or "").lower()[:500]
        combined = title + " " + text
        ck = {"USD": ["dollar","fed","federal reserve","usd","powell","treasury"],
              "EUR": ["euro","ecb","eur","lagarde"],
              "GBP": ["pound","boe","gbp","bank of england","sterling"],
              "JPY": ["yen","boj","jpy","bank of japan"],
              "AUD": ["aussie","rba","aud","australia"],
              "CAD": ["loonie","boc","cad","canada"],
              "XAU": ["gold","xau","bullion"]}
        keys = ck.get(currency, [currency.lower()])
        if not any(k in combined for k in keys): continue
        bull = sum(1 for w in BULLISH_WORDS if w in combined)
        bear = sum(1 for w in BEARISH_WORDS if w in combined)
        score += (bull - bear); count += 1
    if count > 0: score = score / max(count, 5) * 10
    score = max(-10, min(10, score))
    sentiment_cache[currency] = (score, now)
    return score

def get_pair_sentiment(pair):
    if "XAU" in pair: return fetch_sentiment("XAU")
    base, quote = pair.split("/")
    return (fetch_sentiment(base) - fetch_sentiment(quote)) / 2

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

def calc_adx(candles, period=14):
    if len(candles) < period * 2: return None
    trs, plus_dms, minus_dms = [], [], []
    for i in range(1, len(candles)):
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        prev_h = float(candles[i-1]["high"])
        prev_l = float(candles[i-1]["low"])
        prev_c = float(candles[i-1]["close"])
        
        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
        trs.append(tr)
        
        up_move = h - prev_h
        down_move = prev_l - l
        plus_dm = up_move if (up_move > down_move and up_move > 0) else 0
        minus_dm = down_move if (down_move > up_move and down_move > 0) else 0
        plus_dms.append(plus_dm)
        minus_dms.append(minus_dm)
    
    def smooth(arr, p):
        s = sum(arr[:p])
        smoothed = [s]
        for v in arr[p:]:
            s = s - (s/p) + v
            smoothed.append(s)
        return smoothed
    
    smoothed_tr = smooth(trs, period)
    smoothed_plus_dm = smooth(plus_dms, period)
    smoothed_minus_dm = smooth(minus_dms, period)
    
    plus_di = [100 * pd / tr if tr != 0 else 0 for pd, tr in zip(smoothed_plus_dm, smoothed_tr)]
    minus_di = [100 * md / tr if tr != 0 else 0 for md, tr in zip(smoothed_minus_dm, smoothed_tr)]
    
    dx = [100 * abs(pdi - mdi) / (pdi + mdi) if (pdi + mdi) != 0 else 0 for pdi, mdi in zip(plus_di, minus_di)]
    
    adx = smooth(dx, period)
    return adx[-1] if adx else None

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
    if body > rng*0.7:
        if cl > o: return "Bullish Engulfing"
        else: return "Bearish Engulfing"
    return None

def volume_spike(candles, lb=20):
    vols = [float(c.get("volume") or 0) for c in candles[-lb:]]
    if not vols or len(vols) < lb: return 1.0
    avg = sum(vols[:-1]) / (len(vols)-1) if len(vols) > 1 else 1
    if avg == 0: return 1.0
    return vols[-1] / avg

def check_daily_reset():
    global daily_signal_count, last_reset_date
    today = datetime.now(timezone.utc).date()
    if last_reset_date != today:
        daily_signal_count = 0; last_reset_date = today

def is_duplicate(pair, signal):
    now = datetime.now(timezone.utc)
    for h in signal_history:
        if h["pair"] == pair and h["signal"] == signal and (now - h["time"]) < timedelta(hours=DEDUP_HOURS):
            return True
    return False

def is_watch_duplicate(pair, watch_reason):
    now = datetime.now(timezone.utc)
    key = f"{pair}_{watch_reason[:30]}"
    if key in watch_dedup:
        if (now - watch_dedup[key]) < timedelta(minutes=WATCH_DEDUP_MIN): return True
    watch_dedup[key] = now
    return False

def grade_signal(conf):
    if conf >= GRADE_A_PLUS: return "A+"
    if conf >= GRADE_A: return "A"
    return "B"

def get_pair_win_rate(pair):
    records = [j for j in journal if j["pair"] == pair and j.get("result") in ("TP", "SL")]
    if not records: return None
    wins = sum(1 for r in records if r["result"] == "TP")
    return wins / len(records) * 100

def pre_flight_check(sig, live_price, atr):
    if not live_price or not atr: return True, "no live data"
    entry = sig["price"]; sl = sig["sl"]; tp = sig["tp"]
    if sig["signal"] == "BUY":
        if live_price <= sl: return False, f"below SL"
        if live_price >= tp: return False, f"above TP"
        drift = abs(live_price - entry)
        if drift > atr * PREFLIGHT_DRIFT_ATR: return False, f"drifted"
    else:
        if live_price >= sl: return False, f"above SL"
        if live_price <= tp: return False, f"below TP"
        drift = abs(live_price - entry)
        if drift > atr * PREFLIGHT_DRIFT_ATR: return False, f"drifted"
    return True, "OK"

def analyze(pair):
    if is_weekend() and pair != "XAU/USD": return {"pair": pair, "skip": "weekend"}
    if not is_active_session(): return {"pair": pair, "skip": "no_session"}
    if not pair_in_session(pair): return {"pair": pair, "skip": "wrong_session"}

    c15 = get_candles(pair, "15min", 100)
    time.sleep(API_DELAY)
    c30 = get_candles(pair, "30min", 100)
    time.sleep(API_DELAY)
    c1h = get_candles(pair, "1h", 100)
    time.sleep(API_DELAY)
    c1d = get_candles(pair, "1day", 50)
    time.sleep(API_DELAY)

    if not c15 or not c30 or not c1h or not c1d: return {"pair": pair, "skip": "no_candles"}
    if len(c15) < 50 or len(c30) < 50 or len(c1h) < 50 or len(c1d) < 20: return {"pair": pair, "skip": "not_enough_data"}

    closes_15 = [float(c["close"]) for c in c15]
    closes_30 = [float(c["close"]) for c in c30]
    closes_1h = [float(c["close"]) for c in c1h]
    closes_1d = [float(c["close"]) for c in c1d]

    rsi_15 = calc_rsi(closes_15, 14)
    atr_15 = calc_atr(c15, 14)
    adx_15 = calc_adx(c15, 14)
    price = closes_15[-1]
    resistance, support = find_zones(c15, 20)
    pattern = detect_pattern(c15[-1])
    
    rsi_30 = calc_rsi(closes_30, 14)
    ema20_30 = calc_ema(closes_30, 20)
    ema50_30 = calc_ema(closes_30, 50)
    
    ema20_1h = calc_ema(closes_1h, 20)
    ema50_1h = calc_ema(closes_1h, 50)
    
    ema20_1d = calc_ema(closes_1d, 20)
    ema50_1d = calc_ema(closes_1d, 50)
    vol_ratio = volume_spike(c15, 20)

    if None in (rsi_15, atr_15, adx_15, rsi_30, ema20_30, ema50_30, ema20_1h, ema50_1h, ema20_1d, ema50_1d):
        return {"pair": pair, "skip": "indicator_calc_failed"}

    # THE MACRO FILTER (DAILY TREND)
    trend_bull_1d = ema20_1d > ema50_1d
    trend_bear_1d = ema20_1d < ema50_1d

    trend_bull_1h = ema20_1h > ema50_1h and price > ema20_1h
    trend_bear_1h = ema20_1h < ema50_1h and price < ema20_1h
    trend_bull_30 = ema20_30 > ema50_30
    trend_bear_30 = ema20_30 < ema50_30

    dist_support = abs(price - support) / atr_15 if atr_15 else 999
    dist_resist = abs(price - resistance) / atr_15 if atr_15 else 999

    sentiment = get_pair_sentiment(pair)
    signal = None; confidence = 0; reasons = []; has_trigger = False
    breakdown = []
    watch = False; watch_reason = ""

    # ===== STRICT TREND + DAILY FILTER + ADX STRENGTH =====
    if trend_bull_1h:
        if trend_bear_1d: return {"pair": pair, "skip": "daily_trend_conflict"}
        
        # ADX FILTER: Must be >25 to confirm strong trend
        if adx_15 < 25: return {"pair": pair, "skip": "weak_trend_adx"}

        confidence += 30; reasons.append("1h bullish trend"); breakdown.append(("1h trend", 30))
        if trend_bull_1d: confidence += 15; reasons.append("Daily bullish"); breakdown.append(("Daily align", 15))
        if trend_bull_30: confidence += 15; reasons.append("30m confirms"); breakdown.append(("30m confirm", 15))
        if rsi_15 < 40: confidence += 25; reasons.append(f"RSI {rsi_15:.1f} OS"); has_trigger = True; breakdown.append(("RSI OS", 25))
        if dist_support < 1.5: confidence += 15; reasons.append("Near support"); breakdown.append(("Support", 15))
        if pattern in ("Bullish Pin Bar", "Bullish Engulfing"): confidence += 15; reasons.append(pattern); has_trigger = True; breakdown.append(("Pattern", 15))
        if vol_ratio > 1.3: confidence += 5; reasons.append(f"Vol {vol_ratio:.1f}x"); breakdown.append(("Volume", 5))
        sq = session_quality(); confidence += sq
        if sq != 0: breakdown.append((f"Session {sq:+d}", sq))
        if sentiment >= 3: confidence += 10; reasons.append(f"Sent +{sentiment:.1f}"); breakdown.append(("Sentiment", 10))
        confidence += 10; breakdown.append(("ADX > 25", 10))
        
        if confidence >= CONFIDENCE_THRESHOLD and sentiment > -5 and has_trigger:
            signal = "BUY"

    elif trend_bear_1h:
        if trend_bull_1d: return {"pair": pair, "skip": "daily_trend_conflict"}
        
        # ADX FILTER: Must be >25 to confirm strong trend
        if adx_15 < 25: return {"pair": pair, "skip": "weak_trend_adx"}

        confidence += 30; reasons.append("1h bearish trend"); breakdown.append(("1h trend", 30))
        if trend_bear_1d: confidence += 15; reasons.append("Daily bearish"); breakdown.append(("Daily align", 15))
        if trend_bear_30: confidence += 15; reasons.append("30m confirms"); breakdown.append(("30m confirm", 15))
        if rsi_15 > 60: confidence += 25; reasons.append(f"RSI {rsi_15:.1f} OB"); has_trigger = True; breakdown.append(("RSI OB", 25))
        if dist_resist < 1.5: confidence += 15; reasons.append("Near resistance"); breakdown.append(("Resistance", 15))
        if pattern in ("Bearish Pin Bar", "Bearish Engulfing"): confidence += 15; reasons.append(pattern); has_trigger = True; breakdown.append(("Pattern", 15))
        if vol_ratio > 1.3: confidence += 5; reasons.append(f"Vol {vol_ratio:.1f}x"); breakdown.append(("Volume", 5))
        sq = session_quality(); confidence += sq
        if sq != 0: breakdown.append((f"Session {sq:+d}", sq))
        if sentiment <= -3: confidence += 10; reasons.append(f"Sent {sentiment:.1f}"); breakdown.append(("Sentiment", 10))
        confidence += 10; breakdown.append(("ADX > 25", 10))
        
        if confidence >= CONFIDENCE_THRESHOLD and sentiment < 5 and has_trigger:
            signal = "SELL"

    result = {
        "pair": pair, "price": price, "rsi": rsi_15, "rsi_30": rsi_30,
        "trend": "Bullish" if trend_bull_1h else ("Bearish" if trend_bear_1h else "Neutral"),
        "daily_trend": "Bullish" if trend_bull_1d else ("Bearish" if trend_bear_1d else "Neutral"),
        "confidence": confidence, "sentiment": sentiment,
        "breakdown": breakdown, "atr": atr_15, "adx": adx_15
    }

    if signal:
        if signal == "BUY":
            sl = price - atr_15 * 1.2; tp = price + atr_15 * 2.0
        else:
            sl = price + atr_15 * 1.2; tp = price - atr_15 * 2.0
        result.update({
            "signal": signal, "sl": sl, "tp": tp,
            "reasons": reasons, "pattern": pattern,
            "grade": grade_signal(confidence),
            "valid_until": datetime.now(timezone.utc) + timedelta(minutes=SIGNAL_VALIDITY_MIN),
        })
    elif not signal and confidence >= 55:
        result["watch"] = True
        if trend_bull_1h: result["watch_reason"] = f"Waiting for RSI pullback (currently {rsi_15:.1f})"
        elif trend_bear_1h: result["watch_reason"] = f"Waiting for RSI rejection (currently {rsi_15:.1f})"

    return result

def format_signal(s):
    emoji = "🟢" if s["signal"] == "BUY" else "🔴"
    dec = 3 if "JPY" in s["pair"] else (2 if "XAU" in s["pair"] else 5)
    fmt = f".{dec}f"
    sent_emoji = "😊" if s["sentiment"] > 2 else ("😟" if s["sentiment"] < -2 else "😐")
    entry_low = s["price"] - s["atr"] * 0.15
    entry_high = s["price"] + s["atr"] * 0.15
    exp_wat = (s["valid_until"].hour + WAT_OFFSET) % 24
    exp_str = f"{exp_wat:02d}:{s['valid_until'].minute:02d} WAT"
    wr = get_pair_win_rate(s["pair"])
    wr_str = f" | WR {wr:.0f}%" if wr else ""
    msg = f"{emoji} <b>{s['signal']} — {s['pair']}</b> [TREND]\n"
    msg += f"<b>Grade: {s['grade']}</b> | Confidence: <b>{s['confidence']}%</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📍 Entry Zone:  {entry_low:{fmt}} – {entry_high:{fmt}}\n"
    msg += f"🎯 Mid Entry:   {s['price']:{fmt}}\n"
    msg += f"🛑 Stop Loss:   {s['sl']:{fmt}}\n"
    msg += f"🎯 Take Profit: {s['tp']:{fmt}}\n"
    msg += f"⏰ Valid until: {exp_str}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Trend (1h): {s['trend']} | Daily: {s['daily_trend']}\n"
    msg += f"📉 RSI: 15m {s['rsi']:.1f} | 30m {s['rsi_30']:.1f}\n"
    msg += f"📊 ADX: {s['adx']:.1f} (Strong Trend)\n"
    msg += f"{sent_emoji} Sentiment: {s['sentiment']:+.1f}/10\n"
    if s.get("pattern"): msg += f"🕯 {s['pattern']}\n"
    msg += "\n<b>Score breakdown:</b>\n"
    for name, pts in s.get("breakdown", []):
        emoji2 = "✅" if pts > 0 else "❌"
        msg += f"  {emoji2} {name}: {pts:+d}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"<i>Not financial advice{wr_str}</i>"
    return msg

def format_watch(w):
    dec = 3 if "JPY" in w["pair"] else (2 if "XAU" in w["pair"] else 5)
    if w["trend"] == "Bullish": target_rsi = 40; direction = "<"
    else: target_rsi = 60; direction = ">"
    msg = f"⏳ <b>WATCH — {w['pair']}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Trend: {w['trend']} (Daily: {w['daily_trend']})\n💵 Price: {w['price']:.{dec}f}\n"
    msg += f"📉 RSI 15m: {w['rsi']:.1f}\n🎯 Confidence: {w['confidence']}%\n"
    msg += f"📊 ADX: {w['adx']:.1f}\n"
    msg += f"🎯 Target RSI: {direction} {target_rsi}\n\n"
    msg += f"<i>{w['watch_reason']}</i>"
    return msg

def main():
    global daily_signal_count, last_news_warn_time
    load_journal()
    print("Ifyfx Signals Bot v9.2 starting...")
    send_telegram("🤖 <b>Ifyfx Signals Bot v9.2 ONLINE</b>\n\n"
                  "💪 <b>ADX TREND FILTER ADDED</b>\n"
                  "• Requires ADX > 25 (Strong Trend)\n"
                  "• Ignores weak, choppy markets\n"
                  "• Daily Macro Filter still active")

    while True:
        try:
            check_daily_reset()
            now = datetime.now(timezone.utc)
            wat = (now.hour + WAT_OFFSET) % 24
            sess = ", ".join(current_session()) or "closed"
            print(f"\n[{wat:02d}:{now.minute:02d} WAT] Scanning... session: {sess}")

            signals_found = []; watches_found = []

            for pair in PAIRS:
                result = analyze(pair)
                if not result: pass
                elif "skip" in result: print(f"    {pair}: skip ({result['skip']})")
                else:
                    conf = result.get("confidence", 0)
                    sig = result.get("signal")
                    if sig:
                        print(f"    {pair}: 🚨 {sig} [{result.get('grade')}] {conf}%")
                        if not is_duplicate(pair, sig): signals_found.append(result)
                    elif result.get("watch"):
                        if not is_watch_duplicate(pair, result.get("watch_reason", "")):
                            watches_found.append(result)

            for s in signals_found:
                if daily_signal_count < MAX_SIGNALS_PER_DAY:
                    live = get_current_price(s["pair"])
                    time.sleep(API_DELAY)
                    ok, reason = pre_flight_check(s, live, s.get("atr"))
                    if not ok:
                        print(f"  ⏭ {s['pair']} DISCARDED: {reason}")
                        continue
                    send_telegram(format_signal(s))
                    record = {"pair": s["pair"], "signal": s["signal"], "time": now,
                              "entry": s["price"], "sl": s["sl"], "tp": s["tp"],
                              "grade": s["grade"], "confidence": s["confidence"],
                              "signal_type": "TREND", "result": None, "checked": False, "expired": False}
                    journal.append(record); signal_history.append(record); save_journal()
                    daily_signal_count += 1
                    print(f"  ✅ Sent #{daily_signal_count}")

            for w in watches_found[:2]: send_telegram(format_watch(w))

            for h in journal:
                if h.get("checked"): continue
                if (now - h["time"]) > timedelta(hours=24): h["checked"] = True; continue
                cur = get_current_price(h["pair"])
                if cur:
                    hit = None
                    if h["signal"] == "BUY":
                        if cur >= h["tp"]: hit = "TP"
                        elif cur <= h["sl"]: hit = "SL"
                    else:
                        if cur <= h["tp"]: hit = "TP"
                        elif cur >= h["sl"]: hit = "SL"
                    if hit == "TP":
                        send_telegram(f"🎯 <b>TP HIT</b> — {h['pair']} {h['signal']}\n✅ Win")
                        h["result"] = "TP"; h["checked"] = True; save_journal()
                    elif hit == "SL":
                        send_telegram(f"🛑 <b>SL HIT</b> — {h['pair']} {h['signal']}\n❌ Loss")
                        h["result"] = "SL"; h["checked"] = True; save_journal()
                    time.sleep(API_DELAY)

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
