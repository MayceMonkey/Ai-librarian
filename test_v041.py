import asyncio
import json
import tempfile
from pathlib import Path
import time
from fastapi.testclient import TestClient

import app.main as m


def fresh_db():
    td = tempfile.TemporaryDirectory()
    m.DB_PATH = str(Path(td.name) / 'test.db')
    m.init_db()
    return td


def insert_suggestion(title, old, new, selected, confidence=0.99, status='pending'):
    with m.db() as con:
        cur = con.execute('''INSERT INTO suggestions(scan_id,item_id,library_id,title,reasons,old_metadata,new_metadata,
            confidence,source,status,created_at,evidence,media_profile,selected_fields)
            VALUES(1,?,?,?,?,?,?,?,?,?,?,?,?,?)''', (
            f'item-{title}', 'lib', title, json.dumps(['test']), json.dumps(old), json.dumps(new), confidence,
            'rules', status, 1, '[]', json.dumps({'kind':'audiobook','label':'Audiobook'}), json.dumps(selected)
        ))
        return cur.lastrowid


def test_apply_many_partial_success():
    td = fresh_db()
    calls = []
    original = m.abs_patch
    async def fake_patch(path, payload):
        calls.append((path, payload)); return {'ok': True}
    m.abs_patch = fake_patch
    try:
        s1 = insert_suggestion('one', {'title':'OLD'}, {'title':'New'}, ['title'])
        s2 = insert_suggestion('two', {'title':'Same'}, {'title':'Same'}, [])
        s3 = insert_suggestion('three', {'title':'A'}, {'title':'B'}, ['title'], status='ignored')
        result = asyncio.run(m.apply_many_suggestions([s1,s2,s3], concurrency=3))
        assert result['applied'] == 1, result
        assert result['skipped'] == 2, result
        assert result['failed'] == 0, result
        assert len(calls) == 1
        with m.db() as con:
            assert con.execute('SELECT status FROM suggestions WHERE id=?',(s1,)).fetchone()['status'] == 'applied'
    finally:
        m.abs_patch = original
        td.cleanup()


def test_strong_exact_skips_redundant_fuzzy():
    original_exact = m.audiosilo_exact_lookup
    original_fuzzy = m.custom_abs_provider_lookup
    old_audio, old_ol, old_hc = m.settings.audiosilo_enabled, m.settings.openlibrary_enabled, m.settings.hardcover_enabled
    async def exact(md, profile):
        return [{'provider':'AudioSilo','score':0.999,'identifierExact':True,'genres':['Fantasy'],'title':'Book'}]
    async def fuzzy(*args, **kwargs):
        raise AssertionError('fuzzy lookup should not run after a strong exact recording match')
    m.audiosilo_exact_lookup = exact
    m.custom_abs_provider_lookup = fuzzy
    m.settings.audiosilo_enabled = True
    m.settings.openlibrary_enabled = False
    m.settings.hardcover_enabled = False
    try:
        rows = asyncio.run(m.gather_provider_evidence({'title':'Book','asin':'ABC'}, {'hasAudio':True,'kind':'audiobook'}))
        assert len(rows) == 1 and rows[0]['identifierExact'] is True
    finally:
        m.audiosilo_exact_lookup = original_exact
        m.custom_abs_provider_lookup = original_fuzzy
        m.settings.audiosilo_enabled, m.settings.openlibrary_enabled, m.settings.hardcover_enabled = old_audio, old_ol, old_hc


def test_review_pagination_query_shape():
    td = fresh_db()
    try:
        for i in range(123):
            insert_suggestion(f'b{i:03d}', {'title':f'B{i}'}, {'title':f'Book {i}'}, ['title'])
        # Verify the database math used by the route without depending on an ABS connection.
        per_page = max(20, min(m.settings.review_page_size, 100))
        with m.db() as con:
            total = con.execute("SELECT COUNT(*) c FROM suggestions WHERE status='pending'").fetchone()['c']
            rows = con.execute("SELECT id FROM suggestions WHERE status='pending' ORDER BY confidence DESC,id DESC LIMIT ? OFFSET ?", (per_page, per_page)).fetchall()
        assert total == 123
        assert len(rows) == min(per_page, 73)
    finally:
        td.cleanup()


def test_cleanup_regression():
    assert m.cleanup_release_title('Wreck Me Forever (Unabridged)') == 'Wreck Me Forever'
    assert m.cleanup_release_title('Book [] -   ') == 'Book'
    assert m.cleanup_release_title('It (Novel)') == 'It (Novel)'



def test_batch_endpoint_background_job():
    td = fresh_db()
    original_get, original_patch = m.abs_get, m.abs_patch
    async def fake_get(path, params=None): return {'libraries': []}
    async def fake_patch(path, payload): return {'ok': True}
    m.abs_get, m.abs_patch = fake_get, fake_patch
    try:
        ids = [
            insert_suggestion('batch1', {'title':'A'}, {'title':'AA'}, ['title']),
            insert_suggestion('batch2', {'title':'B'}, {'title':'BB'}, ['title']),
            insert_suggestion('batch3', {'title':'C'}, {'title':'CC'}, ['title']),
        ]
        with TestClient(m.app) as client:
            r = client.post('/batch', data={'action':'apply','ids':[str(x) for x in ids]},
                            headers={'X-Requested-With':'fetch','Accept':'application/json'})
            assert r.status_code == 200, r.text
            job_id = r.json()['job_id']
            job = None
            for _ in range(100):
                job = client.get('/api/actions/' + job_id).json()['job']
                if job['status'] in {'completed','failed'}: break
                time.sleep(.01)
            assert job['status'] == 'completed', job
            assert job['applied'] == 3 and job['failed'] == 0, job
    finally:
        m.abs_get, m.abs_patch = original_get, original_patch
        td.cleanup()


def test_auto_apply_endpoint_uses_threshold():
    td = fresh_db()
    original_get, original_patch = m.abs_get, m.abs_patch
    old_allow, old_threshold = m.settings.allow_auto_apply, m.settings.auto_apply_threshold
    async def fake_get(path, params=None): return {'libraries': []}
    async def fake_patch(path, payload): return {'ok': True}
    m.abs_get, m.abs_patch = fake_get, fake_patch
    m.settings.allow_auto_apply = True
    m.settings.auto_apply_threshold = 0.98
    try:
        high = insert_suggestion('auto-high', {'title':'A'}, {'title':'AA'}, ['title'], confidence=.99)
        low = insert_suggestion('auto-low', {'title':'B'}, {'title':'BB'}, ['title'], confidence=.95)
        with TestClient(m.app) as client:
            r = client.post('/auto-apply', headers={'X-Requested-With':'fetch','Accept':'application/json'})
            assert r.status_code == 200, r.text
            job_id = r.json()['job_id']
            job = None
            for _ in range(100):
                job = client.get('/api/actions/' + job_id).json()['job']
                if job['status'] in {'completed','failed'}: break
                time.sleep(.01)
            assert job['status'] == 'completed' and job['applied'] == 1, job
        with m.db() as con:
            assert con.execute('SELECT status FROM suggestions WHERE id=?',(high,)).fetchone()['status'] == 'applied'
            assert con.execute('SELECT status FROM suggestions WHERE id=?',(low,)).fetchone()['status'] == 'pending'
    finally:
        m.abs_get, m.abs_patch = original_get, original_patch
        m.settings.allow_auto_apply, m.settings.auto_apply_threshold = old_allow, old_threshold
        td.cleanup()

def test_no_per_request_http_clients_left():
    source = Path(m.__file__).read_text()
    # v0.4.1 should only construct AsyncClient in startup, not in request/provider functions.
    assert source.count('httpx.AsyncClient(') == 1


if __name__ == '__main__':
    tests = [v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print('PASS', fn.__name__)
    print(f'PASS {len(tests)} v0.4.1 tests total')
