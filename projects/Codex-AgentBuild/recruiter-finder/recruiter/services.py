import os
import subprocess
import logging
from datetime import datetime, timedelta
import requests
from .core import reserve, ingest, now, format_post, reevaluate, canonical

LOG = logging.getLogger('finder')

def record_error(db, stage, exc):
    # Never persist exception text: HTTP errors may contain credential-bearing URLs.
    with db:
        db.execute('INSERT INTO errors VALUES (?,?,?)', (now().isoformat(), stage, type(exc).__name__))
    LOG.warning('%s: %s', stage, type(exc).__name__)

def search(db, cfg):
    key = os.environ.get('SERPAPI_KEY')
    if not key:
        raise RuntimeError('Run recruiter setup to configure SerpApi')
    failures = 0
    diagnostics = []
    for index, query in enumerate(cfg['queries'][:3]):
        settings = cfg.get('query_settings', [{} for _ in cfg['queries']])[index]
        engine = settings.get('engine', 'google')
        params = {'api_key': key, 'engine': engine, 'q': query}
        if engine == 'google':
            params.update(gl='us', hl='en', num=10)
            date_filter = settings.get('date_filter', cfg.get('search_date_filter', 'qdr:d3'))
            if date_filter:
                params['tbs'] = date_filter
        elif engine == 'duckduckgo':
            params.update(kl='us-en', m=10)
            if settings.get('date_filter'):
                params['df'] = settings['date_filter']
        try:
            reserve(db, cfg['monthly_request_limit'])
            response = requests.get('https://serpapi.com/search.json', params=params, timeout=(10, 45))
            response.raise_for_status()
            data = response.json()
            if data.get('error') and not any(phrase in str(data['error']).lower() for phrase in ('no results', "hasn't returned any results", 'no organic results')):
                raise RuntimeError('Search provider rejected query')
            results = data.get('organic_results', [])
            accepted = sum(canonical(r.get('link','')) is not None for r in results)
            diagnostics.append({'warning': 'No LinkedIn post URLs in returned results' if results and not accepted else None, 'query': query, 'engine': engine, 'date_filter': settings.get('date_filter',cfg.get('search_date_filter','qdr:d3')), 'search_id': data.get('search_metadata',{}).get('id'), 'results': len(results),
                'linkedin_posts': sum(canonical(r.get('link', '')) is not None for r in results)})
            for result in results:
                ingest(db, result, cfg)
        except Exception as exc:
            failures += 1
            diagnostics.append({'query': query, 'error': type(exc).__name__})
            record_error(db, 'search', exc)
    from .public_posts import enrich
    enrich(db, cfg)
    reevaluate(db, cfg)
    import json
    with db:
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('search_diagnostics', json.dumps(diagnostics)))
    return failures

def twilio_client():
    from twilio.rest import Client
    from twilio.http.http_client import TwilioHttpClient
    return Client(os.environ['TWILIO_ACCOUNT_SID'], os.environ['TWILIO_AUTH_TOKEN'],
                  http_client=TwilioHttpClient(timeout=30))

def send(body, cfg):
    if cfg['provider'] == 'messages':
        script = '''on run argv
        tell application "Messages"
          set svc to first service whose service type = iMessage
          set recipient to buddy (item 1 of argv) of svc
          send (item 2 of argv) to recipient
        end tell
        end run'''
        subprocess.run(['osascript', '-e', script, cfg['destination'], body],
                       check=True, capture_output=True, timeout=45)
        return 'apple-submitted', 'submitted-unverified'
    message = twilio_client().messages.create(body=body, from_=os.environ['TWILIO_FROM'], to=cfg['destination'])
    return message.sid, message.status

def notify(db, cfg):
    if not cfg.get('notifications_enabled'):
        return
    reevaluate(db, cfg)
    rows = db.execute("SELECT * FROM posts WHERE status IN ('pending','retry') AND (next_retry IS NULL OR next_retry<=?) ORDER BY score DESC", (now().isoformat(),)).fetchall()
    fresh = []
    for row in rows:
        if now() - datetime.fromisoformat(row['published']) > timedelta(hours=72):
            with db:
                db.execute("UPDATE posts SET status='old' WHERE id=?", (row['id'],))
        else:
            fresh.append(row)
    fresh = fresh[:cfg['max_notifications']]
    groups = [fresh] if cfg.get('digest') and fresh else [[row] for row in fresh]
    for group in groups:
        # Persist before I/O. An ambiguous crash/timeout requires review rather than
        # automatically duplicating a potentially delivered message.
        with db:
            db.executemany("UPDATE posts SET status='sending',attempts=attempts+1 WHERE id=?", [(r['id'],) for r in group])
        try:
            sid, delivery = send('\n\n'.join(format_post(r) for r in group), cfg)
            with db:
                db.executemany("UPDATE posts SET status='submitted',provider_id=?,delivery=? WHERE id=?",
                               [(sid, delivery, r['id']) for r in group])
        except Exception as exc:
            from twilio.base.exceptions import TwilioRestException
            # Only explicit rejections that cannot have sent are safe to retry.
            safe = isinstance(exc, TwilioRestException) and exc.status == 429
            with db:
                db.executemany("UPDATE posts SET status=?,next_retry=? WHERE id=?",
                    [('retry' if safe and r['attempts'] < 4 else 'uncertain',
                      (now() + timedelta(minutes=15)).isoformat(), r['id']) for r in group])
            record_error(db, 'notification', exc)

def reconcile(db, cfg):
    if cfg['provider'] != 'twilio' or not os.getenv('TWILIO_ACCOUNT_SID'):
        return
    for row in db.execute("SELECT * FROM posts WHERE status='submitted' AND delivery NOT IN ('delivered','failed','undelivered')").fetchall():
        try:
            message = twilio_client().messages(row['provider_id']).fetch()
            with db:
                db.execute('UPDATE posts SET delivery=? WHERE id=?', (message.status, row['id']))
        except Exception as exc:
            record_error(db, 'delivery', exc)
