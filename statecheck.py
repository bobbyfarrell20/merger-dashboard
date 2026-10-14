"""Shared (stdlib-only) state machine check used by update.py and watch.py.
Reads recent Tesla/SpaceX EDGAR filings since state.since and moves:
  pre -> meeting_only : TSLA meeting filing (DEF/PRE 14A, or 8-K 5.08/8.01 announcing the annual meeting) with no merger language
  pre/meeting_only -> letter : any TSLA or SPCX filing with Tesla-SpaceX merger/committee language
"""
import json, re, html, urllib.request
from pathlib import Path
D = Path(__file__).parent / "data"
UA = "merger-dashboard state check (contact: dashboard@example.com)"
CIKS = {"TSLA": "0001318605", "SPCX": "0001181412"}
FORMS = {"DEF 14A", "PRE 14A", "DEFA14A", "DEFM14A", "PREM14A", "8-K", "8-K/A", "S-4", "S-4/A", "425", "SC 13D", "SC 13D/A", "SCHEDULE 13D", "SCHEDULE 13D/A", "SC TO-T", "SC 14D9"}
SKIP_ONLY_ITEMS = {"2.02", "9.01", "5.02", "7.01"}   # 8-Ks with only these items aren't checked (earnings, exhibits, officer changes, Reg FD)
OTHER = {"TSLA": r"space\s*x|space exploration technologies|spcx", "SPCX": r"tesla"}
MERGER_RE = r"(merger|business combination|combination agreement|acquisition proposal|proposal to acquire|special committee|exchange ratio|tender offer)"
MEETING_RE = re.compile(r"(20\d\d annual meeting of (stock|share)holders|annual meeting of (stock|share)holders (will be|to be) held|designated .{0,60} as the date of)", re.I)

def _get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=20) as r:
        return r.read().decode("utf-8", "ignore")

def _text(url):
    t = html.unescape(re.sub(r"<[^>]+>", " ", _get(url)))
    return re.sub(r"\s+", " ", t)

def classify(co, form, text):
    near = re.search(rf"({OTHER[co]}).{{0,250}}{MERGER_RE}|{MERGER_RE}.{{0,250}}({OTHER[co]})", text, re.I)
    if near or form in ("S-4", "S-4/A", "425", "DEFM14A", "PREM14A"):
        return "letter", (near.group(0)[:200] if near else form)
    if co == "TSLA" and MEETING_RE.search(text):
        return "meeting_only", MEETING_RE.search(text).group(0)[:200]
    return None, None

def check(state=None, max_docs=6):
    state = state or json.loads((D / "state.json").read_text())
    if state["state"] == "letter": return state, []
    hits = []
    for co, cik in CIKS.items():
        r = json.loads(_get(f"https://data.sec.gov/submissions/CIK{cik}.json"))["filings"]["recent"]
        n = 0
        for i in range(len(r["form"])):
            if r["filingDate"][i] < state["since"] or n >= max_docs: break
            f, items = r["form"][i], set(filter(None, r["items"][i].split(",")))
            if f not in FORMS or (f.startswith("8-K") and items and items <= SKIP_ONLY_ITEMS): continue
            url = f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{r['accessionNumber'][i].replace('-', '')}/{r['primaryDocument'][i]}"
            n += 1
            new, ev = classify(co, f, _text(url))
            if new: hits.append({"co": co, "form": f, "date": r["filingDate"][i], "url": url, "to": new, "evidence": ev})
    order = {"pre": 0, "meeting_only": 1, "letter": 2}
    for h in sorted(hits, key=lambda h: order[h["to"]]):
        if order[h["to"]] > order[state["state"]]:
            state = {"state": h["to"], "since": state["since"], "changed": h["date"], "evidence": h, "_doc": state.get("_doc")}
    return state, hits

if __name__ == "__main__":
    import sys
    st = {"state": "pre", "since": sys.argv[1] if len(sys.argv) > 1 else "2026-09-01"}
    print(json.dumps(check(st), indent=1))
