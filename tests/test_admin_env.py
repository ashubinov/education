"""Администратор — только тот, кого назвали в переменной ADMIN_USERNAME (её меняет владелец сервера).
Запуск: LQ_DB=<tmp.db> python tests/test_admin_env.py"""
import os
import sys
import time

root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, root)
os.environ["ADMIN_USERNAME"] = ""
from server import auth  # noqa: E402
from server.db import db  # noqa: E402


class Req:
    def __init__(self, token):
        self.headers = {"authorization": "Bearer " + token}


tag = str(int(time.time()) % 100000)
first = auth.register(f"first{tag}", "secret1")      # без ADMIN_USERNAME первый зарегистрированный становится админом
second = auth.register(f"second{tag}", "secret1")
assert db.one("SELECT is_admin FROM users WHERE id=?", (first,))["is_admin"] == 1
assert db.one("SELECT is_admin FROM users WHERE id=?", (second,))["is_admin"] == 0
assert auth.user_from_request(Req(auth.make_token(first)))["is_admin"] == 1  # пока переменная не задана, отметка остаётся

os.environ["ADMIN_USERNAME"] = f"owner{tag}"           # владелец задал переменную на сервере
owner = auth.register(f"owner{tag}", "secret1")
assert db.one("SELECT is_admin FROM users WHERE id=?", (owner,))["is_admin"] == 1
assert auth.user_from_request(Req(auth.make_token(owner)))["is_admin"] == 1
assert auth.user_from_request(Req(auth.make_token(first)))["is_admin"] == 0, "прежний «первый» админ должен потерять права"
assert db.one("SELECT is_admin FROM users WHERE id=?", (first,))["is_admin"] == 0
sneaky = auth.register(f"Second{tag}x", "secret1")
db.x("UPDATE users SET is_admin=1 WHERE id=?", (sneaky,))  # даже ручная отметка в базе не делает админом чужого
assert auth.user_from_request(Req(auth.make_token(sneaky)))["is_admin"] == 0
print("OK: админом остаётся только ADMIN_USERNAME")
