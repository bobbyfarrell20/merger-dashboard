#!/usr/bin/env python3
"""Refresh live data and recompute the merger-announcement model.
Free sources: Yahoo Finance (yfinance) prices/options, SEC EDGAR submissions,
Polymarket gamma API, Kalshi public API. Anything that fails keeps its last value.
Usage: python update.py [--offline]
"""
import json, math, re, sys, datetime as dt
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
    """Yahoo chart meta: regularMarketPrice + regularMarketTime (intraday during market hours), previous close."""
    import requests
    out = {}
    for t in ["TSLA", "SPCX"]:
        r = requests.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{t}?interval=1d&range=5d", headers={"User-Agent": "Mozilla/5.0"}, timeout=15).json()
        mt = r["chart"]["result"][0]["meta"]
        ts = dt.datetime.fromtimestamp(mt["regularMarketTime"], ET)
        prev = mt.get("previousClose") or mt.get("chartPreviousClose")
        if ts.date() < dt.datetime.now(ET).date() or not (ts.hour < 16):
            closes = [c for c in r["chart"]["result"][0]["indicators"]["quote"][0]["close"] if c]
            prev = closes[-2] if len(closes) > 1 else prev
        out[t] = {"close": round(float(mt["regularMarketPrice"]), 2), "prev": round(float(prev), 2),
                  "date": ts.strftime("%Y-%m-%d %H:%M ET"), "market_time": ts.strftime("%Y-%m-%d %H:%M ET")}
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
WD = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
def P(s): return dt.date.fromisoformat(s)
def logit(p): return math.log(p / (1 - p))
def sig(x): return 1 / (1 + math.exp(-x))

def inputs_of(cfg, jud):
    keys = ["window", "p_any_by_end", "a_share", "signals", "urgency", "weekday_mult", "blackouts", "post_event_boost", "timing_anchors", "leak_decay", "lockups", "starship", "grain", "special_meeting_planned", "blank_reasons"]
    out = {k: cfg[k] for k in keys}
    ss, gr = events_state(cfg); out["starship"], out["grain"] = ss, gr   # live event states count as news
    return out

def leak_state(cfg, today):
    """Blank-week count from the defined clock start; penalty softened by the quiet-hold share of merger-hold reasons."""
    ld = cfg["leak_decay"]; leak_on = not any(x["id"] == "committee_or_leak" and x["active"] for x in cfg["signals"])
    weeks_blank = max(0, (today - P(ld["ref_date"])).days // 7)
    br = blank_reasons(cfg, today)
    act = sum(r["p"] for r in br if r["id"] == "merger_hold"); quiet = sum(r["p"] for r in br if r["id"] == "quiet_hold")
    soft = act / (act + quiet) if ld.get("soften_by_quiet_hold") and act + quiet > 0 else 1.0
    raw = max(ld["cap"], ld["per_week"] * weeks_blank)
    return {"on": leak_on, "weeks_blank": weeks_blank, "raw": raw, "soft": soft, "logodds": raw * soft if leak_on else 0.0,
            "start": ld["ref_date"]}

def events_state(cfg):
    ev = load("events_state.json", {})
    st = dict(cfg["starship"]); st.update({k: v for k, v in ev.get("starship", {}).items() if v is not None})
    gr = dict(cfg["grain"]); gr.update({k: v for k, v in ev.get("grain", {}).items() if v is not None})
    return st, gr

def day_close_min(cfg, d):
    """Minutes after midnight ET when day d closes: 8 PM, or later if a scheduled event runs past it (event_end_et, default start + 2.5h)."""
    ro = cfg.get("rolloff", {}); hm = lambda t: int(t.split(":")[0]) * 60 + int(t.split(":")[1])
    close = hm(ro.get("day_close_et", "20:00"))
    for b in cfg.get("blackouts", []) + cfg.get("post_event_boost", []):
        if b.get("date") == str(d) and (b.get("event_end_et") or b.get("event_start_et")):
            end = hm(b["event_end_et"]) if b.get("event_end_et") else hm(b["event_start_et"]) + int(60 * ro.get("event_default_hours", 2.5))
            close = max(close, end)
    return close

def headline_of(cfg, mm):
    hz = cfg.get("headline_horizon", "2026-11-26"); dd = [x for x in mm["days"] if x["date"] <= hz]; T = mm["totals"]
    a = sum(x["pA"] for x in dd); b = sum(x["pB"] for x in dd); Lp = sum(x["pL"] for x in dd) if mm["later"].get("on") else 0.0
    return {"X": a + Lp, "A": a, "B": Lp, "C": b - Lp, "D": T["none"]}

def odds_history(cfg, today, inp, prev, news_day):
    """Daily merger-any-path (Nov 26 basis, toggle OFF) points; today's point is overwritten on reruns."""
    oh = load("odds_history.json", [])
    off = {**cfg, "special_meeting_planned": {**cfg["special_meeting_planned"], "on": False}}
    H = headline_of(off, compute(off, today))
    pt = {"date": str(today), "as_of": now_et(), **{k: round(v, 5) for k, v in H.items()}, "news": 0.0, "time": 0.0, "events": []}
    if prev:
        pin = {**off, **prev["inputs"], "special_meeting_planned": off["special_meeting_planned"]}
        cf = headline_of(off, compute(pin, today))["X"]
        pp = next((p for p in reversed(oh) if p["date"] < str(today)), None)
        base = pp["X"] if pp else cf
        pt["news"] = round((H["X"] - cf) * 100, 2); pt["time"] = round((H["X"] - base) * 100 - pt["news"], 2)
    if abs(pt["news"]) >= 0.05:
        items = (news_day or {}).get("items", [])
        tagged = [i for i in items if i.get("odds_event")]
        for i in tagged or items[:1]:
            oe = i.get("odds_event", {})
            pt["events"].append({"label": oe.get("label") or i["title"][:28], "delta": oe.get("delta", pt["news"]),
                                 "title": i["title"], "url": i.get("url_alt") or i.get("url"), "source": i.get("source")})
    oh = [p for p in oh if p["date"] != str(today)] + [pt]
    oh.sort(key=lambda p: p["date"]); save("odds_history.json", oh)
    return oh

def compute(cfg, today, force_no_merger=False):
    """Anchor-calibrated daily model. Cumulative 'either' hits timing_anchors; each day's mass splits A/B by a_share."""
    start0 = P(cfg["window"]["start"]); end = P(cfg["window"]["end"])
    _n = dt.datetime.now(ET)   # dead-day rolloff: today closes at 8 PM ET, or at a scheduled event's end if later
    start = max(start0, today + dt.timedelta(days=1) if (today == _n.date() and _n.hour * 60 + _n.minute >= day_close_min(cfg, today)) else today)
    lp, la = logit(cfg["p_any_by_end"]["base"]), logit(cfg["a_share"]["base"])
    for s in cfg["signals"]:
        if s["active"]:
            if s["target"] == "p_any": lp += s["logodds"]
            else: la += s["logodds"]
    ld = cfg["leak_decay"]; LS = leak_state(cfg, today); leak_on = LS["on"]
    weeks_blank = LS["weeks_blank"]; leak_lo = LS["logodds"]
    la += leak_lo
    p_end, a_sh = sig(lp), (1e-9 if force_no_merger else sig(la))
    amult, alab = {}, {}
    def addA(ds, m, lab):
        amult[ds] = amult.get(ds, 1.0) * m; alab.setdefault(ds, []).append(lab)
    lk = cfg["lockups"]
    for t in lk["tranches"]:
        if t.get("blackout", True) is False: continue  # unconfirmed date: no model effect
        t0 = P(t["date"])
        addA(str(t0), lk["day_mult"], f"SPCX unlock {t['shares']/1e6:,.0f}M" + ("" if t["verified"] else " (est.)"))
        for k in (-1, 1):
            addA(str(t0 + dt.timedelta(days=k)), lk["near_mult"], "near SPCX unlock")
    ss, gr = events_state(cfg); s0 = P(ss["next_net"])
    addA(str(s0), ss["launch_mult"], "Starship launch (NET)")
    if ss["status"] in ("scheduled", "success"):
        addA(str(s0 + dt.timedelta(days=1)), ss["day_after_mult"], "day after Starship launch")
    if ss["status"] == "success":
        for k in range(ss["success_days"][0], ss["success_days"][1] + 1): addA(str(s0 + dt.timedelta(days=k)), ss["success_boost"], "after a successful Starship flight")
    if ss["status"] == "failure":
        for k in range(ss["failure_days"][0], ss["failure_days"][1] + 1): addA(str(s0 + dt.timedelta(days=k)), ss["failure_mult"], "Starship failure blackout")
    if gr["status"] == "pending":
        for k in range(gr["pending_within_days"]): addA(str(today + dt.timedelta(days=k)), gr["pending_mult"], "Grain spectrum deal pending FCC")
    elif gr["status"] == "approved" and gr.get("approved_on"):
        for k in range(1, gr["approval_days"] + 1): addA(str(P(gr["approved_on"]) + dt.timedelta(days=k)), gr["approval_boost"], "FCC approved Grain spectrum")
    days = []
    d = start0
    while d <= end:
        w = cfg["weekday_mult"][WD[d.weekday()]]; why = [f"{WD[d.weekday()]} x{w}"]
        um = 1.0
        for u in cfg["urgency"]:
            if P(u["from"]) <= d <= P(u["to"]): w *= u["mult"]; um *= u["mult"]; why.append(f"urgency x{u['mult']}")
        wa = wb = w; ev = []
        for bl in cfg["blackouts"] + cfg["post_event_boost"]:
            if bl["date"] == str(d):
                ev.append(bl["label"])
                if bl["applies"] in ("A", "both"): wa *= bl["mult"]
                if bl["applies"] in ("B", "both"): wb *= bl["mult"]
                why.append(f"{bl['label']} x{bl['mult']}" + ("" if bl["applies"] == "both" else f" ({bl['applies']} only)"))
        if str(d) in amult:
            wa *= amult[str(d)]; ev += alab[str(d)]; why.append(f"merger-only x{amult[str(d)]:.2f} ({'; '.join(alab[str(d)])})")
        if leak_on:
            ahead = (d - today).days
            for lz in ld["lead_days"]:
                if ahead < lz["within"]:
                    mm = 1 - (1 - lz["mult"]) * LS["soft"]
                    wa *= mm; why.append(f"no-leak lead x{mm:.2f} (merger)"); break
        days.append({"date": str(d), "wa": wa, "wb": wb, "u": um, "why": why, "events": ev})
        d += dt.timedelta(days=1)
    # cumulative anchors (absolute) from the window start
    anchors = [(P(x["date"]), x["share_of_total"] * p_end) for x in cfg["timing_anchors"]]
    def mix(x, ae): return ae * x["wa"] + (1 - ae) * x["wb"]
    def build(ae):
        prev_d, prev_c = start0 - dt.timedelta(days=1), 0.0
        for ad, ac in anchors:
            seg = [x for x in days if prev_d < P(x["date"]) <= ad]
            tot = sum(mix(x, ae) for x in seg) or 1
            for x in seg:
                m = (ac - prev_c) * mix(x, ae) / tot
                fa = ae * x["wa"] / (mix(x, ae) or 1)
                x["pA"], x["pB"] = m * fa, m * (1 - fa)
            prev_d, prev_c = ad, ac
        return sum(x["pA"] for x in days) / max(sum(x["pA"] + x["pB"] for x in days), 1e-9)
    lo, hi = 0.01, 0.99
    for _ in range(60):  # effective split so overall A share == a_sh despite A-only blackouts
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if build(mid) < a_sh else (lo, mid)
    build((lo + hi) / 2)
    if force_no_merger:
        for x in days: x["pB"] += x["pA"]; x["pA"] = 0.0
    if cfg.get("special_meeting_planned", {}).get("on") and not force_no_merger:
        # merger letter decoupled from the meeting deadlines: same total, timing by weekday x calendar only (no urgency/anchors)
        TA = sum(x["pA"] for x in days); wsum = sum(x["wa"] / x["u"] for x in days) or 1
        cE = cW = cA = cB = 0.0
        for x in days:
            cE += x["pA"] + x["pB"]; cW += TA * (x["wa"] / x["u"]) / wsum
            nB = max(cB, cE - min(cW, cE)); nA = cE - nB      # either curve unchanged; letter timing unforced, capped by it
            x["pA"], x["pB"] = nA - cA, nB - cB; cA, cB = nA, nB
    # path separation + missed-card switch: merger mass moved to 'routine date now, merger letter later (2027 special)'
    for x in days: x["pL"] = 0.0
    PS, MC = cfg.get("path_separation", {}), cfg.get("missed_card_switch", {})
    sep_on = PS.get("on") and not force_no_merger and not cfg.get("special_meeting_planned", {}).get("on")
    later = {"on": bool(sep_on), "mc_status": "off", "mc_shift": 0.0, "mc_projected": 0.0}
    if sep_on:
        br_ = blank_reasons(cfg, today)
        act = sum(r["p"] for r in br_ if r["id"] == "merger_hold"); qu = sum(r["p"] for r in br_ if r["id"] == "quiet_hold")
        act_sh = act / (act + qu) if PS.get("quiet_hold_no_deadline") and act + qu > 0 else 1.0
        ds = P(PS["decay_start"]); later["active_share"] = act_sh
        for x in days:
            n = (P(x["date"]) - ds).days
            if n > 0:
                keep = act_sh * math.exp(-PS["merger_decay_per_day"] * n)
                mv = x["pA"] * (1 - keep); x["pA"] -= mv; x["pB"] += mv; x["pL"] += mv
                x["why"].append(f"post-Oct 26 merger x{keep:.2f} (active-process share {act_sh:.2f}, decay); rest -> routine now + letter later")
        if MC.get("on"):
            st_ = load("state.json", {"state": "pre"}).get("state", "pre")
            act_d = P(MC["activate_on"]); sh = MC["shift_share"]
            elig = [x for x in days if P(x["date"]) >= max(act_d, start)]
            if st_ == "letter": later["mc_status"] = "disarmed (merger filing found)"
            elif today >= act_d:
                later["mc_status"] = "active"
                for x in elig:
                    mv = x["pA"] * sh; x["pA"] -= mv; x["pB"] += mv; x["pL"] += mv; later["mc_shift"] += mv
                    x["why"].append(f"missed-card switch: merger x{1-sh:.2f}")
            else:
                later["mc_status"] = "armed"; later["mc_projected"] = sh * sum(x["pA"] for x in elig)
    # condition on no announcement before `today` (window start is past): rescale remaining days
    past = [x for x in days if P(x["date"]) < start]; days = [x for x in days if P(x["date"]) >= start]
    gone = sum(x["pA"] + x["pB"] for x in past)
    if gone > 0:
        for x in days: x["pA"] /= (1 - gone); x["pB"] /= (1 - gone); x["pL"] /= (1 - gone)
        later["mc_shift"] /= (1 - gone); later["mc_projected"] /= (1 - gone)
    cA = cB = 0
    for x in days:
        cA += x["pA"]; cB += x["pB"]; x["cumA"], x["cumB"], x["none"] = cA, cB, 1 - cA - cB
    weeks = {}
    for x in days:
        dd = P(x["date"]); mon = dd - dt.timedelta(days=dd.weekday())
        w = weeks.setdefault(str(mon), {"monday": str(mon), "friday": str(mon + dt.timedelta(days=4)), "first_day": x["date"],
                                        "A": 0, "B": 0, "days": {k: {"A": 0, "B": 0, "date": None} for k in ["Mon", "Tue", "Wed", "Thu", "Fri", "Wknd"]}})
        k = WD[dd.weekday()] if dd.weekday() < 5 else "Wknd"
        w["days"][k]["A"] += x["pA"]; w["days"][k]["B"] += x["pB"]
        if dd.weekday() < 5: w["days"][k]["date"] = x["date"]
        w["A"] += x["pA"]; w["B"] += x["pB"]; w["cumA"], w["cumB"], w["none"] = x["cumA"], x["cumB"], x["none"]
    W = list(weeks.values())
    for w in W:
        w["either"] = w["A"] + w["B"]; w["cum"] = w["cumA"] + w["cumB"]
        for v in w["days"].values(): v["either"] = v["A"] + v["B"]
        w["mon"], w["tue"] = w["days"]["Mon"]["either"], w["days"]["Tue"]["either"]
    best = max(W, key=lambda w: w["either"])["monday"] if W else None
    later["p"] = sum(x["pL"] for x in days)
    for w in W: w["L"] = sum(x["pL"] for x in days if w["monday"] <= x["date"] <= str(P(w["monday"]) + dt.timedelta(days=6)))
    return {"later": later, "days": days, "weeks": W, "best_week": best, "p_end": p_end, "a_share": a_sh, "leak_logodds": leak_lo, "weeks_blank": weeks_blank, "leak": LS, "starship": ss, "grain": gr,
            "totals": {"A": days[-1]["cumA"], "B": days[-1]["cumB"], "none": days[-1]["none"]} if days else {}}

def metrics(m):
    out = {f"end_{k}": v for k, v in m["totals"].items()}
    for w in m["weeks"]:
        out[f"wk{w['monday']}_either"] = w["either"]; out[f"wk{w['monday']}_A"] = w["A"]; out[f"wk{w['monday']}_B"] = w["B"]
    return out

def drivers_diff(old, new):
    out = []
    os_ = {x["id"]: x for x in old["signals"]}
    for x in new["signals"]:
        o = os_.get(x["id"])
        if not o: out.append(f"new signal: {x['label']}")
        elif o["active"] != x["active"]: out.append(f"signal {'ON' if x['active'] else 'OFF'}: {x['label']} ({x['logodds']:+})")
        elif o["logodds"] != x["logodds"]: out.append(f"signal weight changed: {x['label']}")
    for k in ("p_any_by_end", "a_share"):
        if old[k]["base"] != new[k]["base"]: out.append(f"{k} prior {old[k]['base']} -> {new[k]['base']}")
    for k in ("urgency", "weekday_mult", "blackouts", "post_event_boost", "window", "timing_anchors", "lockups", "starship", "leak_decay", "grain", "special_meeting_planned", "blank_reasons"):
        if old.get(k) != new.get(k): out.append(f"calendar/config changed: {k}")
    return out

def fetch_shares(live):
    import requests
    r = requests.get("https://data.sec.gov/api/xbrl/companyconcept/CIK0001318605/dei/EntityCommonStockSharesOutstanding.json", headers=UA, timeout=20).json()
    last = r["units"]["shares"][-1]
    live["shares"] = {"TSLA": {"shares": last["val"], "as_of": last["end"], "form": last["form"], "filed": last["filed"], "src": "SEC XBRL dei:EntityCommonStockSharesOutstanding"}}

def fetch_spcx_shares(live):
    import requests
    r = requests.get("https://data.sec.gov/api/xbrl/companyconcept/CIK0001181412/dei/EntityCommonStockSharesOutstanding.json", headers=UA, timeout=20)
    if r.status_code != 200: return   # not published as a single dei total for the dual-class SPCX; config total is used
    rows = r.json()["units"]["shares"]; last = max(x["end"] for x in rows)
    tot = sum(x["val"] for x in rows if x["end"] == last and x.get("form") in ("10-Q", "10-K"))
    if tot > 1e9: live.setdefault("shares", {})["SPCX"] = {"shares": tot, "as_of": last, "src": "SEC XBRL dei (sum of classes)"}

def fetch_polymarket(live, cfg):
    import requests
    d = requests.get("https://gamma-api.polymarket.com/public-search", params={"q": cfg["polymarket"]["query"], "limit_per_type": 20}, timeout=20).json()
    rows = []
    for e in d.get("events", []):
        for m in e.get("markets", []):
            q = m.get("question", "")
            if "Tesla" not in q or "SpaceX" not in q: continue
            pr = json.loads(m.get("outcomePrices") or "[]")
            rows.append({"question": q, "yes": float(pr[0]) if pr else None, "closed": bool(m.get("closed")),
                         "volume": float(m.get("volume") or 0), "volume_24h": float(m.get("volume24hr") or 0),
                         "end": (m.get("endDate") or "")[:10], "url": f"https://polymarket.com/event/{e.get('slug')}"})
    rows.sort(key=lambda r: r["end"])
    live["polymarket"] = {"as_of": now_et(), "markets": rows}

def grain_check(live, cfg):
    """Heuristic FCC approval detection; writes events_state.json so it counts as news."""
    hits = [f for f in live.get("fcc", {}).get("filings", []) if re.search(r"order|grant|consent|approv", f["type"], re.I)]
    import requests, xml.etree.ElementTree as X
    rss = requests.get("https://news.google.com/rss/search", params={"q": "FCC approves SpaceX Grain spectrum", "hl": "en-US", "gl": "US", "ceid": "US:en"}, timeout=20).text
    for it in X.fromstring(rss).iter("item"):
        t = it.findtext("title") or ""
        if re.search(r"\bFCC\b.*\b(approv\w*|grant\w*|clear\w*|consent\w*)", t) and re.search(r"Grain|800 ?MHz", t) and not re.search(r"seek|ask|request|could|may|would", t, re.I):
            hits.append({"type": "news", "title": t, "url": it.findtext("link")})
    live["grain_check"] = {"as_of": now_et(), "approval_hits": hits[:5]}
    ev = load("events_state.json", {})
    # news-only approval wording is shown for review but no longer auto-applied; trackers.py applies FCC orders from ECFS

def fetch_fcc(live, cfg):
    import requests
    f = cfg["fcc_watch"]
    r = requests.get(f["api"], params={"api_key": "DEMO_KEY", "q": f["query"], "sort": "date_disseminated,DESC", "limit": 10}, timeout=20).json()
    rows = [{"date": x["date_disseminated"][:10], "proceedings": [p["name"] for p in x.get("proceedings", [])],
             "filers": [p["name"] for p in x.get("filers", [])], "type": x["submissiontype"]["description"],
             "url": (x.get("documents") or [{}])[0].get("src")} for x in r.get("filing", []) if x["date_disseminated"][:10] >= f["since"]
             and (re.search(r"grain|spacex|space exploration|starlink", " ".join(p["name"] for p in x.get("filers", [])), re.I)
                  or any(p["name"] == "25-178" for p in x.get("proceedings", [])))]
    live["fcc"] = {"as_of": now_et(), "filings": rows, "note": f["note"]}

def deal_math(cfg, live):
    pr = live.get("prices", {}); g = cfg["deal_math"]
    if not pr: return None
    T, S = pr["TSLA"]["close"], pr["SPCX"]["close"]
    tsh = live.get("shares", {}).get("TSLA", {}).get("shares", 3949547394)
    ssh = live.get("shares", {}).get("SPCX", {}).get("shares") or (g["spcx_class_a"] + g["spcx_class_b"])
    V = 10 * g["spcx_class_b"] + g["spcx_class_a"]
    mv, mts, mss = g["musk_spcx_votes"], g["musk_tsla_shares"], g["musk_spcx_shares_outstanding"]
    def case(label, ratio):
        n = tsh * ratio
        spx_vote = (mv + mts * ratio) / (V + n)                    # SpaceX survives (dual class)
        tsla_econ = (mts + mss / ratio) / (tsh + ssh / ratio)      # Tesla survives (single class: votes = shares)
        price = ratio * S
        return {"label": label, "ratio": ratio, "tsla_price": price, "premium": price / T - 1, "new_spcx_shares": n,
                "tesla_holders_pct": n / (ssh + n), "musk_votes_spacex_survives": spx_vote, "musk_votes_tesla_survives": tsla_econ,
                "passes_floor": round(price, 2) >= g["vote_floor"] - 0.01, "control_ok": spx_vote > 0.5, "survivor": "SpaceX"}
    r0 = T / S
    cases = [case("At market (0%)" if p == 0 else f"{int(p*100)}% premium", r0 * (1 + p)) for p in g["premiums"]]
    cases += [case(f"${x}/TSLA", x / S) for x in g["fixed_prices"]]
    cases.append(case("1:1 market-cap parity (50/50)", ssh / tsh))
    rv = case("Reverse: Tesla acquires SpaceX (at market)", r0)
    rv.update({"survivor": "Tesla", "control_ok": rv["musk_votes_tesla_survives"] > 0.5, "low_probability": True})
    cases.append(rv)
    par = next(c for c in cases if c["label"].startswith("1:1"))
    gap = {"implied_ratio": r0, "parity_ratio": ssh / tsh, "parity_price": ssh / tsh * S, "parity_premium": ssh / tsh * S / T - 1,
           "cap_gap": S * ssh - T * tsh, "ratio_gap": ssh / tsh - r0}
    return {"gap": gap, "tsla_price": T, "spcx_price": S, "tsla_shares": tsh, "spcx_shares": ssh, "tsla_cap": T * tsh, "spcx_cap": S * ssh,
            "musk_votes_now": mv / V, "cases": cases, "floor": g["vote_floor"],
            "notes": [g["_spcx_src"], g["_musk_votes_why"], g["_musk_tsla_src"]],
            "parity_note": f"1:1 market-cap parity works out to about ${par['tsla_price']:.0f} per TSLA at current prices ({par['premium']:+.0%}), which clears the ${g['vote_floor']} floor.",
            "collar_note": g["collar_note"], "reverse_note": g["reverse_note"],
            "spcx_shares_src": live.get("shares", {}).get("SPCX", {}).get("src", "config (10-Q cover + Cursor shares)"),
            "prices_as_of": (f"last trade {pr['TSLA']['market_time']}, fetched {live.get('prices_as_of')}" if pr.get("TSLA", {}).get("market_time") else live.get("prices_as_of"))}

def blank_reasons(cfg, today):
    b = cfg["blank_reasons"]; wk = max(0, (today - P(b["ref_date"])).days // 7)
    ws = [(x, x["w"] * x["weekly"] ** wk) for x in b["items"]]
    tot = sum(w for _, w in ws)
    return sorted([{"id": x["id"], "label": x["label"], "p": w / tot, "src": x.get("src"), "note": (x.get("note") or "").replace("{checked}", now_et()) or None, "merger": x.get("merger", False)} for x, w in ws], key=lambda r: -r["p"])

def make_why(cfg, m, br, today, state):
    """WHY bullets generated from the same numbers as the ranked reasons and the headline, so they can't drift."""
    T = m["totals"]; L = m["leak"]; sh = T["A"] / max(T["A"] + T["B"], 1e-9)
    mer = sum(r["p"] for r in br if r["merger"]); top = br[:3]
    out = []
    if state["state"] == "meeting_only":
        out.append("Tesla set a routine 2026 meeting with no SpaceX language. The 2026 merger path is closed, and any letter now points to a 2027 special meeting.")
    else:
        out.append(f"No 2026 meeting date yet. The clock started {fmt(L['start'])}, and {L['weeks_blank']} full blank weeks have passed since.")
    out.append("Most likely reasons for the blank page: " + "; ".join(f"{r['label'][0].lower() + r['label'][1:]} {r['p']:.0%}" for r in top)
               + f". Merger-related reasons add up to {mer:.0%}, and non-merger reasons to {1 - mer:.0%}.")
    out.append(f"No committee, bankers or law firms have leaked. That costs the merger share {L['raw']:.2f} log-odds, softened to {L['logodds']:.2f}, "
               f"because the quiet-hold path ({next((r['p'] for r in br if r['id']=='quiet_hold'), 0):.0%}) wouldn't leak.")
    out.append(f"Deadlines force some announcement ({m['p_end']:.0%} by Dec 14): the e-proxy cutoff is ~Oct 27 and the last Texas 10-day notice is Nov 26. "
               f"By Dec 14: {T['A']:.0%} meeting with a merger, {T['B']:.0%} routine, {T['none']:.0%} nothing, so {sh:.0%} of announcements come with a merger.")
    out.append("Dead days roll off: a day with no filing closes at 8 PM ET and its odds move to the remaining open days. On a scheduled Tesla/SpaceX event day (e.g. Roadster, Oct 15, 8:55 PM ET) the day stays open until the event ends (default start + 2.5h), since an on-stage or X statement that night with an 8-K next morning counts as that day; after that the residual rolls to the next open morning.")
    out.append("Mondays are likeliest, then Tuesdays. Merger news should avoid SpaceX unlock days, Starship launch plus the day after, and the Oct 15-22 Roadster/earnings stretch, Oct 16 included.")
    LT = m.get("later", {})
    if LT.get("on"):
        out.append(f"Merger and routine now carry separate deadline pressure. After Oct 26, merger odds keep only the active-process share ({LT.get('active_share',0):.0%}) and decay daily; the quiet-hold path gets no deadline pressure. That moves {LT.get('p',0):.1%} to 'routine date now, merger letter later (2027 special meeting)'. Missed-card switch: {LT.get('mc_status')}.")
    return out

def fmt(s): d = P(s); return d.strftime("%b ") + str(d.day)

def main():
    cfg, jud = load("config.json"), load("judgment.json")
    q3 = load("events_state.json", {}).get("spcx_q3") or {}
    if q3.get("confirmed"):   # trackers.py found SpaceX's Q3 date in an SEC filing: confirm the 28% tranche and turn its blackout on
        for t in cfg["lockups"]["tranches"]:
            if t.get("blackout") is False and "Q3" in t["label"]:
                t.update({"date": q3["unlock_date"], "blackout": True, "verified": True,
                          "note": f"CONFIRMED by tracker: SpaceX Q3 results {q3['release_date']} ({q3['timing']}-market), 2nd full Nasdaq trading day after = {q3['unlock_date']}. Source: {q3.get('url')}"})
    live = load("live.json", {})
    news = load("news.json", {"days": []})
    if not OFFLINE:
        for f in (fetch_prices, fetch_options, lambda l: fetch_edgar(l, cfg["alerts"]["watch_forms"])):
            try: f(live)
            except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} {getattr(f, '__name__', 'edgar')}: {ex}")
        try: fetch_news(news)
        except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} news: {ex}")
        live["errors"] = live.get("errors", [])[-10:]
        live.pop("markets", None); live.pop("markets_as_of", None)
        save("live.json", live)
    today = dt.datetime.now(ET).date()
    state = load("state.json", {"state": "pre", "since": "2026-10-13"})
    if not OFFLINE:
        try:
            import statecheck
            new_state, hits = statecheck.check(state)
            if new_state["state"] != state["state"]:
                live.setdefault("state_changes", []).append({"at": now_et(), "from": state["state"], "to": new_state["state"], "evidence": new_state.get("evidence")})
            state = new_state; save("state.json", state)
        except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} statecheck: {ex}")
        try: fetch_shares(live)
        except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} shares: {ex}")
        for nm, fn in (("fcc", lambda: fetch_fcc(live, cfg)), ("grain", lambda: grain_check(live, cfg)), ("spcx_shares", lambda: fetch_spcx_shares(live)), ("polymarket", lambda: fetch_polymarket(live, cfg))):
            try: fn()
            except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} {nm}: {ex}")
        save("live.json", live)
    m = compute(cfg, today)
    pre_kill = m
    kill = None
    if state["state"] == "meeting_only":   # kill switch: 2026 merger path closed, bars re-weighted, letter moves to a 2027 special meeting
        m = compute(cfg, today, force_no_merger=True)
        lk = cfg["letter_after_kill"]
        kill = {"letter_by": lk["by"], "p_letter": lk["mult_of_prior"] * pre_kill["a_share"], "why": lk["_why"]}
        m["totals"] = {"A": 0.0, "B": 1.0, "none": 0.0}
    alt = compute({**cfg, "special_meeting_planned": {**cfg["special_meeting_planned"], "on": not cfg["special_meeting_planned"]["on"]}}, today)
    dm = deal_math(cfg, live)
    br = blank_reasons(cfg, today)
    why = make_why(cfg, m, br, today, state)
    exps = sorted(o["expiry"] for o in live.get("options", []) if "expiry" in o)
    ivs = {o["expiry"]: o.get("atm_iv") for o in live.get("options", []) if "expiry" in o}
    for w in m["weeks"] + alt["weeks"]:
        fri = w["friday"]  # Friday weekly that covers a Mon/Tue announcement that week
        e = fri if fri in ivs else next((x for x in exps if w["first_day"] <= x <= fri), None)
        w["expiry"] = fri; w["expiry_listed"] = fri in ivs; w["expiry_iv"] = ivs.get(fri)
    # history + news/time split
    hist = load("history.json", [])
    hist = [h for h in hist if h.get("v") == 2]           # old-format snapshots dropped on redesign
    inp = inputs_of(cfg, jud); met = metrics(m)
    prev = next((h for h in reversed(hist) if h["date"] < str(today)), None)
    split, drivers = {}, []
    if prev:
        cf = metrics(compute({**cfg, **prev["inputs"]}, today))
        drivers = drivers_diff(prev["inputs"], inp)
        for k, v in met.items():
            if k in prev["metrics"]:
                split[k] = {"total": round((v - prev["metrics"][k]) * 100, 2), "time": round((cf[k] - prev["metrics"][k]) * 100, 2),
                            "news": round((v - cf[k]) * 100, 2)}
    hist.append({"v": 2, "date": str(today), "as_of": now_et(), "metrics": met, "inputs": inp})
    hist = [h for i, h in enumerate(hist) if h["date"] != str(today) or i == len(hist) - 1]
    save("history.json", hist[-200:])
    news_moved = any(abs(v["news"]) > 0.005 for v in split.values())
    day = next((d for d in news["days"] if d["date"] == str(today)), None)
    if not day:
        day = {"date": str(today), "odds_note": "", "items": []}; news["days"].insert(0, day)
    if prev:
        e = lambda k: split.get(k, {"total": 0, "time": 0, "news": 0})
        day["odds_note"] = ("By Dec 14: with merger {:+.1f} pts (time {:+.1f}, news {:+.1f}); routine {:+.1f} (time {:+.1f}, news {:+.1f}); nothing {:+.1f}. "
                            .format(e("end_A")["total"], e("end_A")["time"], e("end_A")["news"], e("end_B")["total"], e("end_B")["time"], e("end_B")["news"], e("end_none")["total"])
                            + ("Why: " + "; ".join(drivers) + "." if drivers else "No new information, so this is just time passing."))
        day["split"] = {k: split[k] for k in ("end_A", "end_B", "end_none") if k in split}
    news["days"].sort(key=lambda d: d["date"], reverse=True); save("news.json", news)
    try: odds_history(cfg, today, inp, prev, day)
    except Exception as ex: live.setdefault("errors", []).append(f"{now_et()} odds_history: {ex}")
    alerts = []
    if news_moved:
        alerts.append({"type": "news_driven_change", "drivers": drivers, "headlines": [i["title"] for i in day["items"]],
                       "text": "News-driven change: " + ", ".join(f"{k} {split[k]['news']:+.1f}pts" for k in ("end_A", "end_B", "end_none") if k in split)})
    for co, fl in live.get("edgar", {}).items():
        for f in fl:
            if f["watch"] and f["date"] >= str(today - dt.timedelta(days=1)):
                alerts.append({"type": "filing", "text": f"{co} filed {f['form']} on {f['date']}", "url": f["url"]})
    model = {"morning_run": (str(today) if "--morning" in sys.argv else load("model.json", {}).get("morning_run")), "generated": now_et(), "today": str(today), "window": cfg["window"], "weeks": m["weeks"], "best_week": m["best_week"],
             "totals": m["totals"], "p_end": m["p_end"], "a_share": m["a_share"], "split": split, "drivers": drivers, "alerts": alerts,
             "prev_as_of": prev["as_of"] if prev else None,
             "state": state, "deal_math": dm, "blank_reasons": br, "leak_logodds": m["leak_logodds"], "weeks_blank": m["weeks_blank"],
             "fcc": live.get("fcc"), "shares": live.get("shares"), "why": why, "kill": kill, "leak": m["leak"],
             "starship": m["starship"], "grain": m["grain"], "grain_check": live.get("grain_check"), "polymarket": live.get("polymarket"),
             "lockups_eff": cfg["lockups"], "trackers": {k: (v if k == "log" or not isinstance(v, dict) else {**v, "value": {kk: vv for kk, vv in (v.get("value") or {}).items() if kk != "seen_ids"}}) for k, v in load("trackers.json", {}).items()},
             "later": m["later"], "special_meeting": {"on": cfg["special_meeting_planned"]["on"], "alt_totals": alt["totals"], "alt_later": alt["later"],
                                 "alt_weeks": [{"monday": w["monday"], "A": w["A"], "B": w["B"], "either": w["either"], "cum": w["cum"]} for w in alt["weeks"]]},
             "merger_share_of_announcements": (m["totals"]["A"] / max(m["totals"]["A"] + m["totals"]["B"], 1e-9)) if m["totals"] else None,
             "daily": [{k: x[k] for k in ("date", "cumA", "cumB", "none", "pA", "pB", "pL", "why", "events")} for x in m["days"]]}
    def scen(mm):
        T = mm["totals"]
        hz = cfg.get("headline_horizon", "2026-11-26"); dd = [x for x in mm["days"] if x["date"] <= hz]
        a = sum(x["pA"] for x in dd); b = sum(x["pB"] for x in dd)
        Lp = sum(x["pL"] for x in dd) if mm["later"].get("on") else 0.0
        hl = {"horizon": hz, "X": a + Lp, "A": a, "B": Lp, "C": b - Lp, "slip": (T["A"] + T["B"]) - (a + b), "D": T["none"]}
        return {"headline": hl, "totals": T, "weeks": mm["weeks"], "best_week": mm["best_week"], "later": mm["later"],
                "merger_share_of_announcements": (T["A"] / max(T["A"] + T["B"], 1e-9)) if T else None,
                "why": make_why(cfg, mm, br, today, state),
                "daily": [{k: x[k] for k in ("date", "cumA", "cumB", "none", "pA", "pB", "pL", "why", "events")} for x in mm["days"]]}
    on_cfg = cfg["special_meeting_planned"]["on"]
    if state["state"] == "pre":
        model["scenarios"] = {("on" if on_cfg else "off"): scen(m), ("off" if on_cfg else "on"): scen(alt), "default": "on" if on_cfg else "off"}
    save("model.json", model)
    print(json.dumps({"totals": m["totals"], "best_week": m["best_week"], "a_share": m["a_share"],
                      "weeks": [(w["monday"], "either", round(w["either"]*100, 1), "A", round(w["A"]*100, 1), "B", round(w["B"]*100, 1), "cum", round(w["cum"]*100, 1), w["expiry"], {k: (round(v["either"]*100, 1), round(v["A"]*100, 1), round(v["B"]*100, 1)) for k, v in w["days"].items()}) for w in m["weeks"]],
                      "alerts": alerts}, indent=1))

if __name__ == "__main__":
    main()
