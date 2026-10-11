"""Test real extracted group handlers with mocked Telegram and permission system."""
import ast
import asyncio
import importlib.util
from pathlib import Path
import types

ROOT=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('installer',ROOT/'INSTALL_TELEGRAM_CHAT_RELAY.py')
installer=importlib.util.module_from_spec(spec);spec.loader.exec_module(installer)

SOURCE='''from . import config, engine, generator, gamification as gm, service, svgtools
# --------------------------------------------------------------------- обработчики
async def on_message(msg: dict):
    chat_id = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    user = user_by_chat(chat_id)
async def _handle(update: dict):
    chat = None
    try:
        if "message" in update:
            chat = update["message"]["chat"]["id"]
        elif "callback_query" in update:
            chat = update["callback_query"]["message"]["chat"]["id"]
        lk = _chat_locks.setdefault(chat, asyncio.Lock())
        async with lk:
            if "message" in update:
                await on_message(update["message"])
            elif "callback_query" in update:
                await on_callback(update["callback_query"])
    except Exception:
        traceback.print_exc()
        if chat:
            try:
                await send(chat, "Что-то пошло не так. Попробуй ещё раз или открой /menu.")
            except Exception:
                pass
async def _run():
    x = await api("getUpdates", offset=0, timeout=25, allowed_updates=["message", "callback_query"])
'''
patched=installer.tgbot(SOURCE)
tree=ast.parse(patched)
handlers=[node for node in tree.body if isinstance(node,ast.AsyncFunctionDef) and node.name in ('on_relay_group_message','on_relay_membership')]
assert len(handlers)==2

class FakeRelay:
    def __init__(self):self.target=None
    def target_group(self):return self.target
    def configure_group(self,n):self.target=n
    def linked_admin(self,uid):return uid==333
    async def telegram_group_admin(self,gid,uid):return uid==333
    async def register_if_authorized(self,gid,uid):
        if uid!=333:return False
        self.target=gid
        return True

relay=FakeRelay()
sent=[]
async def send(group,body):sent.append((group,body))
ns={'relay':relay,'send':send,'bot_username':lambda:'test_bot'}
exec(compile(ast.Module(body=handlers,type_ignores=[]),'<handlers>','exec'),ns)

async def test():
    msg=lambda txt,uid: {'chat':{'id':-100111,'type':'supergroup'},'text':txt,'from':{'id':uid}}
    await ns['on_relay_group_message'](msg('Hello everybody',333))
    assert not sent
    await ns['on_relay_group_message'](msg('/relay_here@not_our_bot',333))
    assert not sent
    await ns['on_relay_group_message'](msg('/relay_here',444))
    assert not relay.target and 'администратор' in sent[-1][1]
    await ns['on_relay_group_message'](msg('/relay_here@test_bot',333))
    assert relay.target==-100111
    await ns['on_relay_group_message'](msg('/relay_status',333))
    assert 'включена' in sent[-1][1]
    await ns['on_relay_group_message'](msg('/relay_off',333))
    assert relay.target is None
    event={'chat':{'id':-100111,'type':'supergroup'},'from':{'id':333},'old_chat_member':{'status':'left'},'new_chat_member':{'status':'member'}}
    await ns['on_relay_membership'](event)
    assert relay.target==-100111
    event['old_chat_member']['status']='member';event['new_chat_member']['status']='left'
    await ns['on_relay_membership'](event)
    assert relay.target is None

asyncio.run(test())
print('OK: automatic site/group binding, permission checks, /relay_here|off|status, message filtering')
