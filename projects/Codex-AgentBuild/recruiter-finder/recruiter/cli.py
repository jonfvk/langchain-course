import argparse
import fcntl
import getpass
import html
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import subprocess
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv, set_key
from .core import ROOT, connect, now, due_slot, next_slot
from .services import search, notify, send, reconcile, record_error
from . import scheduler

ENV = ROOT / '.env'
CONFIG = ROOT / 'config.json'

def save(cfg):
    CONFIG.write_text(json.dumps(cfg, indent=2) + '\n')

def secret(key):
    value = getpass.getpass(f'{key} (hidden; Enter keeps existing): ').strip()
    if value:
        set_key(str(ENV), key, value)
    os.chmod(ENV, 0o600)
    load_dotenv(ENV, override=True)

def setup(cfg, db):
    print('Opening SerpApi. Sign up/sign in, then copy your API key from Dashboard.\nNo purchase is needed by this wizard. Choose a plan yourself if necessary.')
    webbrowser.open('https://serpapi.com/manage-api-key')
    secret('SERPAPI_KEY')
    if os.getenv('SERPAPI_KEY') and input('Run three real search queries, charged to your API allowance? [y/N] ').lower() == 'y':
        print('Failed queries:', search(db, cfg))
    choice = input('Notification provider: twilio or messages [twilio]: ').strip() or 'twilio'
    if choice not in ('twilio', 'messages'):
        raise ValueError('Choose twilio or messages')
    cfg['provider'] = choice
    cfg['notifications_enabled'] = False
    save(cfg)
    if choice == 'twilio':
        webbrowser.open('https://console.twilio.com/')
        print('Sign in/create an account. Console > Account Dashboard: copy Account SID and Auth Token.\nPhone Numbers > Manage > Active Numbers: select your SMS-capable number.\nTrial accounts require verifying the destination in Verified Caller IDs.\nUS long-code sending requires the applicable A2P 10DLC registration; toll-free sending requires verification.\nComplete the Console registration/verification for your number; this wizard never buys numbers or upgrades accounts.')
        for key in ('TWILIO_ACCOUNT_SID', 'TWILIO_AUTH_TOKEN', 'TWILIO_FROM'):
            secret(key)
    else:
        subprocess.run(['open', '-a', 'Messages'], check=True)
        print('Messages > Settings > iMessage: sign in. Allow the macOS Automation prompt for Messages.\nIf denied: System Settings > Privacy & Security > Automation > allow Messages for your terminal.\nSelf-addressed iMessages may sync without an iPhone notification; verify on the locked phone.')
    if input('Authorize one real test message to your configured phone? [y/N] ').lower() == 'y':
        print('Provider response:', send('Recruiter Finder test — please confirm receipt on your iPhone.', cfg))
        delivered = input('Did the iPhone receive and notify you? [y/N] ').lower() == 'y'
        if delivered:
            cfg['notifications_enabled'] = input('Authorize automatic alerts to this number? [y/N] ').lower() == 'y'
            save(cfg)
    print('Saved. Activate with: .venv/bin/recruiter start')

def scan(db, cfg):
    failures = search(db, cfg)
    notify(db, cfg)
    with db:
        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('last_scan', now().isoformat()))
    return failures

def status(db, cfg):
    return {'last_scan': dict(db.execute('SELECT key,value FROM meta')),
            'next_scan_eastern': next_slot(), 'notifications_enabled': cfg['notifications_enabled'],
            'provider': cfg['provider'],
            'post_counts': {r[0]:r[1] for r in db.execute('SELECT status,count(*) FROM posts GROUP BY status')},
            'usage': [dict(r) for r in db.execute('SELECT * FROM usage')],
            'posts': [dict(r) for r in db.execute('SELECT * FROM posts ORDER BY found DESC LIMIT 200')],
            'errors': [dict(r) for r in db.execute('SELECT * FROM errors ORDER BY time DESC LIMIT 30')]}

def dashboard(db, cfg):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            data = status(db, cfg)
            body = '<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="60"><title>Recruiter Finder</title><h1>Recruiter Finder</h1>'
            body += '<pre>' + html.escape(json.dumps({k:v for k,v in data.items() if k != 'posts'}, indent=2)) + '</pre>'
            body += '<table><tr><th>Original post</th><th>Published (UTC)</th><th>Score</th><th>Alert</th></tr>'
            for r in data['posts']:
                body += f'<tr><td><a rel="noreferrer" href="{html.escape(r["url"], quote=True)}">{html.escape(r["title"])}</a></td><td>{html.escape(str(r["published"]))}</td><td>{r["score"]}</td><td>{html.escape(r["status"] + " / " + str(r["delivery"]))}</td></tr>'
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Content-Security-Policy', "default-src 'none'; base-uri 'none'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(body.encode())
        def log_message(self, *args):
            pass
    print('Dashboard: http://127.0.0.1:8765 (Ctrl-C to close)')
    webbrowser.open('http://127.0.0.1:8765')
    HTTPServer(('127.0.0.1', 8765), Handler).serve_forever()

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['setup','scan','tick','matches','status','logs','test-sms','start','stop','dashboard'])
    args = parser.parse_args()
    ENV.touch(mode=0o600, exist_ok=True)
    os.chmod(ENV, 0o600)
    load_dotenv(ENV)
    cfg = json.loads(CONFIG.read_text())
    db = connect()
    handler = RotatingFileHandler(ROOT / 'state/finder.log', maxBytes=500000, backupCount=3)
    logging.getLogger('finder').addHandler(handler)
    lock = open(ROOT / 'state/process.lock', 'a')
    try:
        if args.command in ('scan','tick','setup','test-sms'):
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'setup':
            setup(cfg, db)
        elif args.command == 'test-sms':
            if input('Authorize one real test message? [y/N] ').lower() == 'y':
                print('Provider response:', send('Recruiter Finder delivery test', cfg))
                print('Verify actual receipt on your iPhone; provider acceptance is not delivery.')
        elif args.command in ('scan','tick'):
            if args.command == 'scan':
                print('Failed queries:', scan(db, cfg))
            else:
                slot = due_slot()
                previous = db.execute("SELECT value FROM meta WHERE key='slot'").fetchone()
                attempt = db.execute("SELECT value FROM meta WHERE key='attempt'").fetchone()
                from datetime import datetime, timedelta
                if (not previous or previous[0] != slot) and (not attempt or now() - datetime.fromisoformat(attempt[0]) >= timedelta(minutes=30)):
                    with db:
                        db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('attempt', now().isoformat()))
                    if scan(db, cfg) == 0:
                        with db:
                            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('slot', slot))
                else:
                    notify(db, cfg)
                reconcile(db, cfg)
        elif args.command == 'start':
            if not os.getenv('SERPAPI_KEY') or not cfg['notifications_enabled']:
                raise RuntimeError('Complete setup and confirm live alert delivery before activation')
            scheduler.install()
            print('Installed: checks every 5 minutes; scans at 08:00/17:00 America/New_York with wake catch-up.')
        elif args.command == 'stop':
            scheduler.stop()
        elif args.command == 'dashboard':
            dashboard(db, cfg)
        elif args.command == 'logs':
            print((ROOT / 'state/finder.log').read_text()[-12000:])
        elif args.command == 'matches':
            print(json.dumps([dict(r) for r in db.execute("SELECT * FROM posts WHERE status NOT IN ('old','irrelevant') ORDER BY score DESC")], indent=2))
        else:
            print(json.dumps(status(db, cfg), indent=2))
            service = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{scheduler.LABEL}'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print('LaunchAgent file installed:', scheduler.path().exists())
            print('LaunchAgent loaded:', service.returncode == 0)
    except Exception as exc:
        record_error(db, args.command, exc)
        print(f'{args.command} failed ({type(exc).__name__}). Check setup, permissions, and recruiter logs. Secrets suppressed.')
        raise SystemExit(1)
    finally:
        db.close()
        lock.close()

if __name__ == '__main__':
    main()
