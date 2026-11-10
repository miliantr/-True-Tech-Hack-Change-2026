"""SQLite persistence; session capabilities keep independent visitors isolated."""
import json
import re
import secrets
import sqlite3
from contextlib import contextmanager
from pathlib import Path


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY, token TEXT NOT NULL,
                    created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    closed INTEGER NOT NULL DEFAULT 0, busy INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id),
                    role TEXT NOT NULL, content TEXT NOT NULL, debug TEXT,
                    created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
                CREATE TABLE IF NOT EXISTS leads (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL UNIQUE REFERENCES sessions(id),
                    name TEXT NOT NULL, phone TEXT NOT NULL, company TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    created TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                );
            """)
            db.execute("UPDATE sessions SET busy=0")

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_session(self):
        sid, token = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        with self.connection() as db:
            db.execute("INSERT INTO sessions(id,token) VALUES (?,?)", (sid,token))
        return {"id": sid, "token": token}

    def authorized(self, sid, token):
        with self.connection() as db:
            row = db.execute("SELECT token FROM sessions WHERE id=?", (sid,)).fetchone()
        return bool(row and secrets.compare_digest(row["token"].encode(), token.encode()))

    def close(self, sid):
        with self.connection() as db:
            db.execute("UPDATE sessions SET closed=1 WHERE id=?", (sid,))

    def begin_turn(self, sid, text):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT closed,busy FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row or row["closed"] or row["busy"]:
                raise ValueError("Диалог закрыт или ответ ещё готовится")
            db.execute("UPDATE sessions SET busy=1 WHERE id=?", (sid,))
            db.execute("INSERT INTO messages(session_id,role,content) VALUES (?,?,?)", (sid,"user",text))

    def end_turn(self, sid, result):
        with self.connection() as db:
            db.execute("INSERT INTO messages(session_id,role,content,debug) VALUES (?,?,?,?)",
                       (sid,"assistant",result["answer"],json.dumps(result,ensure_ascii=False)))
            db.execute("UPDATE sessions SET busy=0 WHERE id=?", (sid,))

    def messages(self, sid):
        with self.connection() as db:
            rows = db.execute("SELECT id,role,content,debug,created FROM messages WHERE session_id=? ORDER BY id", (sid,)).fetchall()
        return [{**dict(r), "debug": json.loads(r["debug"]) if r["debug"] else None} for r in rows]

    def sessions(self):
        with self.connection() as db:
            rows = db.execute("""SELECT s.id,s.created,s.closed,
                (SELECT count(*) FROM messages WHERE session_id=s.id) AS message_count,
                (SELECT content FROM messages WHERE session_id=s.id AND role='user' ORDER BY id LIMIT 1) AS title,
                (SELECT id FROM leads WHERE session_id=s.id) AS lead_id
                FROM sessions s ORDER BY s.created DESC""").fetchall()
        return [dict(r) for r in rows]

    def lead(self, sid):
        with self.connection() as db:
            row = db.execute("SELECT * FROM leads WHERE session_id=?", (sid,)).fetchone()
        return dict(row) if row else None

    def create_lead(self, sid, fields):
        clean = {}
        for key in ("name","phone","company"):
            value = fields.get(key)
            if not isinstance(value,str) or not 2 <= len(value.strip()) <= 200 or any(ord(c)<32 for c in value):
                raise ValueError("Заполните ФИО, телефон и компанию тестовыми данными")
            clean[key] = value.strip()
        if not re.fullmatch(r"[+\d ()-]+", clean["phone"]) or not 10 <= len(re.sub(r"\D", "", clean["phone"])) <= 15:
            raise ValueError("Телефон должен содержать от 10 до 15 цифр")
        if fields.get("test_data_confirmed") is not True:
            raise ValueError("Подтвердите, что используете только тестовые данные")
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            previous = db.execute("SELECT * FROM leads WHERE session_id=?", (sid,)).fetchone()
            if previous:
                return dict(previous)
            session = db.execute("SELECT closed,busy FROM sessions WHERE id=?", (sid,)).fetchone()
            if not session or session["closed"] or session["busy"]:
                raise ValueError("Дождитесь ответа в активном диалоге")
            users = [r[0] for r in db.execute("SELECT content FROM messages WHERE session_id=? AND role='user' ORDER BY id", (sid,))]
            if not users:
                raise ValueError("Сначала опишите задачу в чате")
            latest = db.execute("SELECT content FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
            summary = {
                "customer_request": users[0][:600],
                "customer_details": [u[:400] for u in users[1:][-5:]],
                "last_consultation": latest[0][:1000] if latest else "Ответ ещё не сформирован",
                "next_step": "Связаться с клиентом и уточнить параметры объекта; расчёты и сроки не обещаны.",
                "note": "Резюме содержит выдержки диалога. Неуказанные параметры неизвестны. Данные тестовые.",
            }
            lid = secrets.token_urlsafe(12)
            db.execute("INSERT INTO leads(id,session_id,name,phone,company,summary) VALUES (?,?,?,?,?,?)",
                       (lid,sid,clean["name"],clean["phone"],clean["company"],json.dumps(summary,ensure_ascii=False)))
            return dict(db.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone())
