#!/usr/bin/env python3
"""Refresh live data and recompute the merger-announcement model.
Free sources: Yahoo Finance (yfinance) prices/options, SEC EDGAR submissions,
Polymarket gamma API, Kalshi public API. Anything that fails keeps its last value.
Usage: python update.py [--offline]
"""
import json, math, sys, datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
D = Path(__file__).parent / "data"
UA = {"User-Agent": "merger-dashboard research (contact: dashboard@example.com)"}
OFFLINE = "--offline" in sys.argv

def load(name, default=None):
    p = D / name
    return json.loads(p.read_text()) if p.exists() else default

def save(name, obj):
    (D / name).write_text(json.dumps(obj, indent=1))

def now_et():
    return dt.datetime.now(ET).strftime("%Y-%m-%d %H:%M ET")

# ---------------- live fetchers ----------------
def fetch_prices(live):
    import yfinance as yf
    out = {}
    for t in ["TSLA", "SPCX"]:
        tk = yf.Ticker(t); fi = tk.fast_info
        h = tk.history(period="5d").dropna(subset=["Close"])
        last, prevc = fi.get("lastPrice"), fi.get("previousClose")
        if last and last == last:
            out[t] = {"close": round(float(last), 2), "prev": round(float(prevc), 2), "date": str(dt.datetime.now(ET).date()) + " (last)"}
        else:
            out[t] = {"close": round(float(h["Close"].iloc[-1]), 2), "prev": round(float(h["Close"].iloc[-2]), 2), "date": str(h.index[-1].date())}
    live["prices"] = out; live["prices_as_of"] = now_et()

def fetch_options(live):
    import yfinance as yf
    t = yf.Ticker("TSLA"); spot = live["prices"]["TSLA"]["close"]
    rows = []
    for e in t.options:
        try:
            ch = t.option_chain(e)
            c, p = ch.calls, ch.puts
            c = c[(c.volume.fillna(0) > 0) | (c.openInterest.fillna(0) > 0)]
            atm = c.iloc[(c.strike - spot).abs().argsort()[:2]]
            iv = float(atm.impliedVolatility.mean())
            rows.append({"expiry": e, "atm_iv": round(iv * 100, 1),
                         "call_vol": int(ch.calls.volume.fillna(0).sum()),
                         "put_vol": int(p.volume.fillna(0).sum()),
                         "call_oi": int(ch.calls.openInterest.fillna(0).sum())})
        except Exception as ex:
            rows.append({"expiry": e, "error": str(ex)[:80]})
    live["options"] = rows; live["options_as_of"] = now_et()

def fetch_edgar(live, watch):
    import requests
    out = {}
    for name, cik in [("TSLA", "0001318605"), ("SPCX", "0001181412")]:
        r = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=UA, timeout=20).json()["filings"]["recent"]
        fl = []
        for i in range(min(25, len(r["form"]))):
            fl.append({"date": r["filingDate"][i], "form": r["form"][i], "items": r["items"][i],
                       "url": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{r['accessionNumber'][i].replace('-','')}/{r['primaryDocument'][i]}",
                       "watch": r["form"][i] in watch})
        out[name] = fl
    live["edgar"] = out; live["edgar_as_of"] = now_et()

def fetch_markets(live):
    import requests
    ev = requests.get("https://gamma-api.polymarket.com/events?slug=tesla-and-spacex-merger-officially-announced-by-june-30", timeout=20).json()[0]
    pm = []
    for m in ev["markets"]:
        if m.get("closed"): continue
        d = dt.datetime.strptime(m["groupItemTitle"], "%B %d, %Y").date()
        pm.append({"date": str(d), "p": float(json.loads(m["outcomePrices"])[0]), "label": "by " + m["groupItemTitle"]})
    ks = requests.get("https://api.elections.kalshi.com/trade-api/v2/markets?event_ticker=KXCOMPANYACTIONMERGER-27&limit=50", timeout=20).json()["markets"]
    km = []
    for m in ks:
        code = m["ticker"].split("-")[-1]  # e.g. 27FEB01
        d = dt.datetime.strptime(code, "%y%b%d").date() - dt.timedelta(days=1)
        if d < dt.date.today(): continue
        bid, ask, last = (float(m.get(k) or 0) for k in ("yes_bid_dollars", "yes_ask_dollars", "last_price_dollars"))
        p = (bid + ask) / 2 if 0 < ask - bid <= 0.06 else last
        km.append({"date": str(d), "p": round(p, 3), "label": m.get("yes_sub_title"), "bid": bid, "ask": ask, "last": last})
    live["markets"] = {"polymarket": sorted(pm, key=lambda x: x["date"]), "kalshi": sorted(km, key=lambda x: x["date"]),
                       "polymarket_note": "Resolves on an official announcement (incl. non-binding).",
                       "kalshi_note": "Resolves only on a definitive binding agreement; stricter than Polymarket.",
                       "polymarket_url": "https://polymarket.com/event/tesla-and-spacex-merger-officially-announced-by-june-30",
                       "kalshi_url": "https://kalshi.com/markets/kxcompanyactionmerger/company-merger/kxcompanyactionmerger-27"}
    live["markets_as_of"] = now_et()

NEWS_Q = '(Tesla SpaceX merger) OR (Tesla annual meeting proxy) OR (SpaceX SPCX lockup) OR (Tesla Roadster) OR (Starship launch) OR (Tesla 8-K)'
FACTOR_KW = [("sec", ["8-k","filing","sec ","proxy","annual meeting","committee","s-4","vote"]),
             ("markets", ["polymarket","kalshi","odds"]), ("options", ["options","implied volatility","call volume"]),
             ("calendar", ["launch","starship","lockup","lock-up","unlock","earnings","event","roadster","rebalance"]),
             ("prices", ["stock","shares rise","shares fall","valuation","market cap"])]

def fetch_news(news):
    """Append Google News RSS headlines (free) to news.json, grouped by ET day, deduped by URL/title."""
    import requests, urllib.parse, xml.etree.ElementTree as X
    from email.utils import parsedate_to_datetime
    url = "https://news.google.com/rss/search?q=" + urllib.parse.quote(NEWS_Q + " when:2d") + "&hl=en-US&gl=US&ceid=US:en"
    root = X.fromstring(requests.get(url, timeout=20, headers=UA).content)
    seen = {i["title"].lower() for d in news["days"] for i in d["items"]} | {i["url"] for d in news["days"] for i in d["items"]}
    added = 0
    for it in root.iter("item"):
        title, link = it.findtext("title"), it.findtext("link")
        if not title or title.lower() in seen or link in seen: continue
        t = parsedate_to_datetime(it.findtext("pubDate")).astimezone(ET)
        low = title.lower()
        if "quote" in low or "forecast -" in low: continue
        if not any(k in low for k in ["tesla","spacex","spcx","musk","starship"]): continue
        factor = next((f for f, kws in FACTOR_KW if any(k in low for k in kws)), "news")
        day = next((d for d in news["days"] if d["date"] == str(t.date())), None)
        if not day:
            day = {"date": str(t.date()), "odds_note": "", "items": []}; news["days"].append(day)
        day["items"].append({"time_et": t.strftime("%H:%M"), "title": title, "url": link, "source": (it.findtext("source") or "Google News"), "factor": factor, "effect": "auto-tagged; review", "auto": True})
        seen.add(title.lower()); added += 1
    news["days"].sort(key=lambda d: d["date"], reverse=True)
    return added

# ---------------- model ----------------
def daterange(a, b):
    d = a
    while d <= b:
        yield d; d += dt.timedelta(days=1)

def P(s): return dt.date.fromisoformat(s)

def rules_model(cfg):
    start, end = P(cfg["window"]["start"]), P(cfg["window"]["end"])
    p0 = cfg["horizon_prior"]["prob_by_end"]
    lo = math.log(p0 / (1 - p0)); contrib = []
    for s in cfg["signals"]:
        if s["active"]:
            lo += s["logodds"]; contrib.append({"label": s["label"], "logodds": s["logodds"]})
    p_end = 1 / (1 + math.exp(-lo))
    ev_by_day = {}
    for e in cfg["events"]:
        a = P(e["date"]); b = P(e.get("to", e["date"]))
        for d in daterange(a, b): ev_by_day.setdefault(str(d), []).append(e)
    wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    days = []
    for d in daterange(start, end):
        w = cfg["weekday_mult"][wd[d.weekday()]]; why = [f"{wd[d.weekday()]} x{w}"]
        for r in cfg["regimes"]:
            if P(r["from"]) <= d <= P(r["to"]):
                w *= r["mult"]
                if r["mult"] != 1: why.append(f"regime x{r['mult']}")
        evs = ev_by_day.get(str(d), [])
        for e in evs:
            m = cfg["event_types"][e["type"]]["mult"]
            w *= m
            if m != 1: why.append(f"{e['type']} x{m}")
        days.append({"date": str(d), "w": w, "why": why, "events": [e["label"] for e in evs],
                     "iv_event": any(cfg["event_types"][e["type"]]["iv"] for e in evs)})
    # Constant-hazard-style allocation: daily hazard h_d = k * w_d, solve k so survival hits 1-p_end
    lo_k, hi_k = 0.0, 1.0
    for _ in range(80):
        k = (lo_k + hi_k) / 2
        S = 1.0
        for x in days: S *= 1 - min(k * x["w"], 0.99)
        lo_k, hi_k = (k, hi_k) if S > 1 - p_end else (lo_k, k)
    S = 1.0
    for x in days:
        h = min(k * x["w"], 0.99); x["p"] = S * h; S *= 1 - h; x["cum"] = 1 - S
    return days, p_end, contrib

def judgment_curve(days, jud, start):
    # Piecewise: scale rules-model daily shape within each anchor segment to hit judgment anchors
    anchors = [(P(a["date"]), a["p"]) for a in jud["anchors"]]
    prev_d, prev_p = start - dt.timedelta(days=1), 0.0
    out = {}
    for ad, ap in anchors:
        seg = [x for x in days if prev_d < P(x["date"]) <= ad]
        tot = sum(x["p"] for x in seg) or 1
        c = prev_p
        for x in seg:
            c += (ap - prev_p) * x["p"] / tot; out[x["date"]] = c
        prev_d, prev_p = ad, ap
    return out

def market_curve(points, dates):
    # Monotone linear interpolation of market cumulative odds; 0 at window start
    pts = [(P(dates[0]) - dt.timedelta(days=1), 0.0)] + [(P(x["date"]), x["p"]) for x in points]
    pts.sort(); fixed = []; mx = 0
    for d, p in pts: mx = max(mx, p); fixed.append((d, mx))
    out = {}
    for s in dates:
        d = P(s)
        if d > fixed[-1][0]: out[s] = None; continue
        for (d0, p0), (d1, p1) in zip(fixed, fixed[1:]):
            if d0 <= d <= d1:
                out[s] = p0 + (p1 - p0) * (d - d0).days / max((d1 - d0).days, 1); break
    return out

def cum_at(curve, date):
    keys = [k for k in curve if k <= date and curve[k] is not None]
    return curve[max(keys)] if keys else 0.0

def signed_from_any(dates, cum_any, cfg):
    """Daily cumulative for 'signed agreement' given cumulative for 'any announcement'."""
    L = cfg["signing_lag"]; k = L["lag_gamma_shape"]; th = L["lag_mean_days"] / k
    lag = [0.0] * (L["max_lag_days"] + 1)
    for t in range(1, len(lag)):
        x = t - 0.5; lag[t] = x ** (k - 1) * math.exp(-x / th) / (math.gamma(k) * th ** k)
    tot = sum(lag); lag = [v / tot for v in lag]
    pdf = []; prev = 0
    for s_ in dates: pdf.append(cum_any[s_] - prev); prev = cum_any[s_]
    f, nf = L["share_signed_at_announcement"], 1 - L["p_never_signs"]
    out = {}; c = 0
    for i, s_ in enumerate(dates):
        v = f * pdf[i] + (1 - f) * nf * sum(pdf[i - t] * lag[t] for t in range(1, min(i, len(lag) - 1) + 1))
        c += v; out[s_] = c
    return out

MODEL_KEYS = ["window", "horizon_prior", "signals", "regimes", "weekday_mult", "event_types", "events", "signing_lag"]
MODEL_SERIES = ["rules", "judgment", "rules_signed", "judgment_signed"]

def model_inputs(cfg, jud):
    return {"cfg": {k: cfg[k] for k in MODEL_KEYS}, "jud": jud["anchors"]}

def model_headline(cfg, jud, base):
    """Headline odds of the 4 model series for a given set of inputs, measured from `base`."""
    days, _, _ = rules_model(cfg); dates = [x["date"] for x in days]
    r = {x["date"]: x["cum"] for x in days}; j = judgment_curve(days, jud, P(cfg["window"]["start"]))
    cur = {"rules": r, "judgment": j, "rules_signed": signed_from_any(dates, r, cfg), "judgment_signed": signed_from_any(dates, j, cfg)}
    hz = lambda c, n: cum_at(c, str(base + dt.timedelta(days=n))) - cum_at(c, str(base))
    return {k: {"d7": hz(c, 7), "d30": hz(c, 30), "d90": hz(c, 90), "end": cum_at(c, cfg["window"]["end"])} for k, c in cur.items()}

def input_drivers(old, new):
    """Human-readable list of model-input changes between two input snapshots."""
    out = []
    os_ = {x["id"]: x for x in old["cfg"]["signals"]}; ns = {x["id"]: x for x in new["cfg"]["signals"]}
    for i, x in ns.items():
        o = os_.get(i)
        if not o: out.append(f"new signal: {x['label']} ({x['logodds']:+})" + (" [on]" if x["active"] else ""))
        elif o["active"] != x["active"]: out.append(f"signal {'ON' if x['active'] else 'OFF'}: {x['label']} ({x['logodds']:+})")
        elif o["logodds"] != x["logodds"]: out.append(f"signal weight {o['logodds']:+} -> {x['logodds']:+}: {x['label']}")
    key = lambda e: (e["date"], e.get("to"), e["type"], e["label"])
    oe = {key(e) for e in old["cfg"]["events"]}; ne = {key(e) for e in new["cfg"]["events"]}
    out += [f"calendar added: {e[0]} {e[3]}" for e in sorted(ne - oe)] + [f"calendar removed: {e[0]} {e[3]}" for e in sorted(oe - ne)]
    if old["jud"] != new["jud"]: out.append("judgment anchors updated: " + ", ".join(f"{a['date']} {a['p']:.0%}" for a in new["jud"]))
    for k in MODEL_KEYS:
        if k not in ("signals", "events") and old["cfg"].get(k) != new["cfg"].get(k): out.append(f"config changed: {k}")
    return out

def main():
    cfg, jud = load("config.json"), load("judgment.json")
    live = load("live.json", {})
    if not OFFLINE:
        for f in (fetch_prices, fetch_options, lambda l: fetch_edgar(l, cfg["alerts"]["watch_forms"]), fetch_markets):
            try: f(live)
            except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} {getattr(f,'__name__','edgar')}: {ex}")
        live["errors"] = live.get("errors", [])[-10:]
        save("live.json", live)
    news = load("news.json", {"days": []})
    if not OFFLINE:
        try: fetch_news(news)
        except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} news: {ex}")
    start = P(cfg["window"]["start"])
    days, p_end, contrib = rules_model(cfg)
    dates = [x["date"] for x in days]
    rules = {x["date"]: x["cum"] for x in days}
    judg = judgment_curve(days, jud, start)
    rules_s = signed_from_any(dates, rules, cfg)
    judg_s = signed_from_any(dates, judg, cfg)
    mk = live.get("markets", {})
    poly = market_curve(mk.get("polymarket", []), dates) if mk else {}
    kal = market_curve(mk.get("kalshi", []), dates) if mk else {}
    today = dt.datetime.now(ET).date()
    base = max(today, start - dt.timedelta(days=1))
    def horizon(curve, n):
        return cum_at(curve, str(base + dt.timedelta(days=n))) - cum_at(curve, str(base))
    H = {}
    for name, c in [("rules", rules), ("judgment", judg), ("rules_signed", rules_s), ("judgment_signed", judg_s), ("polymarket", poly), ("kalshi", kal)]:
        H[name] = {"d7": horizon(c, 7), "d30": horizon(c, 30), "d90": horizon(c, 90), "end": cum_at(c, cfg["window"]["end"]) if c and c.get(cfg["window"]["end"]) is not None else None}
    # expected date (conditional on announcement in window), rules & judgment
    def exp_date(curve):
        prev = 0; acc = 0; tot = 0
        for s in dates:
            p = curve[s] - prev; prev = curve[s]; acc += p * (P(s) - start).days; tot += p
        return str(start + dt.timedelta(days=round(acc / tot))) if tot else None
    def median_date(curve):
        tgt = curve[dates[-1]] / 2
        return next(s for s in dates if curve[s] >= tgt)
    # expiries
    opts = {o["expiry"]: o for o in live.get("options", []) if "expiry" in o}
    exps = sorted(opts) if opts else []
    ev_iv = [(x["date"], x["events"]) for x in days if x["iv_event"]]
    exp_rows = []
    for e in exps:
        o = opts[e]; d = P(e)
        row = {"expiry": e, "dte": (d - today).days, "atm_iv": o.get("atm_iv"), "call_vol": o.get("call_vol"),
               "kind": "weekly" if not (15 <= d.day <= 21 and d.weekday() == 4) and d.month not in () else "monthly",
               "iv_events": [f"{a}: {'; '.join(b)}" for a, b in ev_iv if str(today) <= a <= e]}
        if 15 <= d.day <= 21 and d.weekday() in (3, 4): row["kind"] = "monthly"
        if (d - today).days > 270: row["kind"] = "LEAP"
        for name, c in [("rules", rules), ("judgment", judg), ("rules_signed", rules_s), ("judgment_signed", judg_s), ("polymarket", poly), ("kalshi", kal)]:
            v = None
            if c and (e <= dates[-1]) and c.get(min(e, dates[-1])) is not None:
                v = cum_at(c, e) - cum_at(c, str(base))
            row[name] = v
        exp_rows.append(row)
    # weekly odds (rules & judgment), week starting Monday
    weeks = {}
    prevr = prevj = prevrs = prevjs = 0
    for s in dates:
        d = P(s); wk = str(d - dt.timedelta(days=d.weekday()))
        w = weeks.setdefault(wk, {"week": wk, "rules": 0, "judgment": 0, "rules_signed": 0, "judgment_signed": 0})
        w["rules"] += rules[s] - prevr; w["judgment"] += judg[s] - prevj; prevr, prevj = rules[s], judg[s]
        w["rules_signed"] += rules_s[s] - prevrs; w["judgment_signed"] += judg_s[s] - prevjs; prevrs, prevjs = rules_s[s], judg_s[s]
    # history, deltas, and time-decay vs news-driven split
    hist = load("history.json", [])
    inputs = model_inputs(cfg, jud)
    snap = {"date": str(today), "as_of": now_et(), **{f"{k}_{h}": v[h] for k, v in H.items() for h in v},
            "signals": sorted(x["label"] for x in contrib), "inputs": inputs}
    prev = next((h for h in reversed(hist) if h["date"] < str(today)), None)
    num = lambda x: x is not None
    delta = {k: (round((snap[k] - prev[k]) * 100, 2) if prev and num(snap.get(k)) and num(prev.get(k)) else None)
             for k in snap if k not in ("date", "as_of", "signals", "inputs")}
    split = {}; drivers = []
    if prev and prev.get("inputs"):
        old_cfg = {**cfg, **prev["inputs"]["cfg"]}
        cf = model_headline(old_cfg, {"anchors": prev["inputs"]["jud"]}, base)   # yesterday's inputs, today's date
        drivers = input_drivers(prev["inputs"], inputs)
        for k in MODEL_SERIES:
            for h in ("d7", "d30", "d90", "end"):
                pv = prev.get(f"{k}_{h}")
                if pv is None: continue
                split[f"{k}_{h}"] = {"total": round((H[k][h] - pv) * 100, 3),
                                     "time_decay": round((cf[k][h] - pv) * 100, 3),
                                     "news": round((H[k][h] - cf[k][h]) * 100, 3)}
    mkt_moves = {k: delta.get(k) for k in delta if k.startswith(("polymarket", "kalshi")) and delta.get(k)}
    day = next((d for d in news["days"] if d["date"] == str(today)), None)
    if not day:
        day = {"date": str(today), "odds_note": "", "items": []}; news["days"].insert(0, day)
    heads = [f"{i['title']} ({i['source']}) [{i['factor']}]" for i in day["items"]]
    news_moved = any(abs(v["news"]) > 1e-6 for v in split.values())
    if prev:
        def fmt(k):
            v = split.get(k)
            return f"{v['total']:+.1f} pts (time {v['time_decay']:+.1f}, news {v['news']:+.1f})" if v else "n/a"
        day["odds_note"] = (f"Any-announcement 30d: {fmt('rules_d30')}; signed-deal 30d: {fmt('rules_signed_d30')}. "
                            + ("News drivers: " + "; ".join(drivers) + ". " if drivers else "No input changes, so this is time decay only. ")
                            + (f"Market moves (pts): {mkt_moves}." if mkt_moves else ""))
        day["split"] = split; day["drivers"] = drivers
    hist = [h for h in hist if h["date"] != str(today)] + [snap]
    save("history.json", hist[-400:])
    news["days"].sort(key=lambda d: d["date"], reverse=True); save("news.json", news)
    # Alerts: news-driven model change (either definition) or a watched filing. Pure time decay never alerts.
    alerts = []
    if news_moved:
        top = {k: v["news"] for k, v in split.items() if k.endswith("_d30") or k.endswith("_end")}
        alerts.append({"type": "news_driven_change", "news_pp": top, "drivers": drivers, "headlines": heads,
                       "text": "News-driven odds change: " + ", ".join(f"{k} {v:+.1f}pts" for k, v in top.items() if abs(v) > 1e-6)})
    for co, fl in live.get("edgar", {}).items():
        for f in fl:
            if f["watch"] and f["date"] >= str(today - dt.timedelta(days=1)):
                alerts.append({"type": "filing", "text": f"{co} filed {f['form']} on {f['date']}", "url": f["url"]})
    # valuation split
    pr = live.get("prices", {}); val = {}
    if pr:
        sh_t, sh_s = cfg.get("shares", {}).get("TSLA", 3.92e9), cfg.get("shares", {}).get("SPCX", 13.5728e9)
        t, s = pr["TSLA"]["close"], pr["SPCX"]["close"]
        mt, ms = t * sh_t, s * sh_s
        val = {"tsla_cap": mt, "spcx_cap": ms, "split_spcx": ms / (mt + ms), "ratio_market": t / s,
               "tsla_at_50_50": ms / sh_t,
               "offers": [{"tsla_price": x, "ratio": x / s, "premium": x / t - 1, "split_spcx": ms / (ms + x * sh_t), "new_spcx_shares": x * sh_t / s} for x in (400, 450, 500, 550)],
               "shares_note": "SPCX 13.573B = 10-Q cover 13.182B + 391M Cursor shares (tape.panke.app sum). TSLA ~3.92B is an ESTIMATE backed out of a ~$1.47T market cap (24/7 Wall St); confirm against Tesla's 10-Q."}
    exp_s = {"rules": exp_date(rules_s), "judgment": exp_date(judg_s)}
    model = {"generated": now_et(), "window": cfg["window"], "headline": H, "delta_pp": delta, "prev_as_of": prev["as_of"] if prev else None,
             "rules_p_end": p_end, "rules_contrib": contrib, "prior": cfg["horizon_prior"]["prob_by_end"],
             "expected_date": {"rules": exp_date(rules), "judgment": exp_date(judg), "rules_signed": exp_s["rules"], "judgment_signed": exp_s["judgment"]},
             "signing_lag": cfg["signing_lag"],
             "median_date": {"rules": median_date(rules), "judgment": median_date(judg), "rules_signed": median_date(rules_s), "judgment_signed": median_date(judg_s)},
             "daily": [{"date": x["date"], "rules_p": x["p"], "rules_cum": x["cum"], "w": round(x["w"], 4), "why": x["why"], "events": x["events"], "iv_event": x["iv_event"],
                        "judgment_cum": judg[x["date"]], "rules_s_cum": rules_s[x["date"]], "judgment_s_cum": judg_s[x["date"]], "poly_cum": poly.get(x["date"]) if poly else None, "kalshi_cum": kal.get(x["date"]) if kal else None} for x in days],
             "expiries": exp_rows, "weeks": list(weeks.values()), "alerts": alerts, "split": split, "drivers": drivers, "market_moves": mkt_moves, "valuation": val}
    save("model.json", model)
    print(json.dumps({"headline": H, "alerts": alerts, "expected": model["expected_date"]}, indent=1, default=str))

if __name__ == "__main__":
    main()
