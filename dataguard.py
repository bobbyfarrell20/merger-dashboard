"""Input guard for prices, implied vol and prediction-market quotes (stdlib only).

Every fetched number is checked before it is used or saved:
  * sanity bounds (price ranges, IV range, probabilities in [0, 1])
  * max deviation vs the last known good value (TSLA/SPCX price, per-expiry IV)
  * previous close must match the close we recorded for that session
  * staleness of quote timestamps (trading-hours aware)
  * crossed / empty order books and last trades far outside the book
A rejected value is replaced by the last good value and a warning is logged; rejections also
produce a "REVIEW NEEDED: data guard ..." item (deduped per field per day). A rejected value that
repeats on `confirm_runs` consecutive runs (within `confirm_tol`) is accepted as a genuine move,
so a real regime change (e.g. a deal-day gap) can't be locked out for long.
"""
import math, datetime as dt
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
# Nasdaq full closures and early-close days, 2026-2027 (trackers.py imports these)
HOLIDAYS = {"2026-11-26", "2026-12-25", "2027-01-01", "2027-01-18", "2027-02-15", "2027-03-26", "2027-05-31",
            "2027-06-18", "2027-07-05", "2027-09-06", "2027-11-25", "2027-12-24"}
HALF_DAYS = {"2026-11-27", "2026-12-24", "2027-07-02", "2027-11-26"}

DEFAULTS = {
    "price_bounds": {"TSLA": [50, 2500], "SPCX": [10, 1500]},
    "max_move": 0.25,          # price vs last good price (fraction)
    "prev_close_tol": 0.005,   # prev close vs recorded close for that session (fraction)
    "price_max_age_min": 45,   # regular session: quote older than this is stale
    "price_max_age_days": 4,   # any time: quote older than this is stale
    "iv_bounds": [15.0, 250.0],# ATM IV in vol points
    "iv_min_dte": 1,           # same-day (0 DTE) IV from free feeds is unreliable intraday: not shown
    "iv_max_rel_move": 0.40,   # vs last good IV for the same expiry
    "iv_good_max_age_days": 3, # last good IV older than this is not used as a fallback
    "confirm_runs": 3,         # a rejected value repeated this many runs in a row is accepted
    "confirm_tol": 0.02,       # ... if each repeat is within 2% of the first rejected value
    "quote_max_age_h": 12,     # prediction-market quote older than this is stale
    "quote_max_lag_h": 3,      # ... or this much older than the freshest contract quote
    "quote_last_slack": 0.02,  # last trade must sit within the book +/- this to be used
}

def cfg_of(cfg):
    return {**DEFAULTS, **((cfg or {}).get("data_guard") or {})}

def _num(x):
    try:
        x = float(x)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None

def _ts(s):
    """'YYYY-MM-DD HH:MM ET' or ISO -> aware datetime (ET)."""
    if isinstance(s, dt.datetime): return s if s.tzinfo else s.replace(tzinfo=ET)
    s = str(s).replace(" ET", "")
    d = dt.datetime.fromisoformat(s.replace(" ", "T") if "T" not in s else s)
    return d if d.tzinfo else d.replace(tzinfo=ET)

def trading_day(d): return d.weekday() < 5 and str(d) not in HOLIDAYS
def prev_trading_day(d):
    d -= dt.timedelta(days=1)
    while not trading_day(d): d -= dt.timedelta(days=1)
    return d
def session_open(now):
    n = now.astimezone(ET)
    if not trading_day(n.date()): return False
    m = n.hour * 60 + n.minute; close = 13 * 60 if str(n.date()) in HALF_DAYS else 16 * 60
    return 9 * 60 + 30 <= m < close

def warn(st, now, key, msg, review=True, level="warn"):
    w = {"at": now.strftime("%Y-%m-%d %H:%M ET"), "key": key, "level": level, "review": review, "msg": msg}
    st.setdefault("log", []).append(w); st["log"] = st["log"][-40:]
    return w

def _confirm(st, key, val, c):
    """Track repeats of a rejected value. True once it has repeated confirm_runs times."""
    pend = st.setdefault("pending", {}); p = pend.get(key)
    if p and abs(val / p["v"] - 1) <= c["confirm_tol"]: p["n"] += 1
    else: p = pend[key] = {"v": val, "n": 1}
    if p["n"] >= c["confirm_runs"]:
        pend.pop(key, None); return True
    return False

def _clear_pending(st, key): st.setdefault("pending", {}).pop(key, None)

# ---------------- prices ----------------
def check_price(sym, cand, st, now, cfg=None):
    """cand = {'price', 'prev', 'time'(datetime|str)}. st = persistent guard state (mutated).
    Returns (accepted dict {'price','prev','time','stale','held'}, warnings list)."""
    c = cfg_of(cfg); W = []
    lg = st.setdefault("last_good", {}).get(sym)          # {'price','time'}
    closes = st.setdefault("closes", {}).setdefault(sym, {})
    price, prev = _num(cand.get("price")), _num(cand.get("prev"))
    try: t = _ts(cand.get("time"))
    except Exception: t = None
    lo, hi = c["price_bounds"].get(sym, [0.01, 1e6])
    held = []
    def hold(field, why):
        held.append(field); W.append(warn(st, now, f"{sym}:{field}", why))
    # price
    if price is None or not (lo <= price <= hi):
        hold("price", f"{sym} price {cand.get('price')} outside sane range ${lo}-${hi}; kept last good {lg and lg['price']}")
        price = lg and lg["price"]
    elif t is None:
        hold("price", f"{sym} quote has no usable timestamp; kept last good {lg and lg['price']}"); price = lg and lg["price"]
    elif lg and t < _ts(lg["time"]) - dt.timedelta(minutes=1):
        hold("price", f"{sym} quote time {t:%Y-%m-%d %H:%M} is older than the last good one ({lg['time']}); kept last good")
        price, t = lg["price"], _ts(lg["time"])
    elif lg and abs(price / lg["price"] - 1) > c["max_move"]:
        if _confirm(st, f"{sym}:price", price, c):
            W.append(warn(st, now, f"{sym}:price", f"{sym} price {price} confirmed on {c['confirm_runs']} runs in a row ({price / lg['price'] - 1:+.0%} vs last good {lg['price']}); accepted", review=True))
        else:
            hold("price", f"{sym} price {price} is {price / lg['price'] - 1:+.0%} vs last good {lg['price']} (limit {c['max_move']:.0%}); kept last good")
            price, t = lg["price"], _ts(lg["time"])
    else:
        _clear_pending(st, f"{sym}:price")
    # staleness (informational: the value is still the latest real print)
    stale = False
    if t is not None:
        age = now - t
        if session_open(now) and age > dt.timedelta(minutes=c["price_max_age_min"]) and "price" not in held:
            stale = True; W.append(warn(st, now, f"{sym}:stale", f"{sym} quote is {age.total_seconds() / 60:.0f} min old during market hours", review=False))
        elif age > dt.timedelta(days=c["price_max_age_days"]):
            stale = True; W.append(warn(st, now, f"{sym}:stale", f"{sym} quote is {age.days} days old", review=False))
    # previous close: must equal the close we recorded for the prior session
    if t is not None:
        sess = t.date() if (trading_day(t.date()) and t.hour * 60 + t.minute >= 9 * 60 + 30) else prev_trading_day(t.date())
        want = str(prev_trading_day(sess)); rec = closes.get(want)
        if rec is not None and (prev is None or abs(prev / rec - 1) > c["prev_close_tol"]):
            hold("prev", f"{sym} previous close {prev} doesn't match the recorded {want} close {rec} ({(prev or 0) / rec - 1:+.1%}); used {rec}")
            prev = rec
        elif rec is None and prev is not None and price and abs(prev / price - 1) > c["max_move"]:
            hold("prev", f"{sym} previous close {prev} is {prev / price - 1:+.0%} from the current price {price}; not shown")
            prev = None
        if prev is not None and rec is None and "prev" not in held:
            closes[want] = round(prev, 2)
        # record today's close once the session is over
        if price is not None and "price" not in held and t.hour * 60 + t.minute >= (13 * 60 if str(t.date()) in HALF_DAYS else 16 * 60) and trading_day(t.date()):
            closes[str(t.date())] = round(price, 2)
        for k in sorted(closes)[:-12]: closes.pop(k)
    if price is not None and "price" not in held and t is not None:
        st["last_good"][sym] = {"price": round(price, 4), "time": t.strftime("%Y-%m-%d %H:%M ET")}
    return {"price": price, "prev": prev, "time": t, "stale": stale, "held": held}, W

# ---------------- implied vol ----------------
def check_iv(rows, st, now, cfg=None):
    """rows: [{'expiry','atm_iv',...}] (mutated in place). Bad IVs are replaced by last good (or None)."""
    c = cfg_of(cfg); W = []; today = now.date(); good = st.setdefault("iv", {})
    lo, hi = c["iv_bounds"]
    for r in rows:
        e = r.get("expiry")
        if not e or "atm_iv" not in r: continue
        iv = _num(r.get("atm_iv")); dte = (dt.date.fromisoformat(e) - today).days; lg = good.get(e)
        lg_ok = lg and (now - _ts(lg["as_of"])) <= dt.timedelta(days=c["iv_good_max_age_days"])
        key = f"iv:{e}"
        if dte < c["iv_min_dte"]:
            r["atm_iv"] = None; r["iv_note"] = f"{dte} DTE: not shown"; continue
        if iv is None or iv < 1:      # free feed returns 0 outside market hours: quietly keep last good
            r["atm_iv"] = lg["iv"] if lg_ok else None; r["iv_note"] = "feed empty (off-hours); last good" if lg_ok else "feed empty"
            continue
        bad = None
        if not (lo <= iv <= hi): bad = f"outside {lo:g}-{hi:g}"
        elif lg_ok and abs(iv / lg["iv"] - 1) > c["iv_max_rel_move"]: bad = f"{iv / lg['iv'] - 1:+.0%} vs last good {lg['iv']}"
        if bad and _confirm(st, key, iv, {**c, "confirm_tol": 0.10}):
            W.append(warn(st, now, key, f"IV {e} {iv} confirmed on {c['confirm_runs']} runs in a row ({bad}); accepted")); bad = None
        if bad:
            W.append(warn(st, now, key, f"ATM IV for {e} = {iv} ({bad}); kept {'last good ' + str(lg['iv']) if lg_ok else 'blank'}"))
            r["atm_iv"] = lg["iv"] if lg_ok else None; r["iv_note"] = "held: " + bad
        else:
            _clear_pending(st, key); good[e] = {"iv": iv, "as_of": now.strftime("%Y-%m-%d %H:%M ET")}
    for e in [e for e in good if e < str(today)]: good.pop(e)
    return rows, W

# ---------------- prediction-market quotes ----------------
def clean_quote(bid, ask, last, vol, mw, cfg=None):
    """Return (p, how, issue). how: mid | last | thin | bad. Never uses a crossed/empty book's mid,
    or a last trade far outside the live book."""
    c = cfg_of(cfg); bid, ask, last, vol = _num(bid), _num(ask), _num(last), _num(vol) or 0.0
    rng = lambda x: x is not None and 0.0 <= x <= 1.0
    issue = None
    if (bid is not None and not rng(bid)) or (ask is not None and not rng(ask)) or (last is not None and not rng(last)):
        return None, "bad", "price outside [0, 1]"
    book = bid is not None and ask is not None and not (bid <= 0 and ask >= 1)
    if bid is not None and ask is not None and ask < bid:
        issue = f"crossed book (bid {bid} > ask {ask})"; book = False
    elif not book and (bid is not None or ask is not None):
        issue = "empty book (no bid and no offer)" if (bid or 0) <= 0 and (ask is None or ask >= 1) else "one-sided book"
    if book and 0 < ask - bid <= mw["max_spread"]:
        return round((bid + ask) / 2, 4), "mid", issue
    if last is not None and vol >= mw["min_volume_24h"]:
        if book and not (bid - c["quote_last_slack"] <= last <= ask + c["quote_last_slack"]):
            return None, "thin", f"last trade {last} outside the book {bid}/{ask}"
        return round(last, 4), "last", issue
    return None, "thin", issue

def quote_staleness(t_last, newest, now, cfg=None):
    """Return a reason string if a contract's latest quote is stale, else None."""
    c = cfg_of(cfg); t = _ts(t_last)
    if now - t > dt.timedelta(hours=c["quote_max_age_h"]): return f"last quote {(now - t).total_seconds() / 3600:.0f}h old"
    if newest and _ts(newest) - t > dt.timedelta(hours=c["quote_max_lag_h"]): return f"quote {(_ts(newest) - t).total_seconds() / 3600:.0f}h behind the other contracts"
    return None

def review_hits(warnings, now, source="data guard"):
    """REVIEW NEEDED items for rejections (one per field per day; seen.json dedupes the id)."""
    out = {}
    for w in warnings:
        if not w.get("review"): continue
        out.setdefault(w["key"], {"id": f"guard:{w['key']}:{now:%Y-%m-%d}", "type": "review", "title": f"REVIEW NEEDED: {source}: {w['msg']}",
                                  "url": None, "source": "tracker", "time_et": now.strftime("%Y-%m-%d %H:%M"), "factor": "prices"})
    return list(out.values())
