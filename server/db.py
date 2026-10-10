"""SQLite: тонкая обёртка + схема."""
import json
import sqlite3
import threading
import time
from contextlib import contextmanager

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE COLLATE NOCASE NOT NULL,
  display_name TEXT,
  pass_hash TEXT NOT NULL,
  salt TEXT NOT NULL,
  avatar TEXT DEFAULT '🦊',
  theme_color TEXT DEFAULT '#7c5cff',
  theme_mode TEXT DEFAULT 'dark',
  theme_extra TEXT,
  must_change INTEGER NOT NULL DEFAULT 0,
  sound INTEGER DEFAULT 1,
  reminders_on INTEGER DEFAULT 1,
  reminder_time TEXT DEFAULT '19:00',
  tg_chat_id INTEGER,
  tg_link_code TEXT,
  tg_run_id INTEGER,
  last_reminder_day TEXT,
  last_reminder2_day TEXT,
  is_admin INTEGER DEFAULT 0,
  token_version INTEGER DEFAULT 0,
  banned INTEGER DEFAULT 0,
  banned_reason TEXT,
  banned_at TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sessions(
  token TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  title TEXT NOT NULL,
  description TEXT DEFAULT '',
  icon TEXT DEFAULT '📘',
  status TEXT DEFAULT 'processing',
  status_text TEXT DEFAULT '',
  error TEXT,
  language TEXT DEFAULT 'ru',
  has_practice INTEGER DEFAULT 0,
  practice_kind TEXT DEFAULT 'none',
  goal_date TEXT,
  goal_set_at TEXT,
  notes TEXT DEFAULT '',
  catalog_no INTEGER,
  origin_id INTEGER,
  is_template INTEGER DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  last_opened TEXT
);
CREATE TABLE IF NOT EXISTS sources(
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL,
  filename TEXT NOT NULL,
  chars INTEGER DEFAULT 0,
  text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL,
  source_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  text TEXT NOT NULL,
  digest TEXT
);
CREATE TABLE IF NOT EXISTS modules(
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  title TEXT NOT NULL,
  summary TEXT DEFAULT '',
  objectives TEXT DEFAULT '[]',
  chunk_ids TEXT DEFAULT '[]',
  practical INTEGER DEFAULT 0,
  status TEXT DEFAULT 'new',
  plan TEXT,
  retries INTEGER DEFAULT 0,
  kind TEXT DEFAULT 'main'
);
CREATE TABLE IF NOT EXISTS lessons(
  id INTEGER PRIMARY KEY,
  course_id INTEGER NOT NULL,
  module_id INTEGER NOT NULL,
  idx INTEGER NOT NULL,
  type TEXT NOT NULL,
  title TEXT NOT NULL,
  minutes INTEGER DEFAULT 5,
  status TEXT DEFAULT 'pending',
  content TEXT,
  error TEXT,
  score REAL,
  completed_at TEXT,
  meta TEXT DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS runs(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  lesson_id INTEGER NOT NULL,
  step_idx INTEGER DEFAULT 0,
  state TEXT DEFAULT '{}',
  started_at TEXT DEFAULT CURRENT_TIMESTAMP,
  finished_at TEXT,
  score REAL,
  xp INTEGER DEFAULT 0,
  last_step_at REAL
);
CREATE TABLE IF NOT EXISTS answers(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  course_id INTEGER NOT NULL,
  lesson_id INTEGER NOT NULL,
  concept TEXT,
  kind TEXT,
  correct REAL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS activity(
  user_id INTEGER NOT NULL,
  course_id INTEGER NOT NULL,
  day TEXT NOT NULL,
  xp INTEGER DEFAULT 0,
  lessons INTEGER DEFAULT 0,
  seconds INTEGER DEFAULT 0,
  answers INTEGER DEFAULT 0,
  correct REAL DEFAULT 0,
  PRIMARY KEY(user_id, course_id, day)
);
CREATE TABLE IF NOT EXISTS achievements(
  user_id INTEGER NOT NULL,
  key TEXT NOT NULL,
  unlocked_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(user_id, key)
);
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);
CREATE TABLE IF NOT EXISTS friendships(
  requester_id INTEGER NOT NULL,
  addressee_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(requester_id, addressee_id)
);
CREATE TABLE IF NOT EXISTS signatures(
  id INTEGER PRIMARY KEY,
  target_id INTEGER NOT NULL,
  author_id INTEGER NOT NULL,
  text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
  moderated_by INTEGER,
  UNIQUE(target_id, author_id)
);
CREATE TABLE IF NOT EXISTS avatars(
  user_id INTEGER PRIMARY KEY,
  key TEXT UNIQUE NOT NULL,
  mime TEXT NOT NULL,
  data BLOB NOT NULL,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS slot_accounts(
  user_id INTEGER PRIMARY KEY,
  balance INTEGER NOT NULL,
  last_daily_day TEXT,
  spins_total INTEGER NOT NULL DEFAULT 0,
  won_total INTEGER NOT NULL DEFAULT 0,
  wagered_total INTEGER NOT NULL DEFAULT 0,
  wins_count INTEGER NOT NULL DEFAULT 0,
  best_win INTEGER NOT NULL DEFAULT 0,
  xp_spent INTEGER NOT NULL DEFAULT 0,
  bought_day TEXT,
  bought_today INTEGER NOT NULL DEFAULT 0,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS slot_purchases(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  request_id TEXT,
  chips INTEGER NOT NULL,
  xp INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_slot_buy_req ON slot_purchases(user_id, request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_slot_buy_user ON slot_purchases(user_id, id DESC);
CREATE TABLE IF NOT EXISTS slot_spins(
  id INTEGER PRIMARY KEY,
  user_id INTEGER NOT NULL,
  request_id TEXT,
  bet INTEGER NOT NULL,
  reels TEXT NOT NULL,
  lines TEXT DEFAULT '{}',
  payout INTEGER NOT NULL,
  net INTEGER NOT NULL,
  balance_after INTEGER NOT NULL,
  win_tier TEXT DEFAULT 'none',
  created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_slot_spins_user ON slot_spins(user_id, id DESC);
CREATE UNIQUE INDEX IF NOT EXISTS ux_slot_spins_req ON slot_spins(user_id, request_id) WHERE request_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_friend_addr ON friendships(addressee_id, status);
CREATE INDEX IF NOT EXISTS ix_sign_status ON signatures(status, created_at);
CREATE INDEX IF NOT EXISTS ix_modules_course ON modules(course_id, idx);
CREATE INDEX IF NOT EXISTS ix_lessons_course ON lessons(course_id, module_id, idx);
CREATE INDEX IF NOT EXISTS ix_runs_user ON runs(user_id, lesson_id);
CREATE INDEX IF NOT EXISTS ix_answers_course ON answers(user_id, course_id);
"""


class DB:
    def __init__(self, path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.execute("PRAGMA foreign_keys=OFF")
            self.conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self):
        """Лёгкие миграции для баз, созданных старыми версиями."""
        def cols(t):
            return {r[1] for r in self.conn.execute(f"PRAGMA table_info({t})")}
        for table, col, ddl in (("users", "token_version", "INTEGER DEFAULT 0"), ("courses", "catalog_no", "INTEGER"),
                                ("courses", "origin_id", "INTEGER"), ("courses", "is_template", "INTEGER DEFAULT 0"),
                                ("slot_accounts", "xp_spent", "INTEGER NOT NULL DEFAULT 0"), ("slot_accounts", "bought_day", "TEXT"),
                                ("slot_accounts", "bought_today", "INTEGER NOT NULL DEFAULT 0"), ("users", "theme_extra", "TEXT"), ("users", "must_change", "INTEGER NOT NULL DEFAULT 0"), ("users", "banned", "INTEGER DEFAULT 0"), ("users", "banned_reason", "TEXT"), ("users", "banned_at", "TEXT")):
            if col not in cols(table):
                self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_catalog_no ON courses(catalog_no) WHERE catalog_no IS NOT NULL")
        self.conn.execute("CREATE INDEX IF NOT EXISTS ix_courses_origin ON courses(user_id, origin_id)")

    def q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def one(self, sql, args=()):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return dict(r) if r else None

    def x(self, sql, args=()):
        """Выполнить запрос, вернуть lastrowid."""
        with self.lock:
            return self.conn.execute(sql, args).lastrowid

    def val(self, sql, args=(), default=None):
        with self.lock:
            r = self.conn.execute(sql, args).fetchone()
            return r[0] if r and r[0] is not None else default

    @contextmanager
    def tx(self):
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self
            except BaseException:
                self.conn.execute("ROLLBACK")
                raise
            else:
                self.conn.execute("COMMIT")

    # --- настройки приложения (ключи API и т.п.) ---
    def get_setting(self, key, default=""):
        return self.val("SELECT value FROM settings WHERE key=?", (key,), default)

    def set_setting(self, key, value):
        self.x("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
               (key, value))


db = DB(config.DB_PATH)


def jl(s, default=None):
    """json.loads с запасным значением."""
    if s is None or s == "":
        return default
    try:
        return json.loads(s)
    except Exception:
        return default


def jd(o):
    return json.dumps(o, ensure_ascii=False)


def now() -> float:
    return time.time()
