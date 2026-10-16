"""Равенство игроков в «Отпетунивании» и «Клубничном воре» (API, сквозной; запускается через tests/run_ci.py).

Раньше в коде была скрытая лазейка для одного логина: его нельзя было поймать, а когда он был жертвой, настоящий нападавший помечался в списке.
Тест фиксирует правило: логин не даёт никаких преимуществ. Игрок с «особым» именем ловится так же, как все, а список подозреваемых у него —
точно такой же, как у остальных (одинаковый набор полей, без пометок).
"""
import sys
import time

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8011"
TAG = str(time.time_ns() % 10**9)
SPECIAL = ["biba", "BIBA", "Biba"]          # регистр не должен ничего менять


def reg(username):
    r = httpx.post(BASE + "/api/auth/register", json={"username": username, "password": "secret1"}, timeout=30)
    assert r.status_code == 200, (username, r.text)
    d = r.json()
    return httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + d["token"]}, timeout=30), d["user"]


def friends(a, aa, b, bb):
    assert a.post("/api/friends/request", json={"username": bb["username"]}).status_code == 200
    assert b.post(f"/api/friends/{aa['id']}/accept").status_code == 200


def rid(prefix, n):
    return f"{prefix}-equal-request-{n:010d}"


def petun_round(attacker, victim, v_user, a_user, n, suspect=None):
    r = attacker.post("/api/minigames/petun/attack", json={"victim_id": v_user["id"], "request_id": rid("pt", n)})
    assert r.status_code == 200, r.text
    pend = victim.get("/api/minigames/petun/state").json()["pending"][0]
    g = victim.post("/api/minigames/petun/guess", json={"attack_id": pend["id"], "suspect_id": suspect or a_user["id"], "request_id": rid("ptg", n)})
    assert g.status_code == 200, g.text
    return pend, g.json()


def theft_round(attacker, victim, v_user, a_user, n):
    r = attacker.post("/api/minigames/strawberry/steal", json={"victim_id": v_user["id"], "amount": 10, "request_id": rid("sb", n)})
    assert r.status_code == 200, r.text
    pend = victim.get("/api/minigames/strawberry/state").json()["pending"][0]
    g = victim.post("/api/minigames/strawberry/guess", json={"theft_id": pend["id"], "suspect_id": a_user["id"], "request_id": rid("sbg", n)})
    assert g.status_code == 200, g.text
    return pend, g.json()


n = 0
for i, special in enumerate(SPECIAL):
    # логин с «особым» именем бывает один; регистр разный — берём по одному аккаунту на проверку, остальные имена пробуем, если регистрация разрешена
    r = httpx.post(BASE + "/api/auth/register", json={"username": special, "password": "secret1"}, timeout=30)
    if r.status_code != 200:
        continue                                                     # имя в другом регистре уже занято — значит оно и так проверено
    d = r.json()
    special_client = httpx.Client(base_url=BASE, headers={"Authorization": "Bearer " + d["token"]}, timeout=30)
    special_user = d["user"]
    v1, v1u = reg(f"vict1{i}{TAG}")          # жертва «особого» нападающего (обе игры)
    v2, v2u = reg(f"vict2{i}{TAG}")          # жертва обычного нападающего (контроль)
    v3, v3u = reg(f"vict3{i}{TAG}")          # обычная жертва для сравнения списков подозреваемых
    ordinary, o_user = reg(f"ordin{i}{TAG}")
    attacker, a_user = reg(f"atk{i}{TAG}")
    friends(special_client, special_user, v1, v1u)
    friends(ordinary, o_user, v2, v2u)
    friends(attacker, a_user, special_client, special_user)
    friends(attacker, a_user, v3, v3u)

    # 1) «особый» нападающий ловится так же, как любой: верно выбранный нападавший = пойман (обе игры)
    n += 1
    pend, res = petun_round(special_client, v1, v1u, special_user, n)
    assert res["caught"] is True and res["status"] == "caught", ("отпетунивание: нападавшего нельзя не поймать", special, res)
    time.sleep(1.1)
    n += 1
    _, res = theft_round(special_client, v1, v1u, special_user, n)
    assert res["caught"] is True, ("клубничный вор: нападавшего нельзя не поймать", special, res)

    # 2) у обычного нападавшего всё так же (контроль)
    n += 1
    _, res = petun_round(ordinary, v2, v2u, o_user, n)
    assert res["caught"] is True

    # 3) у «особой» жертвы список подозреваемых такой же, как у обычной: тот же набор полей, никаких пометок и подсветки
    n += 1
    for victim_user in (special_user, v3u):
        r = attacker.post("/api/minigames/petun/attack", json={"victim_id": victim_user["id"], "request_id": rid("pc", n * 10 + victim_user["id"])})
        assert r.status_code == 200, r.text
        time.sleep(0.2)
    cand_special = special_client.get("/api/minigames/petun/state").json()["pending"][0]["candidates"]
    cand_plain = v3.get("/api/minigames/petun/state").json()["pending"][0]["candidates"]
    for lst in (cand_special, cand_plain):
        assert all(set(c) == set(cand_plain[0]) for c in lst), lst
        assert not any("emphasis" in c for c in lst), lst
    assert a_user["id"] in {c["id"] for c in cand_special}
    print("OK: логин", special, "не даёт ни защиты от угадывания, ни подсказок")

print("ALL OK")
