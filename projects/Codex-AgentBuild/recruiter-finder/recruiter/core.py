"""Conservative discovery: only LinkedIn activity IDs establish publication time.
Google's date/snippet are evidence for review, never publication verification.
"""
import json
import re
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlsplit

UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[1]

def now():
    return datetime.now(UTC)

def canonical(url):
    p = urlsplit(url)
    if p.scheme != 'https' or not re.fullmatch(r'(?:www\.|[a-z]{2}\.)?linkedin\.com', p.hostname or ''):
        return None
    if not (p.path.startswith('/posts/') or p.path.startswith('/feed/update/urn:li:activity:')):
        return None
    match = re.search(r'(?:activity[-:]|ugcPost:)(\d{18,20})', p.path)
    key = match.group(1) if match else p.path.rstrip('/')
    return key, 'https://www.linkedin.com' + p.path.rstrip('/')

def publication(key):
    # LinkedIn activity snowflake: high 41 bits encode Unix milliseconds.
    # This timestamps the original activity, not Google's indexing event.
    if not key.isdigit():
        return None
    try:
        stamp = datetime.fromtimestamp((int(key) >> 22) / 1000, UTC)
        return stamp if datetime(2010, 1, 1, tzinfo=UTC) <= stamp <= now() else None
    except (ValueError, OverflowError, OSError):
        return None

def score(text, cfg):
    t = text.lower()
    roles = any(re.search(r'(?<!\w)' + re.escape(x) + r'(?!\w)', t) for x in cfg['roles'])
    hiring = bool(re.search(r"\bhiring\b|\bopenings?\b|join (?:my|our) team|\breferrals?\b|team is (?:growing|expanding)", t))
    if not roles or not hiring or re.search(r'career advice|resume tips|job search tips|how to get hired', t):
        return 0
    points = 45
    points += min(15, 5 * sum(x.lower() in t for x in cfg['skills']))
    points += 10 * bool(re.search(r'\b(senior|sr\.?|lead|staff|principal|architect)\b', t))
    points += 10 * any(x.lower() in t for x in cfg['locations'])
    points += 10 * bool(re.search(r'my team|our team|i.m hiring|we.re hiring|message me|reach out|referral', t))
    points += 5 * any(x.lower() in t for x in cfg['preferred_companies'])
    salaries = re.findall(r'\$([\d,]+)(k)?', t)
    points += 5 * any(int(a.replace(',', '')) * (1000 if k else 1) >= cfg['salary_preference'] for a, k in salaries)
    return min(100, points)

def location_status(text, cfg):
    """Require explicit geography/work arrangement; snippets are incomplete evidence."""
    if not cfg.get('require_location_match', False):
        return 'allowed'
    t = text.lower()
    t = re.sub(r'remote (?:access|desktop|monitoring|management|procedure call|sensing)', '', t)
    charlotte = bool(re.search(r'\bcharlotte(?:\s*,?\s*(?:nc|north carolina))?\b', t))
    onsite = bool(re.search(r'\bon[ -]?site\b|\bhybrid\b|in[ -]office|office attendance', t))
    negated_remote = bool(re.search(r'not (?:a )?remote|no remote|remote (?:is )?not|non[ -]remote', t))
    remote = bool(re.search(r'\bremote\b|work from (?:home|anywhere)|distributed team', t)) and not negated_remote
    restricted = bool(re.search(r'(?:uk|india|canada|europe|eu|emea|apac|australia)[ -]only|only (?:in )?(?:the )?(?:uk|india|canada|europe|eu|emea|apac|australia)|remote\s*[-:(]\s*(?:uk|india|canada|europe|eu|emea|apac|australia)\b', t))
    if charlotte and re.search(r'charlotte\s*,?\s*(?:mi\b|michigan|va\b|virginia)', t):
        return 'excluded_location'
    if restricted:
        return 'excluded_location'
    if onsite or negated_remote:
        # Mixed-role snippets can combine remote and on-site jobs; require review.
        return 'allowed' if charlotte else ('review_location' if remote else 'excluded_location')
    if remote or charlotte:
        return 'allowed'
    return 'review_location'

def personal_post(text):
    """Require first-person hiring/referral evidence; never infer a human author."""
    t = text.lower().replace('’', "'")
    return bool(re.search(
        r"\bi(?:'m| am) (?:currently )?(?:hiring|recruiting|looking for)\b|"
        r"\bmy team\b|\b(?:message|dm|contact) me\b|"
        r"\bi (?:can|will|would be happy to) (?:refer|recommend)\b|"
        r"\breach out to me\b", t))

def classify(stamp, relevance, text, cfg, person_verified=False):
    if stamp is None:
        return 'review'
    if now() - stamp > timedelta(hours=72):
        return 'old'
    if relevance < cfg['threshold']:
        return 'irrelevant'
    if cfg.get('require_public_post') and not person_verified:
        return 'review_person'
    if not person_verified and not personal_post(text):
        return 'review_person'
    location = location_status(text, cfg)
    return 'pending' if location == 'allowed' else location

def reevaluate(db, cfg):
    # Never reopen submitted, ambiguous, or interrupted sends.
    for row in db.execute("SELECT * FROM posts WHERE status NOT IN ('submitted','sending','uncertain')").fetchall():
        text = row['title'] + ' ' + (row['post_text'] or row['snippet'])
        stamp = datetime.fromisoformat(row['published']) if row['published'] else None
        relevance = score(text, cfg)
        verified = row['verification_status'] == 'verified' and row['author_type'] == 'Person' and bool(row['post_text'])
        status = classify(stamp, relevance, text, cfg, person_verified=verified)
        if row['verification_status'] == 'not_person':
            status = 'excluded_not_person'
        if status == 'pending' and row['status'] == 'retry':
            status = 'retry'
        with db:
            db.execute('UPDATE posts SET score=?,status=? WHERE id=?', (relevance,status,row['id']))

def connect(root=ROOT):
    state = root / 'state'
    state.mkdir(mode=0o700, exist_ok=True)
    db = sqlite3.connect(state / 'finder.sqlite', timeout=30)
    db.row_factory = sqlite3.Row
    db.executescript('''
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS posts (
      id TEXT PRIMARY KEY, url TEXT, title TEXT, snippet TEXT, published TEXT,
      date_evidence TEXT, score INTEGER, status TEXT, attempts INTEGER DEFAULT 0,
      provider_id TEXT, delivery TEXT, found TEXT, next_retry TEXT);
    CREATE TABLE IF NOT EXISTS usage (month TEXT PRIMARY KEY, requests INTEGER);
    CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS errors (time TEXT, stage TEXT, kind TEXT);
    ''')
    columns = {r[1] for r in db.execute('PRAGMA table_info(posts)')}
    with db:
        for column in ('post_text','author','author_url','author_type','verified_at','verification_status'):
            if column not in columns:
                db.execute(f'ALTER TABLE posts ADD COLUMN {column} TEXT')
    return db

def reserve(db, limit):
    month = now().strftime('%Y-%m')
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('INSERT OR IGNORE INTO usage VALUES (?,0)', (month,))
        used = db.execute('SELECT requests FROM usage WHERE month=?', (month,)).fetchone()[0]
        if used >= limit:
            raise RuntimeError('Monthly request limit reached')
        db.execute('UPDATE usage SET requests=requests+1 WHERE month=?', (month,))

def ingest(db, result, cfg):
    identity = canonical(result.get('link', ''))
    if not identity:
        return
    key, url = identity
    stamp = publication(key)
    title, snippet = result.get('title', ''), result.get('snippet', '')
    relevance = score(title + ' ' + snippet, cfg)
    status = classify(stamp, relevance, title + ' ' + snippet, cfg)
    with db:
        db.execute('''INSERT OR IGNORE INTO posts
          (id,url,title,snippet,published,date_evidence,score,status,found)
          VALUES (?,?,?,?,?,?,?,?,?)''', (key,url,title,snippet,stamp.isoformat() if stamp else None,
          'activity ID timestamp (derived)' if stamp else str(result.get('date', 'unverified')),
          relevance,status,now().isoformat()))

def format_post(row):
    hours = max(0, int((now() - datetime.fromisoformat(row['published'])).total_seconds() // 3600))
    keys = row.keys()
    poster = f"Poster: {row['author']}\n" if 'author' in keys and row['author'] else ''
    excerpt = row['post_text'] if 'post_text' in keys and row['post_text'] else row['snippet']
    age_source = 'public post date' if row['date_evidence'] == 'public post datePublished' else 'activity ID'
    return (f"NEW LINKEDIN HIRING POST\n{row['title'][:100]}\n"
            f"{poster}Posted: {hours}h ago ({age_source}) | Match: {row['score']}/100\n"
            f"{excerpt[:200]}\nLINK TO ORIGINAL POST:\n{row['url']}")

def due_slot(at=None):
    from zoneinfo import ZoneInfo
    at = (at or now()).astimezone(ZoneInfo('America/New_York'))
    for hour in (17, 8):
        slot = at.replace(hour=hour, minute=0, second=0, microsecond=0)
        if at >= slot:
            return slot.isoformat()
    return (at - timedelta(days=1)).replace(hour=17, minute=0, second=0, microsecond=0).isoformat()

def next_slot(at=None):
    from zoneinfo import ZoneInfo
    at = (at or now()).astimezone(ZoneInfo('America/New_York'))
    for hour in (8, 17):
        slot = at.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > at:
            return slot.isoformat()
    return (at + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0).isoformat()
