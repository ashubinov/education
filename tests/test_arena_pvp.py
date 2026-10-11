"""Standalone PvP integration smoke test, no running LearnQuest server needed.

Run from the education repository root:
    python tests/test_arena_pvp.py
Creates a temporary SQLite database and tests the actual router, auth and wallets.
"""
import os
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
tmp = tempfile.TemporaryDirectory(prefix="learnquest-pvp-test-")
os.environ["LQ_DB"] = str(Path(tmp.name) / "test.db")
os.environ["DATA_DIR"] = tmp.name
os.environ["CORS_ORIGINS"] = "http://testserver"
os.environ["TELEGRAM_ENABLED"] = "0"

from fastapi import FastAPI
from fastapi.testclient import TestClient
from server import arena_pvp as pvp, auth, pet, social, slots
from server.db import db

pet.init_schema()
pvp.init_schema()

@asynccontextmanager
async def lifespan(app):
    import asyncio
    pvp.set_loop(asyncio.get_running_loop())
    yield

app = FastAPI(lifespan=lifespan)
app.include_router(pvp.router)


def request_id(n):
    return f"test-pvp-idempotent-{n:010d}"


def assert_http(response, code=200):
    assert response.status_code == code, (response.status_code, response.text)
    return response.json()


def balance(uid):
    return slots.ensure_account(uid)["balance"]


def post(client, path, uid, data, n):
    return client.post('/api/pet/pvp/' + path, headers={"Authorization": "Bearer " + auth.make_token(uid)},
                       json={**data, "request_id": request_id(n)})


def state(client, uid):
    return assert_http(client.get('/api/pet/pvp', headers={"Authorization": "Bearer " + auth.make_token(uid)}))


def ws_auth(ws, uid):
    ws.send_json({"type": "auth", "token": auth.make_token(uid)})
    assert ws.receive_json()["type"] == "pvp_ready"


def setup_users():
    users = []
    for username, species in (("pvp_alpha", "beaver"), ("pvp_beta", "doberman"), ("pvp_gamma", "ram")):
        uid = auth.register(username, "secret1", "", "test-" + username)
        pet.adopt(uid, species, username)
        slots.ensure_account(uid)
        users.append(uid)
    a, b, c = users
    social.request_friend(a, "pvp_beta")
    social.accept(b, a)
    social.request_friend(a, "pvp_gamma")
    social.accept(c, a)
    return users


def main():
    a, b, c = setup_users()
    with TestClient(app) as client:
        assert not state(client, a)['active']
        assert state(client, a)['pet']['species'] == 'beaver'
        initial_a, initial_b = balance(a), balance(b)
        assert initial_a == slots.START_BALANCE
        # Creating challenge immediately escrows only challenger stake.
        req = {"defender_id": b, "bet": 50}
        result = assert_http(post(client, 'invite', a, req, 1))
        mid = result['active']['id']
        assert result['active']['status'] == 'invited'
        assert balance(a) == initial_a - 50 and balance(b) == initial_b
        assert_http(post(client, 'invite', a, req, 1))
        assert balance(a) == initial_a - 50, 'replay charged twice'
        assert_http(post(client, 'invite', a, {"defender_id": c, "bet": 50}, 2), 409)
        assert_http(post(client, 'decline', b, {"match_id": mid}, 3))
        assert balance(a) == initial_a and balance(b) == initial_b
        assert_http(post(client, 'decline', b, {"match_id": mid}, 3))
        assert balance(a) == initial_a
        print('PASS: invite escrow, conflict, decline refund, idempotency')

        # Without actual two live sockets, accepting is forbidden.
        mid = assert_http(post(client, 'invite', a, req, 4))['active']['id']
        assert_http(post(client, 'accept', b, {"match_id": mid}, 5), 409)
        assert balance(b) == initial_b
        with client.websocket_connect('/api/pet/pvp/ws', headers={"origin": "http://testserver"}) as wa:
            ws_auth(wa, a)
            with client.websocket_connect('/api/pet/pvp/ws', headers={"origin": "http://testserver"}) as wb:
                ws_auth(wb, b)
                assert_http(post(client, 'accept', b, {"match_id": mid}, 6))
                assert balance(a) == initial_a - 50 and balance(b) == initial_b - 50
                sa = state(client, a)['active']
                sb = state(client, b)['active']
                assert sa['status'] == 'betting' and sa['pot'] == 100
                assert sa['my_turn'] and not sb['my_turn']
                assert sa['power_me'] > 0 and sb['power_me'] > 0
                # Raise means call current amount + explicit raise increment.
                assert_http(post(client, 'action', a, {"match_id":mid,"move":"raise","amount":30}, 7))
                assert balance(a) == initial_a - 80
                sb = state(client, b)['active']
                assert sb['to_call'] == 30 and sb['my_turn']
                assert_http(post(client, 'action', b, {"match_id":mid,"move":"call","amount":0}, 8))
                assert balance(b) == initial_b - 80
                m = state(client, a)['active']
                assert m['pot'] == 160 and m['round_no'] == 1
                assert m['status'] == 'reveal' and m['combat']['hp_me'] == pvp.BASE_HP
                assert pvp.COMBAT_SECONDS == 15
                assert 13.0 < m['deadline'] - time.time() <= 15.0, 'fight should last 15s by server clock'
                assert m['combat']['hp_other'] == pvp.BASE_HP
                # Ticker must NEVER hit either pet without a player's action.
                initial_combat = state(client,a)['active']['combat']
                with db.tx():
                    db.x('UPDATE pvp_combat SET tick_at=?,next_qte=? WHERE match_id=?',
                         (time.time()-60,time.time()+60,mid))
                pvp.combat_step()
                idle = state(client,a)['active']['combat']
                assert idle['hp_me'] == initial_combat['hp_me'] == pvp.BASE_HP
                assert idle['hp_other'] == initial_combat['hp_other'] == pvp.BASE_HP
                assert not [e for e in idle['events'] if e['type']=='hit'], 'ticker inflicted passive damage'
                print('PASS: idle pets cause no damage even after overdue server ticks')
                # Spam controls are limited by server, not just by JS buttons.
                strike = assert_http(post(client, 'combat', a, {'match_id':mid,'move':'attack'}, 30))
                health1 = strike['active']['combat']['hp_other']
                assert 0 < health1 < pvp.BASE_HP
                assert_http(post(client, 'combat', a, {'match_id':mid,'move':'attack'}, 30))
                assert state(client, a)['active']['combat']['hp_other'] == health1
                assert_http(post(client, 'combat', a, {'match_id':mid,'move':'attack'}, 31), 429)
                assert_http(post(client, 'combat', b, {'match_id':mid,'move':'guard'}, 32))
                # QTE is emitted for both players; the code is private per user.
                with db.tx():
                    db.x('UPDATE pvp_combat SET next_qte=? WHERE match_id=?', (time.time()-1,mid))
                pvp.combat_step()
                ma, mb = state(client, a)['active'], state(client, b)['active']
                assert ma['combat']['qte_code'] and mb['combat']['qte_code']
                assert 1 <= len(ma['combat']['qte_code']) <= 3
                assert ma['combat']['hp_other'] == health1, 'QTE prompt must not hit by itself'
                nonce = ma['combat']['qte_nonce']
                old_hp = ma['combat']['hp_other']
                qte = {'match_id':mid,'nonce':nonce,'sequence':ma['combat']['qte_code']}
                assert_http(post(client, 'qte', a, qte, 33))
                assert state(client, a)['active']['combat']['hp_other'] < old_hp
                assert_http(post(client, 'qte', a, qte, 33))
                assert_http(post(client, 'qte', a, qte, 34), 409)
                # Round one ends by its clock, but HP is not restored in round two.
                ca = state(client, a)['active']['combat']
                with db.tx():
                    db.x('UPDATE pvp_combat SET phase_until=? WHERE match_id=?', (time.time()-1,mid))
                pvp.combat_step()
                m = state(client,b)['active']
                assert m['round_no']==2 and m['status']=='betting'
                assert m['combat']['hp_me']==ca['hp_other'] and m['combat']['hp_other']==ca['hp_me']
                assert len(m['rounds'])==1
                # Check/check begins a second interactive segment with SAME HP.
                assert_http(post(client,'action',b,{'match_id':mid,'move':'check','amount':0},35))
                assert_http(post(client,'action',a,{'match_id':mid,'move':'check','amount':0},36))
                m = state(client,a)['active']
                assert m['status']=='reveal' and m['round_no']==2
                assert m['combat']['hp_me']==ca['hp_me']
                # Guaranteed knockout for a deterministic money-settlement test.
                with db.tx():
                    db.x('UPDATE pvp_combat SET hp_b=0.1,attack_a=0 WHERE match_id=?', (mid,))
                assert_http(post(client,'combat',a,{'match_id':mid,'move':'attack'},37))
                result=state(client,a)['recent'][0]
                assert result['status']=='finished' and result['reason']=='knockout'
                assert result['winner_id']==a and result['pot']==160
                assert balance(a)+balance(b)==initial_a+initial_b, 'chips must be conserved'
                ledger=db.q('SELECT delta FROM pvp_ledger WHERE match_id=?',(mid,))
                assert sum(x['delta'] for x in ledger)==0, ledger
                assert_http(post(client,'combat',a,{'match_id':mid,'move':'attack'},38),409)
                print('PASS: two sockets, combat, visible damage, QTE, persisted HP, knockout, settlement')

        # Invites that time out refund the full initial bet.
        old = balance(a)
        mid2 = assert_http(post(client,'invite',a,{"defender_id":b,"bet":40},13))['active']['id']
        with db.tx(): db.x('UPDATE pvp_matches SET deadline=? WHERE id=?',(time.time()-1,mid2))
        pvp.expire_due()
        assert balance(a)==old and state(client,a)['recent'][0]['status']=='cancelled'
        print('PASS: invite expiry refunded')

        # Turn timeout while behind the bet automatically forfeits the committed chips.
        before_a, before_b = balance(a), balance(b)
        with client.websocket_connect('/api/pet/pvp/ws', headers={"origin":"http://testserver"}) as wa:
            ws_auth(wa, a)
            with client.websocket_connect('/api/pet/pvp/ws', headers={"origin":"http://testserver"}) as wb:
                ws_auth(wb, b)
                mid3 = assert_http(post(client,'invite',a,{"defender_id":b,"bet":30},14))['active']['id']
                assert_http(post(client,'accept',b,{"match_id":mid3},15))
                pa = state(client,a)['active']['power_me']
                with db.tx(): db.x('UPDATE pets SET xp=xp+20000 WHERE user_id=?',(a,))
                assert state(client,a)['active']['power_me']==pa, 'combat strength must be snapshotted'
                assert_http(post(client,'action',a,{"match_id":mid3,"move":"raise","amount":20},16))
                assert_http(post(client,'action',a,{"match_id":mid3,"move":"raise","amount":30},17),409)
                assert_http(post(client,'action',b,{"match_id":mid3,"move":"check","amount":0},18),400)
                with db.tx(): db.x('UPDATE pvp_matches SET deadline=? WHERE id=?',(time.time()-1,mid3))
                pvp.expire_due()
                finished = state(client,a)['recent'][0]
                assert finished['status']=='finished' and finished['reason']=='timeout_fold'
                assert finished['winner_id']==a and finished['pot']==80
                assert balance(a)==before_a-30-20+80 and balance(b)==before_b-30
                assert balance(a)+balance(b)==before_a+before_b
                assert_http(post(client,'action',b,{"match_id":mid3,"move":"call","amount":0},19),409)
                assert_http(post(client,'invite',a,{"defender_id":b,"bet":20},20),400)
        print('PASS: timeout fold, illegal action rejection, snapshotted power, daily pair limit')

    tmp.cleanup()
    print('ALL PVP INTEGRATION CHECKS PASS')

if __name__ == '__main__':
    main()
