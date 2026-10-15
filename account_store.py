"""SQLite account data, opaque sessions and encrypted Google credentials."""
from contextlib import contextmanager
import hashlib
import json
import os
import secrets
import sqlite3
import time
import uuid
from pathlib import Path

from cryptography.fernet import Fernet


class AccountStore:
    def __init__(self, path, encryption_key=None):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.cipher = Fernet(encryption_key) if encryption_key else None
        with self.connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY, google_sub TEXT UNIQUE NOT NULL,
                    email TEXT NOT NULL, name TEXT NOT NULL, tokens TEXT);
                CREATE TABLE IF NOT EXISTS sessions (
                    digest TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    csrf_token TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS oauth_states (
                    digest TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS items (
                    id TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                    source_url TEXT NOT NULL, title TEXT NOT NULL, platform TEXT NOT NULL,
                    filename TEXT NOT NULL, destination TEXT NOT NULL,
                    created_at TEXT NOT NULL, drive_url TEXT, transcript TEXT,
                    study_notes TEXT, learning_status TEXT NOT NULL DEFAULT 'idle',
                    learning_error TEXT, learning_started_at REAL, learning_lease_until REAL,
                    learning_claim TEXT, media_index INTEGER NOT NULL DEFAULT 0,
                    audio_only INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS drive_requests (
                    user_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
                    claim_token TEXT NOT NULL, lease_until REAL NOT NULL, result_json TEXT,
                    PRIMARY KEY(user_id,request_id));
                CREATE INDEX IF NOT EXISTS items_owner ON items(user_id, created_at);
            ''')
            db.execute('BEGIN IMMEDIATE')
            columns = {row['name'] for row in db.execute('PRAGMA table_info(items)')}
            for name, declaration in {
                'media_index': 'INTEGER NOT NULL DEFAULT 0', 'audio_only': 'INTEGER NOT NULL DEFAULT 0',
                'learning_started_at': 'REAL', 'learning_lease_until': 'REAL', 'learning_claim': 'TEXT',
            }.items():
                if name not in columns:
                    db.execute(f'ALTER TABLE items ADD COLUMN {name} {declaration}')
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def encrypt(self, value):
        if not self.cipher:
            raise RuntimeError('Account encryption is not configured')
        return self.cipher.encrypt(json.dumps(value).encode()).decode()

    def decrypt(self, value):
        if not self.cipher or not value:
            return {}
        return json.loads(self.cipher.decrypt(value.encode()))

    def upsert_user(self, subject, email, name, tokens):
        with self.connect() as db:
            row = db.execute('SELECT * FROM users WHERE google_sub=?', (subject,)).fetchone()
            user_id = row['id'] if row else uuid.uuid4().hex
            old = self.decrypt(row['tokens']) if row else {}
            merged = {**old, **tokens}
            db.execute('''INSERT INTO users VALUES(?,?,?,?,?)
                ON CONFLICT(google_sub) DO UPDATE SET email=excluded.email,
                name=excluded.name,tokens=excluded.tokens''',
                (user_id, subject, email, name, self.encrypt(merged)))
        return {'id': user_id, 'email': email, 'name': name}

    def get_tokens(self, user_id):
        with self.connect() as db:
            row = db.execute('SELECT tokens FROM users WHERE id=?', (user_id,)).fetchone()
        return self.decrypt(row['tokens']) if row else {}

    def set_tokens(self, user_id, tokens):
        encrypted = self.encrypt(tokens)
        with self.connect() as db:
            db.execute('UPDATE users SET tokens=? WHERE id=?', (encrypted, user_id))

    def create_session(self, user_id, lifetime=7 * 86400):
        token = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?)',
                (self.digest(token), user_id, secrets.token_urlsafe(32), time.time() + lifetime))
        return token

    def get_session(self, token):
        if not token:
            return None
        with self.connect() as db:
            row = db.execute('''SELECT users.id,users.google_sub,users.email,users.name,
                sessions.csrf_token FROM sessions JOIN users ON users.id=sessions.user_id
                WHERE digest=? AND expires>?''', (self.digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def delete_session(self, token):
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE digest=?', (self.digest(token),))

    def create_oauth_state(self, payload):
        state = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('DELETE FROM oauth_states WHERE expires<?', (time.time(),))
            db.execute('INSERT INTO oauth_states VALUES(?,?,?)',
                (self.digest(state), self.encrypt(payload), time.time() + 600))
        return state

    def pop_oauth_state(self, state):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM oauth_states WHERE digest=?', (self.digest(state),)).fetchone()
            db.execute('DELETE FROM oauth_states WHERE digest=?', (self.digest(state),))
        return self.decrypt(row['payload']) if row and row['expires'] > time.time() else None

    def create_item(self, user_id, data, request_id=None, claim_token=None):
        item_id = uuid.uuid4().hex
        created = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        fields = ['source_url', 'title', 'platform', 'filename', 'destination', 'drive_url', 'media_index', 'audio_only']
        defaults = {'drive_url': None, 'media_index': 0, 'audio_only': False}
        values = [data.get(field, defaults.get(field, '')) for field in fields]
        with self.connect() as db:
            if request_id:
                db.execute('BEGIN IMMEDIATE')
                claim = db.execute('SELECT claim_token FROM drive_requests WHERE user_id=? AND request_id=?', (user_id, request_id)).fetchone()
                if not claim or claim['claim_token'] != claim_token:
                    raise RuntimeError('This Drive save claim has expired')
            db.execute('''INSERT INTO items(id,user_id,created_at,source_url,title,platform,
                filename,destination,drive_url,media_index,audio_only) VALUES(?,?,?,?,?,?,?,?,?,?,?)''',
                [item_id, user_id, created, *values])
            if request_id:
                row = db.execute('SELECT * FROM items WHERE user_id=? AND id=?', (user_id, item_id)).fetchone()
                db.execute('UPDATE drive_requests SET result_json=? WHERE user_id=? AND request_id=? AND claim_token=?',
                    (json.dumps({'item': self.public_item(row)}), user_id, request_id, claim_token))
        return self.get_item(user_id, item_id)

    @staticmethod
    def public_item(row):
        item = {key: value for key, value in dict(row).items() if key not in ('user_id', 'learning_started_at', 'learning_lease_until', 'learning_claim')}
        return {**item, 'audio_only': bool(item['audio_only'])}

    def list_items(self, user_id):
        self.recover_learning(user_id)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM items WHERE user_id=? ORDER BY created_at DESC,rowid DESC LIMIT 100', (user_id,)).fetchall()
        return [self.public_item(row) for row in rows]

    def get_item(self, user_id, item_id):
        with self.connect() as db:
            row = db.execute('SELECT * FROM items WHERE user_id=? AND id=?', (user_id, item_id)).fetchone()
        return self.public_item(row) if row else None

    def recover_learning(self, user_id):
        with self.connect() as db:
            db.execute("""UPDATE items SET learning_status='error',learning_claim=NULL,
                learning_error='Study generation was interrupted. Please try again.'
                WHERE user_id=? AND learning_status IN ('queued','transcribing','summarizing')
                AND learning_lease_until<?""", (user_id, time.time()))

    def claim_learning(self, user_id, item_id, status, lease_seconds=300):
        claim = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            result = db.execute("""UPDATE items SET learning_status=?,learning_error=NULL,
                learning_started_at=?,learning_lease_until=?,learning_claim=?
                WHERE user_id=? AND id=? AND (learning_status NOT IN ('queued','transcribing','summarizing')
                OR learning_lease_until IS NULL OR learning_lease_until<?)""",
                (status, now, now + lease_seconds, claim, user_id, item_id, now))
        return {**self.get_item(user_id, item_id), 'learning_claim': claim} if result.rowcount else None

    def renew_learning(self, user_id, item_id, claim_token, lease_seconds=300):
        with self.connect() as db:
            result = db.execute("""UPDATE items SET learning_lease_until=?
                WHERE user_id=? AND id=? AND learning_claim=?
                AND learning_status IN ('queued','transcribing','summarizing')""",
                (time.time() + lease_seconds, user_id, item_id, claim_token))
        return bool(result.rowcount)

    def update_learning(self, user_id, item_id, status, transcript=None, study_notes=None, error=None, claim_token=None):
        query = """UPDATE items SET learning_status=?,learning_error=?,
            transcript=COALESCE(?,transcript),study_notes=COALESCE(?,study_notes)
            WHERE user_id=? AND id=?"""
        values = [status, error, transcript, study_notes, user_id, item_id]
        if claim_token is not None:
            query += ' AND learning_claim=?'
            values = [*values, claim_token]
        with self.connect() as db:
            db.execute(query, values)
        return self.get_item(user_id, item_id)

    def claim_drive_request(self, user_id, request_id, payload_hash, lease_seconds=900):
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM drive_requests WHERE user_id=? AND request_id=?', (user_id, request_id)).fetchone()
            if row and row['payload_hash'] != payload_hash:
                return {'status': 'mismatch'}
            if row and row['result_json']:
                return {'status': 'done', 'result': json.loads(row['result_json'])}
            if row and row['lease_until'] > now:
                return {'status': 'busy'}
            claim = uuid.uuid4().hex
            db.execute("""INSERT INTO drive_requests VALUES(?,?,?,?,?,NULL)
                ON CONFLICT(user_id,request_id) DO UPDATE SET claim_token=excluded.claim_token,
                lease_until=excluded.lease_until,result_json=NULL""", (user_id, request_id, payload_hash, claim, now + lease_seconds))
        return {'status': 'claimed', 'claim_token': claim}

    def finish_drive_request(self, user_id, request_id, claim_token, result):
        with self.connect() as db:
            db.execute('UPDATE drive_requests SET result_json=? WHERE user_id=? AND request_id=? AND claim_token=?',
                (json.dumps(result), user_id, request_id, claim_token))

    def abandon_drive_request(self, user_id, request_id, claim_token):
        with self.connect() as db:
            db.execute('DELETE FROM drive_requests WHERE user_id=? AND request_id=? AND claim_token=? AND result_json IS NULL',
                (user_id, request_id, claim_token))
