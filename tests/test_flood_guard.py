"""No network required: checks rate-limit persistence, group concurrency, 429 handling, worker backoff."""
import asyncio
import contextlib
import importlib.util
import sqlite3
import sys
import tempfile
import time
import threading
import types
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
conn=sqlite3.connect(':memory:',isolation_level=None)
conn.row_factory=sqlite3.Row
conn.executescript('''CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
CREATE TABLE chat_messages(id INTEGER PRIMARY KEY,user_id INTEGER,system_kind TEXT,text TEXT,created_ts INTEGER);
CREATE TABLE users(id INTEGER PRIMARY KEY,display_name TEXT,username TEXT);''')
class DB:
    conn=conn
    lock=threading.RLock()
    def q(self,sql,args=()):return [dict(x) for x in conn.execute(sql,args)]
    def one(self,sql,args=()):
        r=conn.execute(sql,args).fetchone()
        return dict(r) if r else None
    def val(self,sql,args=(),default=None):
        r=conn.execute(sql,args).fetchone()
        return r[0] if r else default
    def x(self,sql,args=()):return conn.execute(sql,args).lastrowid
    def get_setting(self,key,default=''):return self.val('SELECT value FROM settings WHERE key=?',(key,),default)
    def set_setting(self,key,val):self.x('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,val))
    @contextlib.contextmanager
    def tx(self):
        conn.execute('BEGIN IMMEDIATE')
        try:yield
        except BaseException:
            conn.execute('ROLLBACK');raise
        else:conn.execute('COMMIT')

db=DB()
server=types.ModuleType('server');server.__path__=[]
server_db=types.ModuleType('server.db');server_db.db=db
sys.modules['server']=server
sys.modules['server.db']=server_db
spec=importlib.util.spec_from_file_location('server.chat_telegram_relay',ROOT/'server/chat_telegram_relay.py')
relay=importlib.util.module_from_spec(spec);sys.modules[spec.name]=relay;spec.loader.exec_module(relay)
sys.modules['server.chat_telegram_relay']=relay

calls=[]
class FakeTelegram:
    def __init__(self):self.failure=False
    def token(self):return 'fake'
    async def api(self,method,**params):
        calls.append((method,params))
        if self.failure:
            raise RuntimeError('Telegram sendMessage: Too Many Requests: retry after 3490')
        return {'message_id': len(calls)}

fake=FakeTelegram()
sys.modules['server.tgbot']=fake

async def run():
    gid=-100123456
    await relay.send_group(gid,'First')
    assert len(calls)==1
    assert relay.seconds_until_send(gid)>5
    try:
        await relay.send_group(gid,'Second')
        assert False,'rate limiting missing'
    except relay.GroupSendCooldown:pass
    assert len(calls)==1, 'should not call Telegram during cooldown'
    db.set_setting(relay._send_key(gid),'0')
    results=await asyncio.gather(*(relay.send_group(gid, f'{x}') for x in range(8)),return_exceptions=True)
    assert sum(isinstance(x,dict) for x in results)==1,results
    assert sum(isinstance(x,relay.GroupSendCooldown) for x in results)==7,results
    assert len(calls)==2

    fake.failure=True
    db.set_setting(relay._send_key(gid),'0')
    try:
        await relay.send_group(gid,'Get 429')
        assert False
    except RuntimeError as exc:assert relay._retry_after(exc)==3490
    assert 3490<=relay.seconds_until_send(gid)<=3500,relay.seconds_until_send(gid)
    # Later messages must be rejected locally even after a module/site restart.
    oldcount=len(calls)
    try:await relay.send_group(gid,'not yet')
    except relay.GroupSendCooldown:pass
    assert len(calls)==oldcount

    db.set_setting(relay.GROUP_KEY,str(gid))
    relay.init_schema()
    conn.execute("INSERT INTO tg_chat_relay_outbox(message_id,group_id,kind,content,author,created_ts) VALUES(1,?,'system','announcement','',0)",(gid,))
    assert relay._take(int(time.time())) is None,'worker should not even claim messages during cooldown'
    assert conn.execute('SELECT attempts FROM tg_chat_relay_outbox WHERE message_id=1').fetchone()[0]==0
    row={'message_id':1,'group_id':gid,'attempts':1}
    conn.execute("UPDATE tg_chat_relay_outbox SET state='sending' WHERE message_id=1")
    relay._retry(row,RuntimeError('Telegram sendMessage: Too Many Requests: retry after 3490'),int(time.time()))
    check=relay.db.one('SELECT next_try_ts,state FROM tg_chat_relay_outbox WHERE message_id=1')
    assert check['state']=='pending' and check['next_try_ts']-time.time()>=3489,check
    print('OK: group throttle, concurrent sends, 3490s cooldown, durable setting, queue honors cooldown')

asyncio.run(run())
