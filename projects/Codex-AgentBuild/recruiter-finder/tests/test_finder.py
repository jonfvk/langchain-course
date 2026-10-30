import json
import os
import plistlib
from datetime import timedelta, datetime, timezone
from unittest.mock import Mock
import pytest
from recruiter import core, services, scheduler
from recruiter.core import now, canonical, ingest, connect, reserve, score, format_post, due_slot

@pytest.fixture
def cfg():
    value = json.loads((core.ROOT / 'config.json').read_text())
    value['require_public_post'] = False
    return value

@pytest.fixture
def db(tmp_path):
    connection = connect(tmp_path)
    yield connection
    connection.close()

def result(age=2, id_override=None):
    key = id_override or str(int((now() - timedelta(hours=age)).timestamp() * 1000) << 22)
    return {'link': f'https://www.linkedin.com/posts/jane_hiring-activity-{key}-abc?utm_source=x',
            'title': 'Jane: I’m hiring Senior Cloud Security Engineer',
            'snippet': 'My team is hiring remote AWS Python cloud security engineers. Message me.'}

def test_recent(db, cfg):
    ingest(db, result(), cfg)
    r = db.execute('SELECT * FROM posts').fetchone()
    assert r['status'] == 'pending'
    assert r['published'] and r['score'] >= cfg['threshold']
    assert 'LINK TO ORIGINAL POST' in format_post(r)
    assert 'utm_' not in r['url']

def test_old(db, cfg):
    ingest(db, result(73), cfg)
    assert db.execute('SELECT status FROM posts').fetchone()[0] == 'old'

def test_unverified_date(db, cfg):
    r = result(id_override='unknown')
    r['date'] = '2 hours ago'
    ingest(db, r, cfg)
    assert db.execute('SELECT status FROM posts').fetchone()[0] == 'review'

@pytest.mark.parametrize('url', ['https://www.linkedin.com/jobs/view/123', 'https://indeed.com/posts/1', 'https://linkedin.com.evil.com/posts/1', 'http://linkedin.com/posts/1', 'https://www.linkedin.com/articles/1'])
def test_exclude(url):
    assert canonical(url) is None

def test_duplicates(db, cfg):
    r = result()
    ingest(db, r, cfg)
    key = canonical(r['link'])[0]
    r['link'] = 'https://www.linkedin.com/feed/update/urn:li:activity:' + key
    ingest(db, r, cfg)
    assert db.execute('SELECT count(*) FROM posts').fetchone()[0] == 1

def test_score(cfg):
    assert score('Senior SRE: my team is hiring remote AWS Python engineers', cfg) >= 60
    assert score('SRE career advice and resume tips', cfg) == 0
    assert score('AWS tutorial for DevOps engineers', cfg) == 0
    assert score('We are hiring a chef', cfg) == 0

def test_budget(db):
    reserve(db, 1)
    with pytest.raises(RuntimeError):
        reserve(db, 1)
    assert db.execute('SELECT requests FROM usage').fetchone()[0] == 1

def test_search(db, cfg, monkeypatch):
    monkeypatch.setenv('SERPAPI_KEY', 'SECRET')
    response = Mock()
    response.json.return_value = {'organic_results': [result()]}
    get = Mock(return_value=response)
    monkeypatch.setattr(services.requests, 'get', get)
    assert services.search(db, cfg) == 0
    assert get.call_count == 3
    assert db.execute('SELECT count(*) FROM posts').fetchone()[0] == 1

def test_unavailable_and_redaction(db, cfg, monkeypatch):
    monkeypatch.setenv('SERPAPI_KEY', 'SECRET')
    monkeypatch.setattr(services.requests, 'get', Mock(side_effect=RuntimeError('SECRET')))
    assert services.search(db, cfg) == 3
    assert 'SECRET' not in str([tuple(r) for r in db.execute('SELECT * FROM errors')])

def test_no_credentials(db, cfg, monkeypatch):
    monkeypatch.delenv('SERPAPI_KEY', raising=False)
    with pytest.raises(RuntimeError):
        services.search(db, cfg)

def test_notify_once(db, cfg, monkeypatch):
    cfg['notifications_enabled'] = True
    ingest(db, result(), cfg)
    sender = Mock(return_value=('SM123', 'queued'))
    monkeypatch.setattr(services, 'send', sender)
    services.notify(db, cfg)
    services.notify(db, cfg)
    assert sender.call_count == 1
    assert db.execute('SELECT status FROM posts').fetchone()[0] == 'submitted'

def test_retry_explicit_rejection(db, cfg, monkeypatch):
    from twilio.base.exceptions import TwilioRestException
    cfg['notifications_enabled'] = True
    ingest(db, result(), cfg)
    monkeypatch.setattr(services, 'send', Mock(side_effect=TwilioRestException(429, '/messages')))
    services.notify(db, cfg)
    assert db.execute('SELECT status FROM posts').fetchone()[0] == 'retry'
    with db:
        db.execute('UPDATE posts SET next_retry=NULL')
    monkeypatch.setattr(services, 'send', Mock(return_value=('SM123', 'queued')))
    services.notify(db, cfg)
    assert db.execute('SELECT status FROM posts').fetchone()[0] == 'submitted'

def test_ambiguous_no_duplicate(db, cfg, monkeypatch):
    cfg['notifications_enabled'] = True
    ingest(db, result(), cfg)
    sender = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(services, 'send', sender)
    services.notify(db, cfg)
    services.notify(db, cfg)
    assert sender.call_count == 1
    assert db.execute('SELECT status FROM posts').fetchone()[0] == 'uncertain'

def test_disabled(db, cfg, monkeypatch):
    cfg['notifications_enabled'] = False
    ingest(db, result(), cfg)
    sender = Mock()
    monkeypatch.setattr(services, 'send', sender)
    services.notify(db, cfg)
    sender.assert_not_called()

def test_expired_not_sent(db, cfg, monkeypatch):
    cfg['notifications_enabled'] = True
    ingest(db, result(), cfg)
    with db:
        db.execute('UPDATE posts SET published=?', ((now()-timedelta(hours=73)).isoformat(),))
    sender = Mock()
    monkeypatch.setattr(services, 'send', sender)
    services.notify(db, cfg)
    sender.assert_not_called()

def test_schedule():
    p = plistlib.loads(plistlib.dumps(scheduler.plist()))
    assert p['StartInterval'] == 300
    assert p['RunAtLoad']
    assert p['ProgramArguments'][-1] == 'tick'
    assert due_slot(datetime(2026, 10, 21, 12, 0, tzinfo=timezone.utc)).endswith('08:00:00-04:00')
    assert due_slot(datetime(2026, 12, 21, 13, 0, tzinfo=timezone.utc)).endswith('08:00:00-05:00')
    assert due_slot(datetime(2026, 10, 21, 11, 0, tzinfo=timezone.utc)).startswith('2026-10-20T17:00')

def test_scheduler_install(tmp_path, monkeypatch):
    target = tmp_path / 'launch' / 'app.plist'
    monkeypatch.setattr(scheduler, 'path', lambda: target)
    run = Mock()
    monkeypatch.setattr(scheduler.subprocess, 'run', run)
    scheduler.install()
    assert target.exists()
    assert target.stat().st_mode & 0o777 == 0o600
    assert run.call_args.args[0][1] == 'bootstrap'

def test_secure_env(tmp_path, monkeypatch):
    from recruiter import cli
    env = tmp_path / '.env'
    env.touch(mode=0o600)
    monkeypatch.setattr(cli, 'ENV', env)
    monkeypatch.setattr(cli.getpass, 'getpass', lambda prompt: 'test-only-token')
    monkeypatch.setattr(cli, 'load_dotenv', lambda *args, **kwargs: None)
    cli.secret('SERPAPI_KEY')
    assert env.stat().st_mode & 0o777 == 0o600
    assert 'test-only-token' in env.read_text()
    assert 'SERPAPI_KEY' not in json.loads((core.ROOT / 'config.json').read_text())

def test_month_rollover(db, monkeypatch):
    monkeypatch.setattr(core, 'now', lambda: datetime(2026, 1, 31, tzinfo=timezone.utc))
    reserve(db, 1)
    monkeypatch.setattr(core, 'now', lambda: datetime(2026, 2, 1, tzinfo=timezone.utc))
    reserve(db, 1)
    assert db.execute('SELECT count(*) FROM usage').fetchone()[0] == 2

def test_fixed_activity_timestamp():
    # Published reference example; ensures decoding isn't tested solely by its inverse.
    assert core.publication('7416739544328249344').replace(microsecond=0) == datetime(2026, 1, 13, 7, 15, 3, tzinfo=timezone.utc)

@pytest.mark.parametrize('text,expected', [
    ('Senior SRE hiring remote AWS Python', 'allowed'),
    ('Senior SRE hiring onsite in Charlotte NC', 'allowed'),
    ('Senior SRE hiring hybrid Charlotte, North Carolina', 'allowed'),
    ('Senior SRE hiring onsite in Raleigh NC', 'excluded_location'),
    ('Senior SRE hiring hybrid in New York', 'excluded_location'),
    ('Senior SRE hiring remote or hybrid in Boston', 'review_location'),
    ('Senior SRE hiring no remote in Dallas', 'excluded_location'),
    ('Senior SRE hiring remote - UK only', 'excluded_location'),
    ('Senior SRE hiring', 'review_location'),
])
def test_strict_location(cfg, text, expected):
    cfg['require_location_match'] = True
    assert core.location_status(text,cfg) == expected

def test_recheck_location_before_send(db,cfg,monkeypatch):
    cfg['notifications_enabled'] = True
    cfg['require_location_match'] = False
    r = result()
    r['snippet'] = 'My team is hiring Senior AWS Python cloud security engineers onsite in Seattle.'
    ingest(db,r,cfg)
    cfg['require_location_match'] = True
    sender = Mock()
    monkeypatch.setattr(services,'send',sender)
    services.notify(db,cfg)
    sender.assert_not_called()
    assert db.execute('SELECT status FROM posts').fetchone()[0] == 'excluded_location'

def test_charlotte_wrong_state(cfg):
    cfg['require_location_match'] = True
    assert core.location_status('Hiring on-site in Charlotte, Michigan', cfg) == 'excluded_location'

@pytest.mark.parametrize('text,allowed', [
    ("I'm hiring a remote Senior DevOps engineer", True),
    ('My team is hiring a remote Cloud Security Engineer', True),
    ('I can refer you for our remote SRE opening', True),
    ('Hiring Senior DevOps Engineer remote. Apply now. Job ID 123', False),
    ('Acme is hiring a remote Senior Security Engineer. Apply today.', False),
    ('Remote SRE jobs available on LinkedIn Jobs', False),
])
def test_personal_post(text,allowed):
    assert core.personal_post(text) is allowed

def test_generic_post_not_alerted(db,cfg,monkeypatch):
    cfg['notifications_enabled'] = True
    r=result(); r['title']='Hiring Senior Cloud Security Engineer'
    r['snippet']='Remote AWS Python security engineer opening. Apply now at Acme.'
    ingest(db,r,cfg)
    sender=Mock(); monkeypatch.setattr(services,'send',sender)
    services.notify(db,cfg)
    sender.assert_not_called()
    assert db.execute('SELECT status FROM posts').fetchone()[0]=='review_person'
