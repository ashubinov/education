"""Strawberry Thief API E2E: run via tests/run_ci.py on disposable LQ_DB.

Tests transfers, escrow, privacy, 24h timeout, idempotency, wrong/correct guess,
user isolation, system chat notices and client-unspendable pending rewards.
"""
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TAG = str(time.time_ns() % 10**11)
DBPATH = os.environ.get("LQ_DB")


def reg(suffix):
    username = f"berry{suffix}{TAG}"
    res = httpx.post(BASE + "/api/auth/register", json={"username":username,"password":"secret1"}, timeout=30)
    assert res.status_code == 200, res.text
    data = res.json()
    return httpx.Client(base_url=BASE,headers={"Authorization":"Bearer "+data["token"]}, timeout=30),data["user"]


def friends(a,aa,b,bb):
    r = a.post("/api/friends/request", json={"username":bb["username"]})
    assert r.status_code == 200,r.text
    r = b.post(f"/api/friends/{aa['id']}/accept")
    assert r.status_code == 200,r.text


def balance(client):
    return client.get("/api/slots/state").json()["balance"]


def post_steal(client,victim_id,rid):
    return client.post("/api/minigames/strawberry/steal",json={"victim_id":victim_id,"request_id":rid})


def post_guess(client,tid,suspect,rid):
    return client.post("/api/minigames/strawberry/guess",json={"theft_id":tid,"suspect_id":suspect,"request_id":rid})


def alter(sql, args):
    assert DBPATH, "E2E test requires LQ_DB separate test database"
    with sqlite3.connect(DBPATH,timeout=30) as conn:
        conn.execute(sql,args)


thief,t = reg("thief")
victim,v = reg("victim")
other,o = reg("other")
outsider,u = reg("outsider")
friends(thief,t,victim,v)
friends(victim,v,other,o)
assert outsider.get("/api/minigames/strawberry/status").json()["pending_count"]==0
assert post_steal(outsider,v["id"],"unauthorized-steal-01").status_code==404
assert httpx.post(BASE+"/api/minigames/strawberry/steal",json={"victim_id":v["id"],"request_id":"anonymous-user-0001"}).status_code==401
assert post_steal(thief,t["id"],"same-user-00000001").status_code==400
start_thief,start_victim=balance(thief),balance(victim)

# Theft gives NO spendable chips to thief; only frozen escrow is displayed.
r=post_steal(thief,v["id"],"strawberry-steal-0001")
assert r.status_code==200,r.text
s=r.json(); amount=s["amount"]; fee=s["reserved_fee"]
assert 10<=amount<=100 and fee==(amount+19)//20 and s["frozen"]
assert balance(thief)==start_thief-fee
assert balance(victim)==start_victim-amount
assert thief.get("/api/minigames/strawberry/state").json()["frozen"]==amount
assert victim.get("/api/minigames/strawberry/status").json()["pending_count"]==1
pending=victim.get("/api/minigames/strawberry/state").json()["pending"][0]
assert pending["id"]==s["id"] and set(x["id"] for x in pending["candidates"])=={t["id"],o["id"]}
assert "thief_id" not in pending and "thief_username" not in pending
assert post_steal(thief,v["id"],"strawberry-steal-0002").status_code==409
assert post_steal(thief,v["id"],"strawberry-steal-0001").json()["replayed"] is True
assert balance(thief)==start_thief-fee

# Correct guess: return amount and 5% fee;  thief never gets frozen principal.
g=post_guess(victim,s["id"],t["id"],"guess-correct-00001")
assert g.status_code==200,g.text
assert g.json()["caught"] and g.json()["compensation"]==fee
assert balance(victim)==start_victim+fee and balance(thief)==start_thief-fee
assert post_guess(victim,s["id"],t["id"],"guess-correct-00001").json()["replayed"]
assert post_guess(victim,s["id"],o["id"],"guess-different-0001").status_code==409
chat=thief.get("/api/chat/messages").json()["messages"]
assert any(m["text"]==f"{t['username'].upper()} СЛОВИЛ КРЕМПАЙ!!!!!" and m["system_kind"]=="strawberry" for m in chat)

# Wrong guess immediately finalizes robbery.
alter("UPDATE strawberry_thefts SET created_ts=created_ts-100 WHERE id=?",(s["id"],))
r=post_steal(thief,v["id"],"strawberry-steal-0003")
assert r.status_code==200,r.text
s2=r.json(); f2=s2["reserved_fee"]
before_guess_thief=balance(thief)
before_guess_victim=balance(victim)
g=post_guess(victim,s2["id"],o["id"],"guess-wrong-000001")
assert g.status_code==200,g.text
assert not g.json()["caught"]
assert balance(thief)==before_guess_thief+s2["amount"]+f2
assert balance(victim)==before_guess_victim
chat=thief.get("/api/chat/messages").json()["messages"]
assert any(m["text"]==f"{v['username'].upper()} ОТКЛУБНИЧЕН ПО ПОЛНОЙ!!!!!" and m["system_kind"]=="strawberry" for m in chat)

# 24h timeout = thief wins WITHOUT action from victim.
alter("UPDATE strawberry_thefts SET created_ts=created_ts-100 WHERE id=?",(s2["id"],))
r=post_steal(thief,v["id"],"strawberry-steal-0004")
assert r.status_code==200,r.text
s3=r.json()
before_timeout_thief=balance(thief)
alter("UPDATE strawberry_thefts SET expires_ts=? WHERE id=?",(int(time.time())-1,s3["id"]))
assert victim.get("/api/minigames/strawberry/status").json()["pending_count"]==0
assert balance(thief)==before_timeout_thief+s3["amount"]+s3["reserved_fee"]
assert post_guess(victim,s3["id"],t["id"],"guess-too-late-0001").status_code==409
# Repeated settlement is a no-op.
bal=balance(thief)
victim.get("/api/minigames/strawberry/status")
assert balance(thief)==bal
assert thief.get("/api/minigames/strawberry/state").json()["frozen"]==0

# Concurrent duplicate requests are one ledger operation, not two.
alter("UPDATE strawberry_thefts SET created_ts=created_ts-100 WHERE id=?",(s3["id"],))
before=balance(thief)
with ThreadPoolExecutor(max_workers=6) as ex:
    results=list(ex.map(lambda _:post_steal(thief,v["id"],"strawberry-race-0001"),range(6)))
assert all(r.status_code==200 for r in results),[(r.status_code,r.text) for r in results]
assert len({r.json()["id"] for r in results})==1
assert balance(thief)==before-results[0].json()["reserved_fee"]

# Existing message commands and genuine system messages are distinguished.
msgs=other.get("/api/chat/messages").json()["messages"]
assert all(m.get("system_kind") is not None for m in msgs if 'СЛОВИЛ КРЕМПАЙ' in m['text'] or 'ОТКЛУБНИЧЕН ПО ПОЛНОЙ' in m['text'])

for c in (thief,victim,other,outsider): c.close()
print("OK: Strawberry escrow, fixed 24h timeout, correct/wrong guesses, idempotency, private candidates, global notices")
