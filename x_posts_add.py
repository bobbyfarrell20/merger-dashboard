#!/usr/bin/env python3
"""Merge curated X posts into data/x_posts.json (dedupe by id, newest first).
Usage:
  python3 x_posts_add.py new_posts.json      # file with a list (or {"posts":[...]})
  cat new.json | python3 x_posts_add.py -
Each post: id, handle, name, created_at (ISO UTC, e.g. 2026-10-14T20:33:59Z), text,
why (one line), factor; optional: url, metrics, kind (direct|report|analyst).
Existing ids are updated (fields merged), never duplicated. Criteria: data/config.json x_watch."""
import json,sys,datetime as dt
from zoneinfo import ZoneInfo
P='data/x_posts.json'
def et(iso):
    t=dt.datetime.fromisoformat(iso.replace('Z','+00:00')).astimezone(ZoneInfo('America/New_York'))
    return t.strftime('%b %-d, %Y %-I:%M %p ET')
def main():
    src=sys.stdin if sys.argv[1:]==['-'] else open(sys.argv[1])
    new=json.load(src); new=new.get('posts',new) if isinstance(new,dict) else new
    try: db=json.load(open(P))
    except FileNotFoundError: db={'posts':[]}
    by={p['id']:p for p in db['posts']}; added=0
    for p in new:
        p=dict(p); p['id']=str(p['id'])
        for k in ('id','handle','created_at','text','why'):
            if not p.get(k): sys.exit(f'missing {k} in {p.get("id")}')
        p.setdefault('url',f"https://x.com/{p['handle']}/status/{p['id']}")
        p['time_et']=et(p['created_at'])
        if p['id'] not in by: added+=1
        by[p['id']]={**by.get(p['id'],{}),**p}
    db['posts']=sorted(by.values(),key=lambda p:p['created_at'],reverse=True)
    db['updated_at']=dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')
    db['updated_et']=dt.datetime.now(ZoneInfo('America/New_York')).strftime('%b %-d, %Y %-I:%M %p ET')
    json.dump(db,open(P,'w'),indent=1,ensure_ascii=False)
    print(f'added {added}, total {len(db["posts"])}')
if __name__=='__main__': main()
