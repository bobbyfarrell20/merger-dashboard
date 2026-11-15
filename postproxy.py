"""Post-proxy model (state meeting_only): merger letter by Jun 30, 2027 via a separate announcement + special meeting.
Dashboard number = (1-w) * rules model + w * prediction-market composite (+ smart-money nudge). Pure functions; update.py saves."""
import json, math, statistics, datetime as dt
from zoneinfo import ZoneInfo
from pathlib import Path
ET = ZoneInfo("America/New_York"); D = Path(__file__).parent / "data"
P = lambda s: dt.date.fromisoformat(s)

def load(n, d=None):
    p = D / n
    return json.loads(p.read_text()) if p.exists() else d

def rules_daily(pp, catalysts):
    """Unconditional daily density from start to `by`, summing to rules_total_from_start."""
    s, e = P(pp["start"]), P(pp["by"]); mw = pp["month_weights"]; days = []
    mult = {}
    for c in catalysts:
        d = P(c["date"])
        while d <= P(c.get("end") or c["date"]):
            mult[str(d)] = mult.get(str(d), 1.0) * c.get("mult", 1.0); d += dt.timedelta(days=1)
    d = s
    while d <= e:
        ym = d.strftime("%Y-%m"); ndays = sum(1 for k in range(40) if (d.replace(day=1) + dt.timedelta(days=k)).strftime("%Y-%m") == ym)
        if ym == s.strftime("%Y-%m"): ndays -= s.day - 1
        wk = 0.35 if d.weekday() >= 5 else 1.0
        days.append([str(d), mw.get(ym, 0) / ndays * wk * mult.get(str(d), 1.0)])
        d += dt.timedelta(days=1)
    # renormalise per month so catalyst/weekend weights move odds within a month, not between months
    bym = {}
    for x in days: bym.setdefault(x[0][:7], []).append(x)
    tot_w = sum(mw.get(k, 0) for k in bym)
    for k, xs in bym.items():
        sw = sum(x[1] for x in xs) or 1
        for x in xs: x[1] = x[1] / sw * mw.get(k, 0) / tot_w * pp["rules_total_from_start"]
    return days

def iso_fit(pts):
    """Weighted pool-adjacent-violators: non-decreasing p over date."""
    blocks = [[p["p"] * p["w"], p["w"], [p]] for p in sorted(pts, key=lambda x: x["date"])]
    i = 0
    while i < len(blocks) - 1:
        if blocks[i][0] / blocks[i][1] > blocks[i + 1][0] / blocks[i + 1][1]:
            a, b = blocks[i], blocks.pop(i + 1); a[0] += b[0]; a[1] += b[1]; a[2] += b[2]; i = max(0, i - 1)
        else: i += 1
    out = []
    for s, w, ps in blocks:
        for p in ps: out.append({"date": p["date"], "fit": s / w})
    return out

def contract_date(name):
    import re
    m = re.search(r"(?:by|Before) ([A-Z][a-z]+ \d{1,2}, \d{4}|\d{4})$", name)
    if not m: return None
    t = m.group(1)
    if re.fullmatch(r"\d{4}", t): return str(dt.date(int(t), 1, 1) - dt.timedelta(days=1))
    try: d = dt.datetime.strptime(t, "%B %d, %Y").date()
    except ValueError: d = dt.datetime.strptime(t, "%b %d, %Y").date()
    return str(d - dt.timedelta(days=1)) if "Before" in name else str(d)

def quality(r, mk):
    """Liquidity quality 0..1 of one contract quote: 24h volume (saturating) x spread tightness.
    A last-trade quote (book too wide) counts at last_factor of a tight book; a thin quote counts 0."""
    v = r.get("vol24") or 0; s_vol = v / (v + mk["vol_half"])
    if r.get("how") == "mid" and r.get("bid") is not None and r.get("ask") is not None:
        s_sp = min(1.0, mk["spread_full"] / max(r["ask"] - r["bid"], 0.005))
    elif r.get("how") == "last": s_sp = mk["last_factor"]
    else: s_sp = 0.0
    return s_vol * s_sp

def _lg(p): p = min(max(p, 0.005), 0.995); return math.log(p / (1 - p))
def _sg(x): return 1 / (1 + math.exp(-x))

def damp(rows, mk):
    """Dampen unconfirmed single-contract moves instead of excluding them.
    Move m = logit(price) - logit(ref), ref = median of the contract's own quotes over the past damp_hours.
    Confirmation c = liquidity-weighted share of that move the other live contracts made too (0..1).
    Kept share k = c + (1 - c) x solo, solo = solo_max x clamp(q / damp_full_q, damp_min, 1).
    Moves under damp_ignore (logit) pass untouched, so ordinary drift never lags."""
    live = [r for r in rows if r.get("ref") is not None and r["p"] is not None and not r.get("expired")]
    for r in live: r["_m"] = _lg(r["p"]) - _lg(r["ref"])
    for r in live:
        m = r["_m"]
        if abs(m) < mk["damp_ignore"]:
            r["k"], r["conf"] = 1.0, None; r["p_used"] = r["p"]; continue
        peers = [x for x in live if x is not r and not x.get("stale")]
        wsum = sum(x["q"] for x in peers)
        c = sum(x["q"] * min(1.0, max(0.0, x["_m"] / m)) for x in peers) / wsum if wsum else 0.0
        solo = mk["solo_max"] * min(1.0, max(mk["damp_min"], r["q"] / mk["damp_full_q"]))
        k = c + (1 - c) * solo
        r["k"], r["conf"] = round(k, 3), round(c, 3); r["p_used"] = round(_sg(_lg(r["ref"]) + k * m), 4)
    for r in rows:
        r.pop("_m", None)
        if "p_used" not in r and r.get("p") is not None: r["p_used"] = r["p"]

def table(mh, news, sm, now, pp=None):
    """Per-contract stats from the watcher's history + smart-money flags (+ liquidity quality, damping, staleness)."""
    import dataguard
    mk = (pp or {}).get("market", {}); v2 = mk.get("method") == "liquidity_v2"; gcfg = {"data_guard": (pp or {}).get("data_guard")}
    newest = max((h["points"][-1]["t"] for h in mh.values() if h.get("points")), default=None, key=lambda t: dt.datetime.fromisoformat(t))
    rows = []
    def at(pts, hours):
        tgt = now - dt.timedelta(hours=hours); older = [x for x in pts if dt.datetime.fromisoformat(x["t"]) <= tgt]
        return older[-1] if older else None
    expl = explained_windows(news)
    for cid, h in mh.items():
        pts = h["points"]
        if not pts: continue
        last = pts[-1]; d24, d7 = at(pts, 24), at(pts, 24 * 7); d72 = at(pts, sm["nudge_window_hours"]) or pts[0]
        ch24 = (last["p"] - d24["p"]) * 100 if d24 else None
        ch7 = (last["p"] - d7["p"]) * 100 if d7 else ((last["p"] - pts[0]["p"]) * 100)
        ch72 = (last["p"] - d72["p"]) * 100
        t_last = dt.datetime.fromisoformat(last["t"])
        news24 = expl(t_last - dt.timedelta(hours=24), t_last); news72 = expl(t_last - dt.timedelta(hours=sm["nudge_window_hours"]), t_last)
        vol = last.get("vol24") or 0
        flag = ch24 is not None and abs(ch24) >= sm["flag_pp"] and vol >= sm["flag_min_vol24"] and not news24
        nud = abs(ch72) >= sm["nudge_pp"] and vol >= sm["flag_min_vol24"] and not news72
        since = [x for x in pts if dt.datetime.fromisoformat(x["t"]) >= now - dt.timedelta(days=7)]
        rows.append({"id": cid, "venue": cid.split(":")[0].capitalize(), "name": h["name"], "short": h["name"].split(": ", 1)[1],
                     "date": contract_date(h["name"]), "p": last["p"], "how": last.get("how"), "bid": last.get("bid"), "ask": last.get("ask"),
                     "last": last.get("last"), "vol24": vol, "ch24": ch24, "ch7": ch7, "ch72": ch72, "as_of": last["t"],
                     "spark": [round(x["p"], 4) for x in since][-60:], "flag": flag, "nudge_ok": nud,
                     "explained_by": news24[:1] if news24 else [], "url": h.get("url")})
        r = rows[-1]; d = r["date"]
        r["expired"] = bool(d and d < str(now.date()))
        r["stale"] = "contract expired" if r["expired"] else dataguard.quote_staleness(last["t"], newest, now, gcfg)
        if v2:
            q = quality(r, mk); r["q"] = round(q, 3)
            win = [x["p"] for x in pts[:-1] if t_last - dt.timedelta(hours=mk["damp_hours"]) <= dt.datetime.fromisoformat(x["t"]) < t_last]
            ref = statistics.median(win) if win else last["p"]
            r["ref"] = round(ref, 4)
    if v2: damp(rows, mk)
    rows.sort(key=lambda r: (r["date"] or "9999", r["venue"]))
    return rows

def explained_windows(news):
    """Items that count as news: SEC filings, items tagged with an odds event, and the watcher's strict deal-headline hits."""
    items = []
    for d in news.get("days", []):
        for i in d["items"]:
            if i.get("factor") == "markets": continue
            if not (i.get("factor") == "sec" or i.get("odds_event") or (i.get("watch") and "prediction-market" not in (i.get("effect") or ""))): continue
            try: t = dt.datetime.fromisoformat(f"{d['date']}T{i.get('time_et') or '12:00'}").replace(tzinfo=ET)
            except Exception: continue
            items.append((t, i["title"]))
    return lambda a, b: [ti for t, ti in items if a <= t <= b]

def composite(rows, pp, today=None):
    mk = pp["market"]
    if mk.get("method") == "liquidity_v2":
        live = [r for r in rows if r["date"] and r["p"] is not None and not r.get("expired") and (today is None or r["date"] > str(today))]
        use = [r for r in live if not r.get("stale")] or live      # all stale: fall back to the last good quotes (flagged)
        pts = [{"date": r["date"], "p": r["p_used"], "w": max(r["q"], mk["min_q"]), "id": r["id"]} for r in use]
        for r in rows: r["in_fit"] = r in use; r["w_fit"] = round(max(r.get("q", 0), mk["min_q"]), 3) if r in use else 0.0
        tw = sum(p["w"] for p in pts) or 1
        for r in use: r["w_share"] = round(max(r["q"], mk["min_q"]) / tw, 3)
    else:
        pts = None
    if pts is None:
        pts = [{"date": r["date"], "p": r["p"], "w": max(r["vol24"] or 0, mk["min_vol_weight"]), "id": r["id"]} for r in rows if r["date"] and r["p"] is not None]
    if not pts: return []
    fit = iso_fit(pts)
    agg = {}
    for f, p in zip(fit, sorted(pts, key=lambda x: x["date"])):
        a = agg.setdefault(f["date"], [0, 0]); a[0] += f["fit"] * p["w"]; a[1] += p["w"]
    return sorted((d, s / w) for d, (s, w) in agg.items())

def market_depth(mh, now, today, days=3):
    """Median over the last `days` days of the total 24h volume across live contracts (USD)."""
    tots = []
    for k in range(days):
        tk = now - dt.timedelta(hours=24 * k); s = n = 0
        for h in mh.values():
            d = contract_date(h["name"])
            if not d or d <= str(today): continue
            pts = [p for p in h["points"] if tk - dt.timedelta(hours=12) <= dt.datetime.fromisoformat(p["t"]) <= tk]
            if pts: s += pts[-1].get("vol24") or 0; n += 1
        if n: tots.append(s)
    return statistics.median(tots) if tots else 0.0

def market_weight(pp, mh, now, today):
    """Fixed weight (legacy) or depth-scaled: w = min + (max - min) x L / (L + half), L = market_depth."""
    rule = pp.get("market_weight_rule")
    if not rule: return pp["market_weight"], None
    L = market_depth(mh, now, today, rule.get("days", 3))
    w = rule["min"] + (rule["max"] - rule["min"]) * L / (L + rule["vol_half"])
    return round(w, 2), L

def interp(curve, d, today):
    """Cumulative from today: 0 at today, linear between contract dates."""
    xs = [(str(today), 0.0)] + [c for c in curve if c[0] > str(today)]
    if d <= xs[0][0] or len(xs) == 1: return 0.0
    for (d0, p0), (d1, p1) in zip(xs, xs[1:]):
        if d <= d1:
            f = (P(d) - P(d0)).days / max(1, (P(d1) - P(d0)).days); return p0 + f * (p1 - p0)
    return xs[-1][1]

def third_fridays(today, by, expiries):
    out = []; y, m = today.year, today.month
    while True:
        d = dt.date(y, m, 15); d += dt.timedelta(days=(4 - d.weekday()) % 7)
        if d > P(by) + dt.timedelta(days=3): break
        if d >= today:
            near = [e for e in expiries if abs((P(e) - d).days) <= 1]
            out.append({"date": near[0] if near else str(d), "listed": bool(near)})
        m += 1
        if m > 12: y, m = y + 1, 1
    return out

def catalysts_live(cfg):
    ev = load("events_state.json", {}); pp = cfg["post_proxy"]; out = [dict(c) for c in pp["catalysts"]]
    q3 = ev.get("spcx_q3") or {}
    for c in out:
        if "SpaceX Q3" in c["label"] and q3.get("confirmed"): c["confirmed"] = True; c["date"] = c["end"] = q3["release_date"]
        if "28%" in c["label"] and q3.get("confirmed"): c["confirmed"] = True; c["date"] = c["end"] = q3["unlock_date"]
    ss = {**cfg["starship"], **{k: v for k, v in ev.get("starship", {}).items() if v is not None}}
    gr = {**cfg["grain"], **{k: v for k, v in ev.get("grain", {}).items() if v is not None}}
    if ss.get("next_net") and ss.get("status") == "scheduled":
        out.append({"date": ss["next_net"], "end": ss["next_net"], "label": "Starship Flight 15 (NET)", "confirmed": False, "mult": 0.5, "note": "Launch day is a poor day for a deal letter; a clean flight can help the days after."})
    out.append({"date": None, "end": None, "label": f"Grain 800 MHz FCC decision (docket 25-178): {gr.get('status')}", "confirmed": False, "mult": 1.0, "note": "No date. An approval would add Tesla-relevant spectrum to SpaceX."})
    return out

GROUPS = [("2026", "2026-12-31", "Dec 2026"), ("JF27", "2027-02-28", "Jan–Feb '27"), ("MA27", "2027-04-30", "Mar–Apr '27"),
          ("MID27", "2027-06-30", "Mid-2027"), ("LATE", "9999-12-31", "2027+")]

def chart_series(mh):
    """Backfill (pm_backfill.py) merged with the watcher's own log; watcher points win within 1h."""
    bf = load("markets_backfill.json", {}).get("series", {}); out = {}
    for cid in sorted(set(bf) | set(mh)):
        name = (mh.get(cid) or bf.get(cid))["name"]; d = contract_date(name)
        et = lambda t: dt.datetime.fromisoformat(t).astimezone(ET).strftime("%Y-%m-%dT%H:%M")   # one clock (ET) for both sources
        pts = {et(p["t"])[:13]: [et(p["t"]), round(p["p"], 4)] for p in bf.get(cid, {}).get("points", [])}
        for p in (mh.get(cid) or {}).get("points", []): pts[et(p["t"])[:13]] = [et(p["t"]), round(p["p"], 4)]
        g = next(k for k, lim, _ in GROUPS if (d or "9999") <= lim)
        out[cid] = {"name": name, "venue": cid.split(":")[0].capitalize(), "date": d, "group": g, "points": sorted(pts.values())}
    return {"groups": [{"id": k, "label": l} for k, _, l in GROUPS], "series": out,
            "backfill_src": "Polymarket CLOB prices-history (6h) and Kalshi candlesticks (hourly, sampled 4h), from Aug 20; watcher log every 15 min from Oct 13"}

def run(cfg, today, live, prev_hist=None):
    pp = cfg["post_proxy"]; sm = pp["smart_money"]; now = dt.datetime.now(ET)
    mh = load("markets_history.json", {}); news = load("news.json", {"days": []})
    cats = catalysts_live(cfg)
    dens = rules_daily(pp, [c for c in cats if c.get("date")])
    gone = sum(v for d, v in dens if d < str(today))
    rem = [(d, v / (1 - gone)) for d, v in dens if d >= str(today)]
    rules_cum = []; c = 0
    for d, v in rem: c += v; rules_cum.append((d, c))
    R = dict(rules_cum)
    rows = table(mh, news, sm, now, {**pp, "data_guard": cfg.get("data_guard")})
    curve = composite(rows, pp, today)
    w, depth = market_weight(pp, mh, now, today)
    issues = [f"{r['name']}: {r['stale']}" for r in rows if r.get("stale") and not r.get("expired")]
    if pp["market"].get("method") == "liquidity_v2" and rows and not any(r.get("in_fit") and not r.get("stale") for r in rows):
        issues.insert(0, "All prediction-market quotes are stale; composite uses the last good quotes")
    nudge_rows = [r for r in rows if r["nudge_ok"]]
    nudge = 0.0
    if nudge_rows:
        big = max(nudge_rows, key=lambda r: abs(r["ch72"]))
        nudge = max(-sm["nudge_cap_pp"], min(sm["nudge_cap_pp"], sm["nudge_share"] * big["ch72"])) / 100
    rules_cum_s = [(d, cv) for d, cv in rules_cum]
    series = [{"date": d, "rules": rv, "market": interp(curve, d, today), } for d, rv in rules_cum_s]
    for s in series:
        s["blend"] = min(0.99, max(0.0, (1 - w) * s["rules"] + w * s["market"] + nudge * (s["rules"] / max(rules_cum[-1][1], 1e-9))))
    S = {s["date"]: s for s in series}
    def at(d):
        d = min(d, series[-1]["date"]); return S.get(d) or series[-1]
    months = []
    for k in pp["month_weights"]:
        y, m = map(int, k.split("-")); e = (dt.date(y + (m == 12), m % 12 + 1, 1) - dt.timedelta(days=1))
        if e < today: continue
        e = min(e, P(pp["by"])); months.append({"date": str(e), "label": e.strftime("%b %Y"), **{x: at(str(e))[x] for x in ("rules", "market", "blend")}})
    exps = sorted(o["expiry"] for o in live.get("options", []) if "expiry" in o); ivs = {o["expiry"]: o.get("atm_iv") for o in live.get("options", []) if "expiry" in o}
    opex = [{"date": o["date"], "listed": o["listed"], "iv": ivs.get(o["date"]), **{x: at(o["date"])[x] for x in ("rules", "market", "blend")}} for o in third_fridays(today, pp["by"], exps)]
    # catalyst strip: blended chance the letter lands inside each catalyst window
    dmap = dict(rem); bl_scale = series[-1]["blend"] / max(rules_cum[-1][1], 1e-9)
    for cx in cats:
        if not cx.get("date") or cx["end"] < str(today): cx["p"] = None; continue
        dd = P(max(cx["date"], str(today))); e = P(cx["end"]); tot = 0
        while dd <= e: tot += dmap.get(str(dd), 0); dd += dt.timedelta(days=1)
        cx["p"] = tot * bl_scale
    cats.sort(key=lambda c: c["date"] or "9999")
    H = series[-1]; D = at(pp.get("near_by", "2026-12-31"))
    return {"by": pp["by"], "market_depth": depth, "data_issues": issues, "model_version": pp.get("model_version"),
            "headline": {"blend": H["blend"], "rules": H["rules"], "market": H["market"], "nudge": nudge, "market_weight": w,
                         "near": {"by": pp.get("near_by", "2026-12-31"), "blend": D["blend"], "rules": D["rules"], "market": D["market"]}},
            "series": series, "months": months, "opex": opex, "catalysts": cats, "markets": rows, "composite": curve,
            "flags": [r for r in rows if r["flag"]], "chart": chart_series(mh), "chart_events": pp.get("chart_events", []), "nudge_from": [r["name"] for r in nudge_rows],
            "rules_total_from_start": pp["rules_total_from_start"], "gone": gone, "docs": {k: pp[k] for k in pp if k.startswith("_")} | {"market": pp["market"]["_why"], "smart_money": sm["_why"]},
            "market_method": pp["market"].get("method", "volume_v1"), "market_weight_rule": pp.get("market_weight_rule")}
