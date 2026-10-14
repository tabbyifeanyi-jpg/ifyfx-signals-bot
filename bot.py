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
MAX_SIGNALS_PER_DAY = 8
CONFIDENCE_THRESHOLD = 70
CONFIDENCE_REVERSAL = 65
CONFIDENCE_MOMENTUM = 68
DEDUP_HOURS = 4
WATCH_DEDUP_MIN = 60
SUMMARY_HOUR_WAT = 8
BIAS_HOUR_WAT = 7
SENTIMENT_CACHE_MIN = 120
SIGNAL_VALIDITY_MIN = 30
GRADE_A_PLUS = 85
GRADE_A = 75
GRADE_B = 65
JOURNAL_FILE = "signal_journal.json"
API_DELAY = 3

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
last_weekly_report_date = None
last_session_shift = None
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

def is_prime_time():
    h = (datetime.now(timezone.utc).hour + WAT_OFFSET) % 24
    return 13 <= h < 17

def session_quality():
    """Returns confidence bonus/penalty based on current session."""
    h = datetime.now(timezone.utc).hour
    if 12 <= h < 17: return 15  # London-NY overlap
    if 7 <= h < 12: return 10   # London
    if 17 <= h < 21: return 5   # NY
    if 0 <= h < 7: return -10   # Tokyo/off-hours
    return -20  # Dead hours

def is_weekend():
    return datetime.now(timezone.utc).weekday() >= 5

def is_active_session():
    return bool(current_session())

def previous_session():
    global last_session_shift
    h = datetime.now(timezone.utc).hour
    today = datetime.now(timezone.utc).date()
    key = None
    if h == 12: key = "london → ny"
    elif h == 7: key = "tokyo → london"
    elif h == 16: key = "london → ny"
    elif h == 21: key = "ny → closed"
    if not key: return None
    shift_id = f"{today}_{key}"
    if last_session_shift == shift_id: return None
    last_session_shift = shift_id
    return key

def get_candles(symbol, interval, outputsize=100):
    url = "https://api.twelvedata.com/time_series"
    params = {"symbol": symbol, "interval": interval, "outputsize": outputsize, "apikey": TWELVEDATA_KEY}
    try:
        r = requests.get(url, params=params, timeout=20)
        data = r.json()
        if "values" not in data: return None
        candles = list(reversed(data["values"]))
        now_utc = datetime.now(timezone.utc)
        im = {"15min": 15, "30min": 30, "1h": 60, "4h": 240, "1day": 1440}.get(interval, 15)
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

def get_news_events():
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    url = f"https://financialmodelingprep.com/api/v3/economic_calendar?from={today}&to={today}&apikey={FMP_KEY}"
    try:
        r = requests.get(url, timeout=20)
        events = r.json()
        if not isinstance(events, list): return []
        return [{"event": e.get("event",""), "currency": e.get("currency",""), "date": e.get("date","")}
                for e in events if (e.get("impact") or "").lower() == "high"]
    except: return []

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

def atr_percentile(candles, lookback=100):
    if len(candles) < lookback: return 50
    series = []
    for i in range(14, len(candles)):
        a = calc_atr(candles[:i], 14)
        if a: series.append(a)
    if not series: return 50
    cur = series[-1]
    return sum(1 for a in series if a <= cur) / len(series) * 100

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

def fib_levels(candles, lb=50):
    highs = [float(c["high"]) for c in candles[-lb:]]
    lows = [float(c["low"]) for c in candles[-lb:]]
    high, low = max(highs), min(lows)
    diff = high - low
    return {"0": high, "23.6": high - diff*0.236, "38.2": high - diff*0.382,
            "50": high - diff*0.5, "61.8": high - diff*0.618, "100": low}

def volume_spike(candles, lb=20):
    vols = [float(c.get("volume") or 0) for c in candles[-lb:]]
    if not vols or len(vols) < lb: return 1.0
    avg = sum(vols[:-1]) / (len(vols)-1) if len(vols) > 1 else 1
    if avg == 0: return 1.0
    return vols[-1] / avg

def get_sl_tp_multipliers(atr_pct, trending):
    if atr_pct < 30: sl, tp = 1.0, 1.8
    elif atr_pct > 75: sl, tp = 1.8, 2.5
    else: sl, tp = 1.2, 2.0
    if trending: tp += 0.5
    return sl, tp

# ===== NEW: FVG DETECTION =====
def detect_fvg(candles, lb=30):
    """Detects Fair Value Gaps in recent candles. Returns list of (type, top, bottom)."""
    fvgs = []
    for i in range(len(candles)-lb, len(candles)-2):
        if i < 1: continue
        c1 = candles[i-1]; c2 = candles[i]; c3 = candles[i+1]
        # Bullish FVG: c1 high < c3 low (gap up)
        if float(c1["high"]) < float(c3["low"]):
            fvgs.append(("BULL", float(c3["low"]), float(c1["high"])))
        # Bearish FVG: c1 low > c3 high (gap down)
        if float(c1["low"]) > float(c3["high"]):
            fvgs.append(("BEAR", float(c1["low"]), float(c3["high"])))
    # Keep only recent 5
    return fvgs[-5:]

def price_in_fvg(price, fvgs, atr):
    """Check if current price is inside or near any FVG."""
    for typ, top, bottom in fvgs:
        # Within 0.3x ATR of the FVG
        if bottom - atr*0.3 <= price <= top + atr*0.3:
            return typ, top, bottom
    return None, None, None

# ===== NEW: MOMENTUM BREAKOUT =====
def detect_momentum(candles, atr, lb=3):
    """Detects 3 consecutive large candles in same direction."""
    if len(candles) < lb + 1: return None
    last = candles[-lb:]
    bodies = []
    for c in last:
        o = float(c["open"]); cl = float(c["close"])
        bodies.append(cl - o)
    # All bullish and large
    if all(b > atr*0.6 for b in bodies):
        return "BULLISH"
    if all(b < -atr*0.6 for b in bodies):
        return "BEARISH"
    return None

# ===== NEW: LIQUIDITY SWEEP =====
def detect_liquidity_sweep(candles, lb=20):
    """Detects wick above/below recent high/low followed by reversal."""
    if len(candles) < lb + 3: return None
    recent_highs = [float(c["high"]) for c in candles[-lb-2:-2]]
    recent_lows = [float(c["low"]) for c in candles[-lb-2:-2]]
    swing_high = max(recent_highs)
    swing_low = min(recent_lows)
    for i in range(-2, 0):
        c = candles[i]
        h = float(c["high"]); l = float(c["low"]); cl = float(c["close"])
        if h > swing_high and cl < swing_high:
            if i+1 < len(candles):
                nxt = candles[i+1]
                if float(nxt["close"]) < float(nxt["open"]):
                    return "HIGH_SWEEP"
        if l < swing_low and cl > swing_low:
            if i+1 < len(candles):
                nxt = candles[i+1]
                if float(nxt["close"]) > float(nxt["open"]):
                    return "LOW_SWEEP"
    return None

# ===== NEW: MULTI-CANDLE CONFIRMATION =====
def multi_candle_confirm(candles, direction):
    """Requires last 2 candles to confirm direction."""
    if len(candles) < 2: return False
    c1 = candles[-2]; c2 = candles[-1]
    if direction == "BULL":
        return float(c2["close"]) > float(c2["open"]) and float(c2["close"]) > float(c1["high"])
    else:
        return float(c2["close"]) < float(c2["open"]) and float(c2["close"]) < float(c1["low"])

# ===== STATE =====
def check_daily_reset():
    global daily_signal_count, last_reset_date
    today = datetime.now(timezone.utc).date()
    if last_reset_date != today:
        daily_signal_count = 0; last_reset_date = today

def should_send_summary():
    global last_summary_date
    now = datetime.now(timezone.utc)
    if (now.hour + WAT_OFFSET) % 24 == SUMMARY_HOUR_WAT and last_summary_date != now.date():
        last_summary_date = now.date(); return True
    return False

def should_send_bias():
    global last_bias_date
    now = datetime.now(timezone.utc)
    if (now.hour + WAT_OFFSET) % 24 == BIAS_HOUR_WAT and last_bias_date != now.date():
        last_bias_date = now.date(); return True
    return False

def should_send_weekly_report():
    global last_weekly_report_date
    now = datetime.now(timezone.utc)
    if now.weekday() == 6 and (now.hour + WAT_OFFSET) % 24 == 20 and last_weekly_report_date != now.date():
        last_weekly_report_date = now.date(); return True
    return False

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
        if (now - watch_dedup[key]) < timedelta(minutes=WATCH_DEDUP_MIN):
            return True
    watch_dedup[key] = now
    return False

def usd_correlated_recently():
    now = datetime.now(timezone.utc)
    usd_pairs = ["EUR/USD","GBP/USD","AUD/USD","USD/CAD","USD/JPY"]
    count = sum(1 for h in signal_history if h["pair"] in usd_pairs and (now - h["time"]) < timedelta(minutes=30))
    return count >= 2

def grade_signal(conf):
    if conf >= GRADE_A_PLUS: return "A+"
    if conf >= GRADE_A: return "A"
    if conf >= GRADE_B: return "B"
    return "C"

def get_pair_win_rate(pair):
    records = [j for j in journal if j["pair"] == pair and j.get("result") in ("TP", "SL")]
    if not records: return None
    wins = sum(1 for r in records if r["result"] == "TP")
    return wins / len(records) * 100

# ===== ANALYZE =====
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
    c4h = get_candles(pair, "4h", 50)
    time.sleep(API_DELAY)
    c1d = get_candles(pair, "1day", 30)
    time.sleep(API_DELAY)
    
    if not c15 or not c30 or not c1h: return {"pair": pair, "skip": "no_candles"}
    if len(c15) < 50 or len(c30) < 50 or len(c1h) < 50: return {"pair": pair, "skip": "not_enough_data"}

    closes_15 = [float(c["close"]) for c in c15]
    closes_30 = [float(c["close"]) for c in c30]
    closes_1h = [float(c["close"]) for c in c1h]

    rsi_15 = calc_rsi(closes_15, 14)
    rsi_1h = calc_rsi(closes_1h, 14)
    atr_15 = calc_atr(c15, 14)
    atr_pct = atr_percentile(c15, 100)
    price = closes_15[-1]
    resistance, support = find_zones(c15, 20)
    if c4h and len(c4h) >= 20: h4_res, h4_sup = find_zones(c4h, 20)
    else: h4_res, h4_sup = resistance, support
    if c1d and len(c1d) >= 10: d_res, d_sup = find_zones(c1d, 10)
    else: d_res, d_sup = resistance, support
    pattern = detect_pattern(c15[-1])
    rsi_30 = calc_rsi(closes_30, 14)
    ema20_30 = calc_ema(closes_30, 20)
    ema50_30 = calc_ema(closes_30, 50)
    ema20_1h = calc_ema(closes_1h, 20)
    ema50_1h = calc_ema(closes_1h, 50)
    vol_ratio = volume_spike(c15, 20)
    fibs = fib_levels(c1h, 50)
    fvgs = detect_fvg(c15, 30)
    fvg_type, fvg_top, fvg_bottom = price_in_fvg(price, fvgs, atr_15)
    momentum = detect_momentum(c15, atr_15, 3)
    sweep = detect_liquidity_sweep(c15, 20)

    if None in (rsi_15, atr_15, rsi_30, ema20_30, ema50_30, ema20_1h, ema50_1h):
        return {"pair": pair, "skip": "indicator_calc_failed"}

    trend_bull_1h = ema20_1h > ema50_1h and price > ema20_1h
    trend_bear_1h = ema20_1h < ema50_1h and price < ema20_1h
    trend_bull_30 = ema20_30 > ema50_30
    trend_bear_30 = ema20_30 < ema50_30

    dist_support = abs(price - support) / atr_15 if atr_15 else 999
    dist_resist = abs(price - resistance) / atr_15 if atr_15 else 999
    dist_h4_sup = abs(price - h4_sup) / atr_15 if atr_15 else 999
    dist_h4_res = abs(price - h4_res) / atr_15 if atr_15 else 999
    dist_d_sup = abs(price - d_sup) / atr_15 if atr_15 else 999
    dist_d_res = abs(price - d_res) / atr_15 if atr_15 else 999

    sentiment = get_pair_sentiment(pair)
    signal = None; confidence = 0; reasons = []; has_trigger = False
    breakdown = []
    watch = False; watch_reason = ""
    signal_type = "TREND"

    # ===== TREND CONTINUATION =====
    if trend_bull_1h:
        confidence += 30; reasons.append("1h bullish trend"); breakdown.append(("1h trend", 30))
        if trend_bull_30: confidence += 15; reasons.append("30m confirms"); breakdown.append(("30m confirm", 15))
        if rsi_15 < 40: confidence += 20; reasons.append(f"RSI {rsi_15:.1f} OS"); has_trigger = True; breakdown.append(("RSI OS", 20))
        if dist_support < 1.5: confidence += 15; reasons.append("Near support"); breakdown.append(("Support", 15))
        if dist_h4_sup < 2.0: confidence += 10; reasons.append("Near 4h support"); breakdown.append(("4h support", 10))
        if pattern in ("Bullish Pin Bar", "Bullish Engulfing"): confidence += 10; reasons.append(pattern); has_trigger = True; breakdown.append(("Pattern", 10))
        if vol_ratio > 1.5: confidence += 8; reasons.append(f"Vol {vol_ratio:.1f}x"); breakdown.append(("Volume", 8))
        sq = session_quality()
        confidence += sq
        if sq != 0: breakdown.append((f"Session {sq:+d}", sq))
        if sentiment >= 3: confidence += 15; reasons.append(f"Sent +{sentiment:.1f}"); breakdown.append(("Sentiment", 15))
        if confidence >= CONFIDENCE_THRESHOLD and sentiment > -5 and has_trigger: signal = "BUY"

    elif trend_bear_1h:
        confidence += 30; reasons.append("1h bearish trend"); breakdown.append(("1h trend", 30))
        if trend_bear_30: confidence += 15; reasons.append("30m confirms"); breakdown.append(("30m confirm", 15))
        if rsi_15 > 60: confidence += 20; reasons.append(f"RSI {rsi_15:.1f} OB"); has_trigger = True; breakdown.append(("RSI OB", 20))
        if dist_resist < 1.5: confidence += 15; reasons.append("Near resistance"); breakdown.append(("Resistance", 15))
        if dist_h4_res < 2.0: confidence += 10; reasons.append("Near 4h resistance"); breakdown.append(("4h resist", 10))
        if pattern in ("Bearish Pin Bar", "Bearish Engulfing"): confidence += 10; reasons.append(pattern); has_trigger = True; breakdown.append(("Pattern", 10))
        if vol_ratio > 1.5: confidence += 8; reasons.append(f"Vol {vol_ratio:.1f}x"); breakdown.append(("Volume", 8))
        sq = session_quality()
        confidence += sq
        if sq != 0: breakdown.append((f"Session {sq:+d}", sq))
        if sentiment <= -3: confidence += 15; reasons.append(f"Sent {sentiment:.1f}"); breakdown.append(("Sentiment", 15))
        if confidence >= CONFIDENCE_THRESHOLD and sentiment < 5 and has_trigger: signal = "SELL"

    # ===== FVG SETUP =====
    if not signal and fvg_type:
        fvg_conf = 0; fvg_reasons = []; fvg_breakdown = []
        fvg_conf += 25; fvg_reasons.append(f"Price in {fvg_type} FVG"); fvg_breakdown.append((f"FVG {fvg_type}", 25))
        if fvg_type == "BULL" and trend_bull_1h:
            fvg_conf += 20; fvg_reasons.append("Aligned with 1h bullish"); fvg_breakdown.append(("Trend align", 20))
        elif fvg_type == "BEAR" and trend_bear_1h:
            fvg_conf += 20; fvg_reasons.append("Aligned with 1h bearish"); fvg_breakdown.append(("Trend align", 20))
        else:
            fvg_conf -= 10; fvg_reasons.append("Against 1h trend"); fvg_breakdown.append(("Trend conflict", -10))
        if vol_ratio > 1.3: fvg_conf += 10; fvg_reasons.append(f"Vol {vol_ratio:.1f}x"); fvg_breakdown.append(("Volume", 10))
        if pattern and pattern.startswith("Bullish") and fvg_type == "BULL":
            fvg_conf += 15; fvg_reasons.append(pattern); fvg_breakdown.append(("Pattern", 15))
        if pattern and pattern.startswith("Bearish") and fvg_type == "BEAR":
            fvg_conf += 15; fvg_reasons.append(pattern); fvg_breakdown.append(("Pattern", 15))
        sq = session_quality()
        fvg_conf += sq; fvg_breakdown.append((f"Session {sq:+d}", sq))
        if fvg_conf >= CONFIDENCE_REVERSAL:
            signal = "BUY" if fvg_type == "BULL" else "SELL"
            confidence = fvg_conf; reasons = fvg_reasons; breakdown = fvg_breakdown
            signal_type = "FVG"; has_trigger = True

    # ===== MOMENTUM BREAKOUT =====
    if not signal and momentum:
        m_conf = 0; m_reasons = []; m_breakdown = []
        m_conf += 25; m_reasons.append(f"3 large {momentum} candles"); m_breakdown.append(("Momentum", 25))
        if (momentum == "BULLISH" and trend_bull_1h) or (momentum == "BEARISH" and trend_bear_1h):
            m_conf += 20; m_reasons.append("Aligned with 1h trend"); m_breakdown.append(("Trend align", 20))
        else:
            m_conf -= 15; m_reasons.append("Fighting 1h trend"); m_breakdown.append(("Trend conflict", -15))
        if vol_ratio > 1.3: m_conf += 10; m_reasons.append(f"Vol {vol_ratio:.1f}x"); m_breakdown.append(("Volume", 10))
        if momentum == "BULLISH" and (pattern in ("Bullish Pin Bar", "Bullish Engulfing", "Doji")):
            m_conf += 10; m_reasons.append("Pullback candle"); m_breakdown.append(("Pullback", 10))
        if momentum == "BEARISH" and (pattern in ("Bearish Pin Bar", "Bearish Engulfing", "Doji")):
            m_conf += 10; m_reasons.append("Pullback candle"); m_breakdown.append(("Pullback", 10))
        sq = session_quality()
        m_conf += sq; m_breakdown.append((f"Session {sq:+d}", sq))
        if m_conf >= CONFIDENCE_MOMENTUM:
            signal = "BUY" if momentum == "BULLISH" else "SELL"
            confidence = m_conf; reasons = m_reasons; breakdown = m_breakdown
            signal_type = "MOMENTUM"; has_trigger = True

    # ===== LIQUIDITY SWEEP =====
    if not signal and sweep:
        s_conf = 0; s_reasons = []; s_breakdown = []
        s_conf += 25; s_reasons.append(f"Liquidity sweep ({sweep})"); s_breakdown.append(("Sweep", 25))
        if sweep == "LOW_SWEEP" and (pattern in ("Bullish Pin Bar", "Bullish Engulfing")):
            s_conf += 20; s_reasons.append(pattern); s_breakdown.append(("Pattern", 20))
        if sweep == "HIGH_SWEEP" and (pattern in ("Bearish Pin Bar", "Bearish Engulfing")):
            s_conf += 20; s_reasons.append(pattern); s_breakdown.append(("Pattern", 20))
        if sweep == "LOW_SWEEP" and dist_h4_sup < 1.5:
            s_conf += 15; s_reasons.append("At 4h support"); s_breakdown.append(("4h zone", 15))
        if sweep == "HIGH_SWEEP" and dist_h4_res < 1.5:
            s_conf += 15; s_reasons.append("At 4h resistance"); s_breakdown.append(("4h zone", 15))
        if vol_ratio > 1.3: s_conf += 10; s_reasons.append(f"Vol {vol_ratio:.1f}x"); s_breakdown.append(("Volume", 10))
        sq = session_quality()
        s_conf += sq; s_breakdown.append((f"Session {sq:+d}", sq))
        if s_conf >= CONFIDENCE_REVERSAL:
            signal = "BUY" if sweep == "LOW_SWEEP" else "SELL"
            confidence = s_conf; reasons = s_reasons; breakdown = s_breakdown
            signal_type = "SWEEP"; has_trigger = True

    result = {
        "pair": pair, "price": price, "rsi": rsi_15, "rsi_30": rsi_30, "rsi_1h": rsi_1h,
        "trend": "Bullish" if trend_bull_1h else ("Bearish" if trend_bear_1h else "Neutral"),
        "confidence": confidence, "sentiment": sentiment, "atr_pct": atr_pct,
        "breakdown": breakdown, "vol_ratio": vol_ratio, "fibs": fibs,
        "signal_type": signal_type,
    }

    if rsi_15 and rsi_1h:
        if rsi_15 < 50 and rsi_1h > 70:
            result["divergence"] = "15m normal but 1h overbought — careful with longs"

    if signal:
        sl_mult, tp_mult = get_sl_tp_multipliers(atr_pct, trend_bull_1h or trend_bear_1h)
        if signal_type in ("FVG", "SWEEP"):
            sl_mult = 1.2; tp_mult = 1.8
        if signal == "BUY":
            sl = price - atr_15 * sl_mult; tp = price + atr_15 * tp_mult
        else:
            sl = price + atr_15 * sl_mult; tp = price - atr_15 * tp_mult
        result.update({
            "signal": signal, "sl": sl, "tp": tp, "atr": atr_15,
            "reasons": reasons, "pattern": pattern, "prime": is_prime_time(),
            "grade": grade_signal(confidence),
            "valid_until": datetime.now(timezone.utc) + timedelta(minutes=SIGNAL_VALIDITY_MIN),
            "sl_mult": sl_mult, "tp_mult": tp_mult,
        })
    elif not signal and confidence >= 55 and confidence < CONFIDENCE_THRESHOLD:
        result["watch"] = True
        if trend_bull_1h: result["watch_reason"] = f"Waiting for RSI pullback (currently {rsi_15:.1f})"
        elif trend_bear_1h: result["watch_reason"] = f"Waiting for RSI rejection (currently {rsi_15:.1f})"

    return result

def format_signal(s):
    is_special = s.get("signal_type", "TREND") != "TREND"
    type_label = s.get("signal_type", "TREND")
    emoji = "🟢" if s["signal"] == "BUY" else "🔴"
    if type_label == "FVG": emoji = "📐🟢" if s["signal"] == "BUY" else "📐🔴"
    elif type_label == "MOMENTUM": emoji = "⚡🟢" if s["signal"] == "BUY" else "⚡🔴"
    elif type_label == "SWEEP": emoji = "💧🟢" if s["signal"] == "BUY" else "💧🔴"
    dec = 3 if "JPY" in s["pair"] else (2 if "XAU" in s["pair"] else 5)
    fmt = f".{dec}f"
    prime_tag = " ⭐" if s.get("prime") else ""
    sent_emoji = "😊" if s["sentiment"] > 2 else ("😟" if s["sentiment"] < -2 else "😐")
    entry_low = s["price"] - s["atr"] * 0.15
    entry_high = s["price"] + s["atr"] * 0.15
    exp_wat = (s["valid_until"].hour + WAT_OFFSET) % 24
    exp_str = f"{exp_wat:02d}:{s['valid_until'].minute:02d} WAT"
    wr = get_pair_win_rate(s["pair"])
    wr_str = f" | WR {wr:.0f}%" if wr else ""
    msg = f"{emoji} <b>{s['signal']} — {s['pair']}</b> [{type_label}]{prime_tag}\n"
    msg += f"<b>Grade: {s['grade']}</b> | Confidence: <b>{s['confidence']}%</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📍 Entry Zone:  {entry_low:{fmt}} – {entry_high:{fmt}}\n"
    msg += f"🎯 Mid Entry:   {s['price']:{fmt}}\n"
    msg += f"🛑 Stop Loss:   {s['sl']:{fmt}}\n"
    msg += f"🎯 Take Profit: {s['tp']:{fmt}}\n"
    msg += f"⏰ Valid until: {exp_str}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Trend (1h): {s['trend']}\n"
    r1 = f" | 1h: {s['rsi_1h']:.1f}" if s.get("rsi_1h") else ""
    msg += f"📉 RSI: 15m {s['rsi']:.1f} | 30m {s['rsi_30']:.1f}{r1}\n"
    msg += f"{sent_emoji} Sentiment: {s['sentiment']:+.1f}/10\n"
    if s.get("pattern"): msg += f"🕯 {s['pattern']}\n"
    msg += "\n<b>Score breakdown:</b>\n"
    for name, pts in s.get("breakdown", []):
        emoji2 = "✅" if pts > 0 else "❌"
        msg += f"  {emoji2} {name}: {pts:+d}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    if s["grade"] in ("A", "A+"): msg += "✅ <b>Tradeable</b>\n"
    elif s["grade"] == "B": msg += "⚠️ <b>B-grade — observe</b>\n"
    else: msg += "❌ <b>Do not trade</b>\n"
    msg += f"<i>Not financial advice{wr_str}</i>"
    return msg

def format_watch(w):
    dec = 3 if "JPY" in w["pair"] else (2 if "XAU" in w["pair"] else 5)
    target_rsi = 40 if w["trend"] == "Bullish" else 60
    msg = f"⏳ <b>WATCH — {w['pair']}</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Trend: {w['trend']}\n💵 Price: {w['price']:.{dec}f}\n"
    msg += f"📉 RSI 15m: {w['rsi']:.1f}\n🎯 Confidence: {w['confidence']}%\n"
    msg += f"🎯 Target RSI: {'<' if w['trend']=='Bullish' else '>'} {target_rsi}\n\n"
    msg += f"<i>{w['watch_reason']}</i>"
    return msg

def format_summary(scan_results):
    msg = "<b>☀️ MORNING BRIEF — 8 AM WAT</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for r in scan_results:
        ta = "↑" if r["trend"] == "Bullish" else ("↓" if r["trend"] == "Bearish" else "→")
        msg += f"<b>{r['pair']}</b> {ta} {r['trend']}\n  RSI {r['rsi']:.1f}  |  {r['price']:.5f}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n<i>Ifyfx Signals Bot</i>"
    return msg

def format_bias(scan_results):
    msg = "<b>🎯 DAILY BIAS — 7 AM WAT</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    for r in scan_results:
        ta = "↑" if r["trend"] == "Bullish" else ("↓" if r["trend"] == "Bearish" else "→")
        msg += f"<b>{r['pair']}</b> {ta} {r['trend']}  (RSI {r['rsi']:.1f})\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n<i>Trade with the bias, not against.</i>"
    return msg

def format_weekly_report():
    total = len(journal)
    if total == 0: return "<b>📊 WEEKLY REPORT</b>\n\nNo signals yet."
    tp = sum(1 for h in journal if h.get("result") == "TP")
    sl = sum(1 for h in journal if h.get("result") == "SL")
    open_ = sum(1 for h in journal if h.get("result") is None)
    wr = (tp / (tp + sl) * 100) if (tp + sl) > 0 else 0
    ps = defaultdict(lambda: {"tp": 0, "sl": 0})
    ts = defaultdict(lambda: {"tp": 0, "sl": 0})
    for h in journal:
        t = h.get("signal_type", "TREND")
        if h.get("result") == "TP":
            ps[h["pair"]]["tp"] += 1; ts[t]["tp"] += 1
        elif h.get("result") == "SL":
            ps[h["pair"]]["sl"] += 1; ts[t]["sl"] += 1
    msg = "<b>📊 WEEKLY PERFORMANCE</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Signals: <b>{total}</b>\n🎯 TP: <b>{tp}</b>\n🛑 SL: <b>{sl}</b>\n⏳ Open: <b>{open_}</b>\n🏆 Win rate: <b>{wr:.1f}%</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n<b>By pair:</b>\n"
    for p, s in ps.items(): msg += f"  {p}: {s['tp']}W / {s['sl']}L\n"
    msg += "\n<b>By type:</b>\n"
    for t, s in ts.items(): msg += f"  {t}: {s['tp']}W / {s['sl']}L\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n<i>Ifyfx Signals Bot</i>"
    return msg

def main():
    global daily_signal_count, last_news_warn_time
    load_journal()
    print("Ifyfx Signals Bot v8 starting...")
    send_telegram("🤖 <b>Ifyfx Signals Bot v8 ONLINE</b>\n\n"
                  "🆕 <b>7 NEW SIGNAL TYPES:</b>\n"
                  "📐 FVG (Fair Value Gap)\n"
                  "⚡ Momentum Breakout\n"
                  "💧 Liquidity Sweep\n"
                  "✅ Multi-candle confirmation\n"
                  "🏔 HTF alignment\n"
                  "📊 Volume filter\n"
                  "🎯 Session quality scoring")

    while True:
        try:
            check_daily_reset()
            now = datetime.now(timezone.utc)
            wat = (now.hour + WAT_OFFSET) % 24
            sess = ", ".join(current_session()) or "closed"
            print(f"\n[{wat:02d}:{now.minute:02d} WAT] Scanning... session: {sess}")

            ps = previous_session()
            if ps:
                send_telegram(f"🔄 <b>SESSION SHIFT</b>\n{ps}\n<i>Volatility expected to change</i>")

            news, mins = is_news_blackout()
            if news:
                print(f"  🔕 NEWS BLACKOUT: {news['event']}")
                time.sleep(SCAN_INTERVAL); continue

            warn, wm = upcoming_news_warning()
            if warn and (last_news_warn_time is None or (now - last_news_warn_time) > timedelta(hours=2)):
                send_telegram(f"⚠️ <b>NEWS ALERT</b>\n\n📰 {warn['event']} ({warn['currency']})\n🕐 ~{wm:.0f} min")
                last_news_warn_time = now

            signals_found = []; watches_found = []; scan_data = []

            for pair in PAIRS:
                result = analyze(pair)
                if not result: pass
                elif "skip" in result: print(f"    {pair}: skip ({result['skip']})")
                else:
                    conf = result.get("confidence", 0)
                    sig = result.get("signal")
                    if sig:
                        print(f"    {pair}: 🚨 {sig} [{result.get('signal_type','?')}] ({result.get('grade')}) {conf}%")
                        if not is_duplicate(pair, sig): signals_found.append(result)
                    elif result.get("watch"):
                        if not is_watch_duplicate(pair, result.get("watch_reason", "")):
                            watches_found.append(result)
                    scan_data.append(result)
                time.sleep(API_DELAY)

            if usd_correlated_recently():
                signals_found = [s for s in signals_found if "XAU" in s["pair"] or "JPY" in s["pair"]]

            for s in signals_found:
                if daily_signal_count < MAX_SIGNALS_PER_DAY:
                    send_telegram(format_signal(s))
                    record = {"pair": s["pair"], "signal": s["signal"], "time": now,
                              "entry": s["price"], "sl": s["sl"], "tp": s["tp"],
                              "grade": s["grade"], "confidence": s["confidence"],
                              "signal_type": s.get("signal_type", "TREND"),
                              "result": None, "checked": False, "expired": False,
                              "update_sent": False}
                    journal.append(record); signal_history.append(record); save_journal()
                    daily_signal_count += 1
                    print(f"  ✅ Sent #{daily_signal_count}")

            for w in watches_found[:2]: send_telegram(format_watch(w))

            if should_send_bias() and scan_data: send_telegram(format_bias(scan_data))
            if should_send_summary() and scan_data: send_telegram(format_summary(scan_data))
            if should_send_weekly_report(): send_telegram(format_weekly_report())

            for h in journal:
                if h.get("checked"): continue
                if (now - h["time"]) > timedelta(hours=24): h["checked"] = True; continue
                if not h.get("expired") and (now - h["time"]) > timedelta(minutes=SIGNAL_VALIDITY_MIN):
                    if h.get("result") is None: h["expired"] = True
                if not h.get("update_sent") and (now - h["time"]) > timedelta(hours=4):
                    if h.get("result") is None:
                        cur = get_current_price(h["pair"])
                        if cur:
                            pips = (cur - h["entry"]) if h["signal"] == "BUY" else (h["entry"] - cur)
                            dec = 3 if "JPY" in h["pair"] else 5
                            send_telegram(f"📊 <b>4H UPDATE</b> — {h['pair']} {h['signal']}\n"
                                          f"Entry: {h['entry']:.{dec}f} → Now: {cur:.{dec}f} ({pips:+.5f})")
                            h["update_sent"] = True
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
                        send_telegram(f"🎯 <b>TP HIT</b> — {h['pair']} {h['signal']} ({h.get('grade','?')})\n✅ Win")
                        h["result"] = "TP"; h["checked"] = True; save_journal()
                    elif hit == "SL":
                        send_telegram(f"🛑 <b>SL HIT</b> — {h['pair']} {h['signal']} ({h.get('grade','?')})\n❌ Loss")
                        h["result"] = "SL"; h["checked"] = True; save_journal()
                    time.sleep(API_DELAY)

            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
