import os
import time
import json
import requests
from datetime import datetime, timezone, timedelta

# ===== CONFIG (from Railway environment variables) =====
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID", "-1004375163609")
TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY", "944649b4a9d24074bbb41b2a954639cb")
FMP_KEY = os.getenv("FMP_KEY", "QmnG8t5DMUrIf8AWB7zHWcYa70kM4OCh")

PAIRS = ["EUR/USD", "USD/JPY", "GBP/USD", "AUD/USD", "USD/CAD", "XAU/USD"]
WAT_OFFSET = 1
SCAN_INTERVAL = 15 * 60
MAX_SIGNALS_PER_DAY = 8
CONFIDENCE_THRESHOLD = 70
DEDUP_HOURS = 4
SUMMARY_HOUR_WAT = 8
BIAS_HOUR_WAT = 7
SENTIMENT_CACHE_MIN = 120
SIGNAL_VALIDITY_MIN = 30
GRADE_A_PLUS = 85
GRADE_A = 75
GRADE_B = 65
JOURNAL_FILE = "signal_journal.json"

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
sentiment_cache = {}
journal = []

# ===== VALIDATION =====
if not BOT_TOKEN:
    print("❌ BOT_TOKEN environment variable is MISSING. Exiting.")
    exit(1)
print(f"✅ Config loaded. Bot ID: {BOT_TOKEN.split(':')[0] if BOT_TOKEN else 'none'}")

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
    allowed = PAIR_SESSIONS.get(pair, ["london", "ny"])
    return any(s in active for s in allowed)

def is_prime_time():
    h = (datetime.now(timezone.utc).hour + WAT_OFFSET) % 24
    return 13 <= h < 17

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
        if "values" not in data:
            print(f"    API fail {symbol} {interval}: {data.get('message', 'unknown')}")
            return None
        candles = list(reversed(data["values"]))
        now_utc = datetime.now(timezone.utc)
        im = {"15min": 15, "30min": 30, "1h": 60}.get(interval, 15)
        if candles:
            try:
                last_dt = datetime.fromisoformat(candles[-1]["datetime"]).replace(tzinfo=timezone.utc)
                if (now_utc - last_dt).total_seconds() < im * 60:
                    candles = candles[:-1]
            except: pass
        return candles
    except Exception as e:
        print(f"    Candle err {symbol}: {e}")
        return None

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
    return None

def get_sl_tp_multipliers(atr_pct, trending):
    if atr_pct < 30: sl, tp = 1.0, 1.8
    elif atr_pct > 75: sl, tp = 1.8, 2.5
    else: sl, tp = 1.2, 2.0
    if trending: tp += 0.5
    return sl, tp

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

def analyze(pair):
    if is_weekend() and pair != "XAU/USD": return {"pair": pair, "skip": "weekend"}
    if not is_active_session(): return {"pair": pair, "skip": "no_session"}
    if not pair_in_session(pair): return {"pair": pair, "skip": "wrong_session"}

    c15 = get_candles(pair, "15min", 100)
    c30 = get_candles(pair, "30min", 100)
    c1h = get_candles(pair, "1h", 100)
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
    signal = None; confidence = 0; reasons = []; has_trigger = False
    watch = False; watch_reason = ""

    if trend_bull_1h:
        confidence += 30; reasons.append("1h bullish trend")
        if trend_bull_30: confidence += 15; reasons.append("30m confirms bullish")
        if rsi_15 < 40: confidence += 20; reasons.append(f"RSI 15m {rsi_15:.1f} oversold"); has_trigger = True
        if rsi_30 < 45: confidence += 10; reasons.append(f"RSI 30m {rsi_30:.1f} supports")
        if rsi_1h and rsi_1h < 50: confidence += 5; reasons.append(f"RSI 1h {rsi_1h:.1f} aligned")
        if dist_support < 1.5: confidence += 15; reasons.append("Near support zone")
        if pattern == "Bullish Pin Bar": confidence += 10; reasons.append("Bullish pin bar"); has_trigger = True
        if is_prime_time(): confidence += 5; reasons.append("Prime time")
        if sentiment >= 3: confidence += 15; reasons.append(f"Sentiment +{sentiment:.1f} supports")
        elif sentiment <= -3: confidence -= 20; reasons.append(f"⚠️ Sentiment {sentiment:.1f} contradicts")
        if confidence >= CONFIDENCE_THRESHOLD and sentiment > -5 and has_trigger: signal = "BUY"
        elif 55 <= confidence < CONFIDENCE_THRESHOLD and rsi_15 < 50:
            watch = True; watch_reason = f"Waiting for RSI pullback (currently {rsi_15:.1f})"

    elif trend_bear_1h:
        confidence += 30; reasons.append("1h bearish trend")
        if trend_bear_30: confidence += 15; reasons.append("30m confirms bearish")
        if rsi_15 > 60: confidence += 20; reasons.append(f"RSI 15m {rsi_15:.1f} overbought"); has_trigger = True
        if rsi_30 > 55: confidence += 10; reasons.append(f"RSI 30m {rsi_30:.1f} supports")
        if rsi_1h and rsi_1h > 50: confidence += 5; reasons.append(f"RSI 1h {rsi_1h:.1f} aligned")
        if dist_resist < 1.5: confidence += 15; reasons.append("Near resistance zone")
        if pattern == "Bearish Pin Bar": confidence += 10; reasons.append("Bearish pin bar"); has_trigger = True
        if is_prime_time(): confidence += 5; reasons.append("Prime time")
        if sentiment <= -3: confidence += 15; reasons.append(f"Sentiment {sentiment:.1f} supports")
        elif sentiment >= 3: confidence -= 20; reasons.append(f"⚠️ Sentiment +{sentiment:.1f} contradicts")
        if confidence >= CONFIDENCE_THRESHOLD and sentiment < 5 and has_trigger: signal = "SELL"
        elif 55 <= confidence < CONFIDENCE_THRESHOLD and rsi_15 > 50:
            watch = True; watch_reason = f"Waiting for RSI rejection (currently {rsi_15:.1f})"

    result = {
        "pair": pair, "price": price, "rsi": rsi_15, "rsi_30": rsi_30, "rsi_1h": rsi_1h,
        "trend": "Bullish" if trend_bull_1h else ("Bearish" if trend_bear_1h else "Neutral"),
        "confidence": confidence, "sentiment": sentiment, "atr_pct": atr_pct,
    }

    if signal:
        trending = (trend_bull_1h or trend_bear_1h)
        sl_mult, tp_mult = get_sl_tp_multipliers(atr_pct, trending)
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
    elif watch:
        result["watch"] = True; result["watch_reason"] = watch_reason
    return result

def format_signal(s):
    emoji = "🟢" if s["signal"] == "BUY" else "🔴"
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
    msg = f"{emoji} <b>{s['signal']} — {s['pair']}</b>{prime_tag}\n"
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
    msg += f"📊 Vol: {s['atr_pct']:.0f}th pct\n"
    if s.get("pattern"): msg += f"🕯 {s['pattern']}\n"
    msg += "\n<b>Why:</b>\n"
    for r in s.get("reasons", []): msg += f"  • {r}\n"
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
    ps = {}
    for h in journal:
        p = h["pair"]
        if p not in ps: ps[p] = {"tp": 0, "sl": 0}
        if h.get("result") == "TP": ps[p]["tp"] += 1
        elif h.get("result") == "SL": ps[p]["sl"] += 1
    msg = "<b>📊 WEEKLY PERFORMANCE</b>\n━━━━━━━━━━━━━━━━━━━━\n"
    msg += f"📈 Signals: <b>{total}</b>\n🎯 TP: <b>{tp}</b>\n🛑 SL: <b>{sl}</b>\n⏳ Open: <b>{open_}</b>\n🏆 Win rate: <b>{wr:.1f}%</b>\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n<b>By pair:</b>\n"
    for p, s in ps.items(): msg += f"  {p}: {s['tp']}W / {s['sl']}L\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n<i>Ifyfx Signals Bot</i>"
    return msg

def main():
    global daily_signal_count, last_news_warn_time
    load_journal()
    print("Ifyfx Signals Bot v6.1 (secure) starting...")
    send_telegram("🤖 <b>Ifyfx Signals Bot v6.1 ONLINE</b>\n\n"
                  "🔐 <b>Secure mode</b>\n"
                  "✅ Session-aware\n"
                  "✅ Dynamic ATR\n"
                  "✅ Watch signals\n"
                  "✅ Daily bias (7 AM)\n"
                  "✅ 4h updates\n"
                  "✅ Real win tracking\n"
                  "✅ 70% threshold")

    while True:
        try:
            check_daily_reset()
            now = datetime.now(timezone.utc)
            wat = (now.hour + WAT_OFFSET) % 24
            sess = ", ".join(current_session()) or "closed"
            print(f"\n[{wat:02d}:{now.minute:02d} WAT] Scanning... session: {sess}")

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
                if not result: print(f"    {pair}: no result")
                elif "skip" in result: print(f"    {pair}: skipped ({result['skip']})")
                else:
                    conf = result.get("confidence", 0)
                    sig = result.get("signal")
                    if sig:
                        print(f"    {pair}: 🚨 {sig} ({result.get('grade')}) {conf}%")
                        if not is_duplicate(pair, sig): signals_found.append(result)
                    elif result.get("watch"):
                        print(f"    {pair}: ⏳ WATCH ({conf}%)")
                        watches_found.append(result)
                    else:
                        print(f"    {pair}: no signal (conf {conf}%)")
                    scan_data.append(result)
                time.sleep(2)

            if usd_correlated_recently():
                signals_found = [s for s in signals_found if "XAU" in s["pair"] or "JPY" in s["pair"]]

            for s in signals_found:
                if daily_signal_count < MAX_SIGNALS_PER_DAY:
                    send_telegram(format_signal(s))
                    record = {"pair": s["pair"], "signal": s["signal"], "time": now,
                              "entry": s["price"], "sl": s["sl"], "tp": s["tp"],
                              "grade": s["grade"], "confidence": s["confidence"],
                              "result": None, "checked": False, "expired": False,
                              "update_sent": False}
                    journal.append(record); signal_history.append(record); save_journal()
                    daily_signal_count += 1
                    print(f"  ✅ Sent #{daily_signal_count}")
                else: print(f"  ⛔ Daily limit")

            for w in watches_found[:2]: send_telegram(format_watch(w))

            if should_send_bias() and scan_data: send_telegram(format_bias(scan_data))
            if should_send_summary() and scan_data: send_telegram(format_summary(scan_data))
            if should_send_weekly_report(): send_telegram(format_weekly_report())

            for h in journal:
                if h.get("checked"): continue
                if (now - h["time"]) > timedelta(hours=24): h["checked"] = True; continue
                if not h.get("expired") and (now - h["time"]) > timedelta(minutes=SIGNAL_VALIDITY_MIN):
                    if h.get("result") is None:
                        send_telegram(f"⚠️ <b>SIGNAL EXPIRED</b> — {h['pair']} {h['signal']}\n<i>Do not enter.</i>")
                        h["expired"] = True
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
                    time.sleep(0.5)

            print(f"  ⏳ Sleeping {SCAN_INTERVAL//60} min...")
            time.sleep(SCAN_INTERVAL)
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(60)

if __name__ == "__main__":
    main()
