"""Post-proxy model (state meeting_only): merger letter by Jun 30, 2027 via a separate announcement + special meeting.
Dashboard number = (1-w) * rules model + w * prediction-market composite (+ smart-money nudge). Pure functions; update.py saves."""
import json, math, datetime as dt
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

def table(mh, news, sm, now):
    """Per-contract stats from the watcher's history + smart-money flags."""
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

def composite(rows, pp):
    mk = pp["market"]
    pts = [{"date": r["date"], "p": r["p"], "w": max(r["vol24"] or 0, mk["min_vol_weight"]), "id": r["id"]} for r in rows if r["date"] and r["p"] is not None]
    fit = iso_fit(pts)
    agg = {}
    for f, p in zip(fit, sorted(pts, key=lambda x: x["date"])):
        a = agg.setdefault(f["date"], [0, 0]); a[0] += f["fit"] * p["w"]; a[1] += p["w"]
    return sorted((d, s / w) for d, (s, w) in agg.items())

def interp(curve, d, today):
    """Cumulative from today: 0 at today, linear between contract dates."""
    xs = [(str(today), 0.0)] + [c for c in curve if c[0] > str(today)]
    if d <= xs[0][0]: return 0.0
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
    rows = table(mh, news, sm, now)
    curve = composite(rows, pp)
    w = pp["market_weight"]
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
    H = series[-1]
    return {"by": pp["by"], "headline": {"blend": H["blend"], "rules": H["rules"], "market": H["market"], "nudge": nudge, "market_weight": w},
            "series": series, "months": months, "opex": opex, "catalysts": cats, "markets": rows, "composite": curve,
            "flags": [r for r in rows if r["flag"]], "nudge_from": [r["name"] for r in nudge_rows],
            "rules_total_from_start": pp["rules_total_from_start"], "gone": gone, "docs": {k: pp[k] for k in pp if k.startswith("_")} | {"market": pp["market"]["_why"], "smart_money": sm["_why"]}}
