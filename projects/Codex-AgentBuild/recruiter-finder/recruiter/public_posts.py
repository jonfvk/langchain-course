"""Read only unauthenticated public post metadata. Never follow login redirects."""
import json
from datetime import datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit
import requests
from .core import canonical, now, score

class MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.active = False
        self.parts = []
        self.documents = []
    def handle_starttag(self, tag, attrs):
        if tag == 'script':
            self.active = dict(attrs).get('type') == 'application/ld+json'
            self.parts = []
    def handle_data(self, data):
        if self.active:
            self.parts.append(data)
    def handle_endtag(self, tag):
        if tag == 'script' and self.active:
            try:
                self.documents.append(json.loads(''.join(self.parts)))
            except (ValueError, TypeError):
                pass
            self.active = False

def documents(value):
    if isinstance(value, list):
        for item in value:
            yield from documents(item)
    elif isinstance(value, dict):
        yield value
        yield from documents(value.get('@graph', []))

def parse_metadata(page, url):
    identity = canonical(url)
    if not identity:
        return None
    parser = MetadataParser()
    parser.feed(page)
    for doc in documents(parser.documents):
        types = doc.get('@type', [])
        if 'SocialMediaPosting' not in ([types] if isinstance(types,str) else types):
            continue
        declared = doc.get('@id', doc.get('url', ''))
        declared = canonical(declared) if isinstance(declared,str) else None
        if not declared or declared[0] != identity[0]:
            continue
        author = doc.get('author', {})
        if not isinstance(author,dict):
            continue
        author_url = author.get('url', '')
        author_path = urlsplit(author_url) if isinstance(author_url,str) else urlsplit('')
        host = author_path.hostname or ''
        linkedin_host = host == 'linkedin.com' or host.endswith('.linkedin.com')
        is_person = author.get('@type') == 'Person' and linkedin_host and author_path.scheme == 'https' and author_path.path.startswith('/in/')
        body = doc.get('articleBody', '')
        if not isinstance(body,str) or not body.strip():
            continue
        try:
            stamp = datetime.fromisoformat(doc.get('datePublished','').replace('Z','+00:00'))
            if stamp.tzinfo is None or stamp > now():
                continue
        except (ValueError, TypeError):
            continue
        return {'author': author.get('name') if isinstance(author.get('name'),str) else None,
                'author_url': author_url, 'author_type': 'Person' if is_person else 'OrganizationOrUnverified',
                'post_text': body, 'published': stamp.isoformat(),
                'verification_status': 'verified' if is_person else 'not_person'}
    return None

def public_metadata(url):
    if not canonical(url):
        return None
    response = requests.get(url, allow_redirects=False, timeout=(5,15))
    if response.status_code != 200 or len(response.content) > 2_000_000:
        return None
    return parse_metadata(response.text,url)

def enrich(db, cfg):
    if not cfg.get('require_public_post', False):
        return
    rows = db.execute("SELECT * FROM posts WHERE status NOT IN ('submitted','sending','uncertain') ORDER BY found DESC").fetchall()
    count = 0
    for row in rows:
        if row['published'] and now() - datetime.fromisoformat(row['published']) > timedelta(hours=72):
            continue
        if row['verification_status'] in ('verified','not_person'):
            continue
        if row['verified_at'] and now() - datetime.fromisoformat(row['verified_at']) < timedelta(hours=12):
            continue
        if count >= cfg.get('max_public_fetches',10):
            break
        count += 1
        try:
            evidence = public_metadata(row['url'])
        except requests.RequestException:
            evidence = None
        with db:
            if evidence:
                db.execute('''UPDATE posts SET author=?,author_url=?,author_type=?,post_text=?,published=?,
                    verification_status=?,verified_at=?,date_evidence='public post datePublished' WHERE id=?''',
                    (evidence['author'],evidence['author_url'],evidence['author_type'],evidence['post_text'],
                     evidence['published'],evidence['verification_status'],now().isoformat(),row['id']))
            else:
                db.execute("UPDATE posts SET verification_status='unavailable',verified_at=? WHERE id=?", (now().isoformat(),row['id']))
