"""Patch compatibility and idempotence with v6 generated handlers, no backend needed."""
from pathlib import Path
import importlib.util, ast
root=Path(__file__).resolve().parent.parent
spec=importlib.util.spec_from_file_location('fix',root/'INSTALL_TELEGRAM_FLOOD_FIX.py');fix=importlib.util.module_from_spec(spec);spec.loader.exec_module(fix)
original='''from . import relay
async def send(chat_id: int, text: str, buttons: list[list[tuple[str, str]]] | None = None, **kw):
    params = {"chat_id": chat_id, "text": text[:4000], "parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons: pass
    return await api('sendMessage', **params)
async def _handle(update):
    chat=0
    try: pass
    except Exception:
        traceback.print_exc()
        if chat and chat > 0:
            try: pass
            except Exception: pass
# LQ_TELEGRAM_GROUP_BINDING_20261011
'''
out=fix.patch_bot(original);ast.parse(out)
assert 'return await relay.send_group(chat_id, text, buttons, **kw)' in out
assert 'isinstance(exc, relay.GroupSendCooldown)' in out
assert fix.patch_bot(out)==out
print('OK: compatible v6 patch, syntax, repeated application')
