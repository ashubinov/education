"""Real subprocess installation twice against a v6-like repo + SQLite settings."""
from pathlib import Path
import ast
import os
import sqlite3
import shutil
import subprocess
import sys
import tempfile
import time
root=Path(__file__).resolve().parent.parent
with tempfile.TemporaryDirectory() as d:
    proj=Path(d)/'education'
    (proj/'server').mkdir(parents=True)
    (proj/'payload').mkdir(parents=True)
    (proj/'data').mkdir(parents=True)
    src=(root/'tests/test_installer_patch.py').read_text()
    # Extract the representative code from prior test's string literal.
    tree=ast.parse(src)
    orig=next(n.value.value for n in tree.body if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=='original' for t in n.targets))
    (proj/'server/tgbot.py').write_text(orig,encoding='utf8')
    (proj/'server/chat_telegram_relay.py').write_bytes((root/'_relay_old.py').read_bytes())
    (proj/'server/__init__.py').write_text('')
    (proj/'server/config.py').write_text('from pathlib import Path\nDB_PATH=Path(__file__).parent.parent / "data" / "app.sqlite"\n')
    (proj/'payload/chat_telegram_relay.py').write_bytes((root/'payload/chat_telegram_relay.py').read_bytes())
    shutil.copy(root/'INSTALL_TELEGRAM_FLOOD_FIX.py',proj)
    conn=sqlite3.connect(proj/'data/app.sqlite')
    conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)')
    conn.execute("INSERT INTO settings VALUES('tg_chat_relay_group_id','-100444')")
    conn.commit();conn.close()
    def run():
        return subprocess.run([sys.executable,'INSTALL_TELEGRAM_FLOOD_FIX.py'],cwd=proj,encoding='utf8',capture_output=True)
    a=run()
    assert a.returncode==0,a.stderr
    assert 'INSTALLATION SUCCESSFUL' in a.stdout,a.stdout
    conn=sqlite3.connect(proj/'data/app.sqlite')
    wait=float(conn.execute("SELECT value FROM settings WHERE key='tg_chat_relay_send_after_-100444'").fetchone()[0])-time.time()
    conn.close()
    assert 3480 < wait <= 3491,wait
    original=(proj/'server/tgbot.py').read_bytes(),(proj/'server/chat_telegram_relay.py').read_bytes()
    b=run()
    assert b.returncode==0,b.stderr
    assert 'ALREADY INSTALLED' in b.stdout,b.stdout
    assert original==((proj/'server/tgbot.py').read_bytes(),(proj/'server/chat_telegram_relay.py').read_bytes())
    assert (proj.parent/'learnquest_telegram_relay_backups').exists()
    print('OK: clean install, persisted 3490s pause, file backup, install twice without reset')
