import os, json
os.environ.setdefault('ABS_URL','http://abs')
os.environ.setdefault('ABS_API_KEY','x')
os.environ.setdefault('AI_PROVIDER','ollama')
import pytest
from fastapi.testclient import TestClient
import app.main as m

@pytest.fixture(autouse=True)
def _db(tmp_path):
    old=m.DB_PATH
    m.DB_PATH=str(tmp_path/'test.db')
    m.init_db()
    try: yield
    finally: m.DB_PATH=old


def seed_suggestion(item_id='b1', status='pending'):
    with m.db() as con:
        cur=con.execute('''INSERT INTO suggestions(scan_id,item_id,library_id,title,reasons,old_metadata,new_metadata,confidence,source,status,created_at,evidence,media_profile,selected_fields)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                        (1,item_id,'lib','Book One',json.dumps(['fix title']),json.dumps({'title':'Old'}),json.dumps({'title':'New'}),.99,'rules',status,1,json.dumps([]),json.dumps({'kind':'audiobook','label':'Audiobook'}),json.dumps(['title'])))
        return cur.lastrowid


@pytest.fixture
def no_abs(monkeypatch):
    async def fake_patch(path, payload):
        return {'ok': True, 'path': path, 'payload': payload}
    monkeypatch.setattr(m, 'abs_patch', fake_patch)


def test_apply_plus_metadata_lock(no_abs):
    sid=seed_suggestion()
    c=TestClient(m.app)
    r=c.post(f'/suggestions/{sid}/apply-metadata-lock', headers={'x-requested-with':'fetch','accept':'application/json'})
    assert r.status_code==200, r.text
    with m.db() as con:
        assert con.execute('SELECT status FROM suggestions WHERE id=?',(sid,)).fetchone()['status']=='applied'
    control=m.get_book_control('lib','b1')
    assert control['metadata_lock'] is True
    assert control['complete'] is False


def test_apply_plus_complete(no_abs):
    sid=seed_suggestion()
    c=TestClient(m.app)
    r=c.post(f'/suggestions/{sid}/apply-complete', headers={'x-requested-with':'fetch','accept':'application/json'})
    assert r.status_code==200, r.text
    control=m.get_book_control('lib','b1')
    assert control['complete'] and control['full_lock'] and control['metadata_lock'] and control['cover_lock'] and control['scan_exempt']


def test_revert_is_backend_blocked_when_metadata_locked(no_abs):
    sid=seed_suggestion(status='applied')
    m.set_book_control('lib','b1','Book One','metadata-lock')
    c=TestClient(m.app)
    r=c.post(f'/suggestions/{sid}/revert', headers={'x-requested-with':'fetch','accept':'application/json'})
    assert r.status_code==409
    assert 'locked' in r.text.lower()


def test_applied_locked_card_is_colored_and_revert_disabled():
    sid=seed_suggestion(status='applied')
    m.set_book_control('lib','b1','Book One','metadata-lock')
    c=TestClient(m.app)
    html=c.get('/suggestions?status=applied').text
    assert 'suggestion-metadata-locked' in html
    assert 'Metadata locked' in html
    assert 'revert-disabled' in html
    assert 'disabled' in html


def test_pending_review_has_both_combined_actions():
    seed_suggestion()
    c=TestClient(m.app)
    html=c.get('/suggestions').text
    assert 'Apply + Metadata Lock' in html
    assert 'Apply + Complete' in html
