"""SQLite intent reservation and append-only sanitized paper-session records."""
import json
import sqlite3
from datetime import datetime, timezone

# Known token fields are recursively omitted. Callers supply only curated broker fields.
def safe(value):
    if isinstance(value,dict):
        return {k:safe(v) for k,v in value.items() if not any(x in k.lower() for x in ('token','secret','authorization','password','digest'))}
    if isinstance(value,(tuple,list)):return [safe(v) for v in value]
    return value

class PaperJournal:
    def __init__(self,path):
        self.db=sqlite3.connect(path,timeout=10)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS intents (id TEXT PRIMARY KEY, state TEXT NOT NULL, order_id TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, timestamp TEXT, intent_id TEXT, kind TEXT, payload TEXT)')
        self.db.commit()
    def seen(self,key):return self.db.execute('SELECT 1 FROM intents WHERE id=?',(key,)).fetchone() is not None
    def unresolved(self):return self.db.execute("SELECT count(*) FROM intents WHERE state IN ('SUBMITTING','UNKNOWN','WORKING')").fetchone()[0]>0
    def reserve(self,key):
        with self.db:
            self.db.execute('BEGIN IMMEDIATE')
            if self.unresolved():raise ValueError('Unreconciled prior submission')
            self.db.execute('INSERT INTO intents(id,state) VALUES (?,?)',(key,'SUBMITTING'))
    def status(self,key,status,order_id=None):
        with self.db:self.db.execute('UPDATE intents SET state=?,order_id=COALESCE(?,order_id) WHERE id=?',(status,order_id,key))
    def record(self,kind,payload,intent_id=None):
        with self.db:self.db.execute('INSERT INTO events(timestamp,intent_id,kind,payload) VALUES (?,?,?,?)',
            (datetime.now(timezone.utc).isoformat(),intent_id,kind,json.dumps(safe(payload),allow_nan=False)))
    def count(self):return self.db.execute('SELECT count(*) FROM events').fetchone()[0]
    def close(self):self.db.close()
