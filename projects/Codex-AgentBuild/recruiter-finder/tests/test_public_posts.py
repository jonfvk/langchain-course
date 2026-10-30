import json
from datetime import timedelta
from unittest.mock import Mock
import pytest
from recruiter import core, services, public_posts

@pytest.fixture
def cfg():
    value=json.loads((core.ROOT/'config.json').read_text())
    value['notifications_enabled']=True
    return value

@pytest.fixture
def db(tmp_path):
    db=core.connect(tmp_path)
    yield db
    db.close()

def post():
    key=str(int((core.now()-timedelta(hours=1)).timestamp()*1000)<<22)
    return 'https://www.linkedin.com/posts/example_hiring-activity-'+key+'-abc'

def metadata(url,author_type='Person',body=None):
    return {'@type':'SocialMediaPosting','@id':url,
       'datePublished':(core.now()-timedelta(hours=1)).isoformat(),
       'author':{'@type':author_type,'name':'Test Poster','url':'https://www.linkedin.com/in/test-poster' if author_type=='Person' else 'https://www.linkedin.com/company/acme'},
       'articleBody':body or "We're hiring a Senior DevOps Engineer. Location: Remote US. AWS Python Kubernetes."}

def page(doc):
    return '<script type="application/ld+json">'+json.dumps(doc)+'</script>'

def test_public_metadata_person():
    url=post();d=public_posts.parse_metadata(page(metadata(url)),url)
    assert d['author_type']=='Person'
    assert d['verification_status']=='verified'
    assert 'Remote US' in d['post_text']

def test_company_not_person():
    url=post();d=public_posts.parse_metadata(page(metadata(url,'Organization')),url)
    assert d['verification_status']=='not_person'

def test_ignore_comments_and_mismatched_posts():
    url=post();doc=metadata(url)
    doc['comment']=[metadata(url)]
    doc['@id']='https://www.linkedin.com/posts/wrong'
    assert public_posts.parse_metadata(page(doc),url) is None

def test_naive_or_missing_date():
    url=post();doc=metadata(url);doc['datePublished']='2026-10-21T01:00:00'
    assert public_posts.parse_metadata(page(doc),url) is None

def test_redirect_not_followed(monkeypatch):
    response=Mock(status_code=302)
    get=Mock(return_value=response)
    monkeypatch.setattr(public_posts.requests,'get',get)
    assert public_posts.public_metadata(post()) is None
    assert get.call_args.kwargs['allow_redirects'] is False

def test_full_text_drives_alert(db,cfg,monkeypatch):
    url=post()
    core.ingest(db,{'link':url,'title':'Hiring DevOps Engineer','snippet':'Short excerpt without location'},cfg)
    assert db.execute('SELECT status FROM posts').fetchone()[0]!='pending'
    evidence=public_posts.parse_metadata(page(metadata(url)),url)
    monkeypatch.setattr(public_posts,'public_metadata',lambda u:evidence)
    public_posts.enrich(db,cfg)
    sender=Mock(return_value=('test-submitted','submitted-unverified'))
    monkeypatch.setattr(services,'send',sender)
    services.notify(db,cfg)
    assert sender.call_count==1
    assert 'Poster: Test Poster' in sender.call_args.args[0]
    assert 'public post date' in sender.call_args.args[0]
    assert db.execute('SELECT status FROM posts').fetchone()[0]=='submitted'

def test_actual_person_onsite_elsewhere_not_alerted(db,cfg,monkeypatch):
    url=post();core.ingest(db,{'link':url,'title':'Senior DevOps hiring','snippet':'I am hiring remote'},cfg)
    evidence=public_posts.parse_metadata(page(metadata(url,body="My team is hiring Senior DevOps AWS Python Engineers on-site in Seattle.")),url)
    monkeypatch.setattr(public_posts,'public_metadata',lambda u:evidence)
    public_posts.enrich(db,cfg)
    sender=Mock();monkeypatch.setattr(services,'send',sender)
    services.notify(db,cfg)
    sender.assert_not_called()
    assert db.execute('SELECT status FROM posts').fetchone()[0]=='excluded_location'

def test_company_excluded_even_with_personal_language(db,cfg,monkeypatch):
    url=post();core.ingest(db,{'link':url,'title':'Senior DevOps hiring','snippet':'My team hiring remote AWS Python'},cfg)
    evidence=public_posts.parse_metadata(page(metadata(url,'Organization')),url)
    monkeypatch.setattr(public_posts,'public_metadata',lambda u:evidence)
    public_posts.enrich(db,cfg)
    sender=Mock();monkeypatch.setattr(services,'send',sender)
    services.notify(db,cfg)
    sender.assert_not_called()
    assert db.execute('SELECT status FROM posts').fetchone()[0]=='excluded_not_person'

def test_unavailable_public_metadata_does_not_alert(db,cfg,monkeypatch):
    url=post();core.ingest(db,{'link':url,'title':'Senior DevOps hiring','snippet':'My team hiring remote AWS Python'},cfg)
    monkeypatch.setattr(public_posts,'public_metadata',lambda u:None)
    public_posts.enrich(db,cfg)
    sender=Mock();monkeypatch.setattr(services,'send',sender)
    services.notify(db,cfg)
    sender.assert_not_called()
    assert db.execute('SELECT verification_status FROM posts').fetchone()[0]=='unavailable'

@pytest.mark.parametrize('host',['in.linkedin.com','ca.linkedin.com','www.linkedin.com'])
def test_regional_domains(host):
    assert core.canonical(post().replace('www.linkedin.com',host))[1].startswith('https://www.linkedin.com/posts/')

def test_search_empty_is_not_a_provider_failure(db,cfg,monkeypatch):
    cfg['require_public_post']=False
    monkeypatch.setenv('SERPAPI_KEY','mock-token')
    response=Mock()
    response.json.return_value={'error':"DuckDuckGo hasn't returned any results for this query."}
    monkeypatch.setattr(services.requests,'get',Mock(return_value=response))
    assert services.search(db,cfg)==0
    assert db.execute('SELECT count(*) FROM posts').fetchone()[0]==0

def test_remote_access_skill_not_remote_job(cfg):
    assert core.location_status('My team is hiring Senior Security Engineers with remote access VPN experience.',cfg)=='review_location'

def test_real_error_stays_failure(db,cfg,monkeypatch):
    cfg['require_public_post']=False
    monkeypatch.setenv('SERPAPI_KEY','mock-token')
    response=Mock();response.json.return_value={'error':'Account limit reached'}
    monkeypatch.setattr(services.requests,'get',Mock(return_value=response))
    assert services.search(db,cfg)==3
