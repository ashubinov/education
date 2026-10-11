"""Fast idempotence/anchor checks against known LearnQuest source fragments."""
import importlib.util
import pathlib
import tempfile
root=pathlib.Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('ins',root/'INSTALL_TELEGRAM_CHAT_RELAY.py')
ins=importlib.util.module_from_spec(spec);spec.loader.exec_module(ins)

main='''from . import chat_commands
import asyncio
async def lifespan():
    chat_commands.init_schema()
    tgbot.start()
    strawberry_task = asyncio.create_task(minigames.expiry_loop())
    try:
        yield
    finally:
        strawberry_task.cancel()
        try:
            await strawberry_task
        except asyncio.CancelledError:
            pass
    await tgbot.stop()
'''
mini='''def steal():
    if True:
        rid = 1
        return {"id":rid,"amount":amount,"frozen":True,"reserved_fee":0,
                "replayed":False,"balance":ta["balance"]}
'''
bot='''from . import config, engine, generator, gamification as gm, service, svgtools
import asyncio
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
for name,raw,patch,marker in [
    ('minigames',mini,ins.minigames,ins.MARK_MINI),
    ('main',main,ins.main,ins.MARK_MAIN),
    ('tgbot',bot,ins.tgbot,ins.MARK_BOT),
]:
    updated=patch(raw)
    ins.validate(name+'.py',updated)
    assert marker in updated
    assert patch(updated)==updated
    assert updated.count(marker)==1
    print('OK patch:',name)

with tempfile.TemporaryDirectory() as tmp:
    tmp=pathlib.Path(tmp)
    for fn,src in [('minigames.py',mini),('main.py',main),('tgbot.py',bot),
                   ('chat.py',''),('chat_commands.py','')]:
        f=tmp/'server'/fn;f.parent.mkdir(parents=True,exist_ok=True);f.write_text(src,encoding='utf-8')
    (tmp/'server'/'chat_telegram_relay.py').write_text((root/'server/chat_telegram_relay.py').read_text())
    previous=ins.ROOT;ins.ROOT=tmp
    try:
        ins.install()
        first={f:(tmp/'server'/f).read_text() for f in ('minigames.py','main.py','tgbot.py')}
        ins.install()
        second={f:(tmp/'server'/f).read_text() for f in ('minigames.py','main.py','tgbot.py')}
        assert first==second
        print('OK installer twice, no duplicate changes')
    finally:ins.ROOT=previous
