"""Standalone relay tests: python tests/test_chat_telegram_relay.py.
No Telegram connection, no dependency on production DB.
"""
import asyncio
from contextlib import contextmanager
import importlib.util
import pathlib
import sqlite3
import sys
import threading
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent


class DB:
    def __init__(self):
        self.conn = sqlite3.connect(':memory:', isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.conn.executescript('''
          CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE users(id INTEGER PRIMARY KEY,username TEXT,display_name TEXT,is_admin INTEGER,banned INTEGER,tg_chat_id INTEGER);
          CREATE TABLE chat_messages(id INTEGER PRIMARY KEY,user_id INTEGER,request_id TEXT,text TEXT,created_ts INTEGER,system_kind TEXT);
          CREATE TABLE chat_events(id INTEGER PRIMARY KEY, message_id INTEGER, kind TEXT,created_ts INTEGER);
        ''')
    def one(self, q, args=()):
        with self.lock:
            r = self.conn.execute(q,args).fetchone()
        return dict(r) if r else None
    def q(self,q,args=()):
        with self.lock:
            return [dict(x) for x in self.conn.execute(q,args).fetchall()]
    def val(self,q,args=(),default=None):
        with self.lock:
            r=self.conn.execute(q,args).fetchone()
        return r[0] if r and r[0] is not None else default
    def x(self,q,args=()):
        with self.lock:
            return self.conn.execute(q,args).lastrowid
    def get_setting(self,k,default=''):
        return self.val('SELECT value FROM settings WHERE key=?',(k,),default)
    def set_setting(self,k,v):
        self.x('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(k,v))
    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute('BEGIN IMMEDIATE')
            try:
                yield self
            except BaseException:
                self.conn.execute('ROLLBACK');raise
            else:
                self.conn.execute('COMMIT')


db=DB()
server = types.ModuleType('server')
server.__path__=[]
sys.modules['server']=server
dbmod=types.ModuleType('server.db');dbmod.db=db
sys.modules['server.db']=dbmod
sent=[]
fail=[False]
tgbot=types.ModuleType('server.tgbot')
tgbot.token=lambda: '123:FAKE'
async def send(target,text,**kwargs):
    if fail[0]: raise RuntimeError('network unreachable')
    sent.append((target,text,kwargs))
    return {'message_id':len(sent)+10}
tgbot.send=send
async def api(method,**args):
    if method=='getChatMember':return {'status':'administrator' if args['user_id']==1122 else 'member'}
    raise AssertionError(method)
tgbot.api=api
sys.modules['server.tgbot']=tgbot
spec=importlib.util.spec_from_file_location('server.chat_telegram_relay',ROOT/'server/chat_telegram_relay.py')
relay=importlib.util.module_from_spec(spec);sys.modules[spec.name]=relay;spec.loader.exec_module(relay)


def put(user,text,kind=None):
    return db.x('INSERT INTO chat_messages(user_id,request_id,text,created_ts,system_kind) VALUES(?,?,?,?,?)',
                (user,str(time.time_ns()),text,int(time.time()),kind))


db.x('INSERT INTO users(id,username,display_name,is_admin,banned,tg_chat_id) VALUES(?,?,?,?,?,?)',
     (1,'test_admin','<Анна & Коля>',1,0,1122))
relay.init_schema()
relay.init_schema()
assert relay.target_group() is None
put(1,'already here','strawberry')
assert not db.q('SELECT * FROM tg_chat_relay_outbox')
assert relay.linked_admin(1122)
assert not relay.linked_admin(1123)
assert asyncio.run(relay.register_if_authorized(-1001511,1122))
assert not asyncio.run(relay.register_if_authorized(-1001511,1123))
assert relay.target_group()==-1001511
put(1,'ordinary',None)
assert db.val('SELECT count(*) FROM tg_chat_relay_outbox') == 0
with db.tx():
    a=put(1,'НАЧАЛО КРАЖИ!!!!!','strawberry')
    b=put(1,'/бонус · +50 🪙',None)
    db.x("UPDATE chat_messages SET system_kind='command' WHERE id=?",(b,))
    db.x("UPDATE chat_messages SET system_kind='command' WHERE id=?",(b,))
    c=put(1,'SYSTEM NOTE','system')
assert db.val('SELECT count(*) FROM tg_chat_relay_outbox')==3
assert asyncio.run(relay.run_once())
assert 'НАЧАЛО КРАЖИ' in sent[0][1]
assert asyncio.run(relay.run_once())
assert 'КОМАНДА' in sent[1][1] and '&lt;Анна &amp; Коля&gt;' in sent[1][1]
assert '/бонус' in sent[1][1]
assert asyncio.run(relay.run_once())
assert not asyncio.run(relay.run_once())
assert len(sent)==3
assert db.val("SELECT count(*) FROM tg_chat_relay_outbox WHERE state='delivered'")==3

# At least once: failure keeps record pending for delayed retry.
fail[0]=True
fourth=put(1,'temporary error','system')
assert asyncio.run(relay.run_once())
row=db.one('SELECT * FROM tg_chat_relay_outbox WHERE message_id=?',(fourth,))
assert row['state']=='pending' and row['attempts']==1
assert not asyncio.run(relay.run_once())
fail[0]=False
db.x('UPDATE tg_chat_relay_outbox SET next_try_ts=0 WHERE message_id=?',(fourth,))
assert asyncio.run(relay.run_once())
assert len(sent)==4
# Changing the group marks obsolete pending events as skipped.
old=put(1,'queued for group A','system')
relay.configure_group(-1002522)
assert db.one('SELECT state FROM tg_chat_relay_outbox WHERE message_id=?',(old,))['state']=='skipped'
put(1,'new group','system')
assert asyncio.run(relay.run_once())
assert sent[-1][0]==-1002522
relay.configure_group(None)
put(1,'no destination','strawberry')
assert db.val('SELECT count(*) FROM tg_chat_relay_outbox')==6
print('OK: durable Telegram outbox, no normal-chat leak, author escaping, retries, switches, authorization')
