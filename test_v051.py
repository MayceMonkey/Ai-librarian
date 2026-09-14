import asyncio
import json
import tempfile
from pathlib import Path

import app.main as m


def fresh_db():
    td = tempfile.TemporaryDirectory()
    m.DB_PATH = str(Path(td.name) / 'test.db')
    m.init_db()
    return td


def add_health(lib, item, title, candidates):
    with m.db() as con:
        con.execute('''INSERT INTO item_health(library_id,item_id,scan_id,title,metadata,media_profile,quality_score,dimensions,issues,cover_candidates,has_cover,updated_at)
            VALUES(?,?,?,?,?,?,80,'{}','[]',?,1,1)''',
            (lib,item,1,title,json.dumps({'title':title}),json.dumps({'kind':'audiobook','label':'Audiobook'}),json.dumps(candidates)))


def test_auto_targets_use_best_candidate_and_96_threshold():
    td=fresh_db()
    try:
        add_health('lib','a','A',[{'url':'https://x/96.jpg','provider':'P','score':.96},{'url':'https://x/99.jpg','provider':'Q','score':.99}])
        add_health('lib','b','B',[{'url':'https://x/959.jpg','provider':'P','score':.959}])
        add_health('lib','c','C',[{'url':'https://x/960.jpg','provider':'P','score':.9600}])
        targets=m.pending_cover_auto_targets('lib',.96)
        assert [x['item_id'] for x in targets] == ['a','c']
        assert targets[0]['cover_url']=='https://x/99.jpg'
        assert targets[0]['match_score']==.99
    finally:
        td.cleanup()


def test_auto_cover_job_applies_and_archives_only_eligible():
    td=fresh_db(); original=m.abs_post; calls=[]
    async def fake_post(path,payload):
        calls.append((path,payload)); return {'ok':True}
    m.abs_post=fake_post
    try:
        add_health('lib','a','A',[{'url':'https://x/a.jpg','provider':'AudioSilo','score':.98}])
        add_health('lib','b','B',[{'url':'https://x/b.jpg','provider':'Open Library','score':.95}])
        add_health('lib','c','C',[{'url':'https://x/c.jpg','provider':'AudioSilo','score':.96}])
        async def run():
            job_id,count=m.start_cover_auto_job('lib',.96)
            assert count==2
            await m.ACTION_TASKS[job_id]
            return m.ACTION_JOBS[job_id]
        job=asyncio.run(run())
        assert job['status']=='completed'
        assert job['applied']==2 and job['failed']==0 and job['completed']==2
        assert {p for p,_ in calls}=={'/api/items/a/cover','/api/items/c/cover'}
        with m.db() as con:
            updated=con.execute("SELECT item_id,match_score FROM cover_updates WHERE library_id='lib' AND status='updated' ORDER BY item_id").fetchall()
            assert [(r['item_id'],round(r['match_score'],2)) for r in updated]==[('a',.98),('c',.96)]
            pending=m.pending_cover_auto_targets('lib',.96)
            assert pending==[]
            still_pending=con.execute("SELECT item_id FROM item_health WHERE item_id='b'").fetchone()
            assert still_pending is not None
    finally:
        m.abs_post=original; td.cleanup()


def test_updated_cover_is_never_auto_selected_again():
    td=fresh_db()
    try:
        add_health('lib','a','A',[{'url':'https://x/a.jpg','provider':'P','score':1.0}])
        with m.db() as con:
            con.execute("INSERT INTO cover_updates(library_id,item_id,title,cover_url,provider,match_score,applied_at,status) VALUES('lib','a','A','https://x/a.jpg','P',1.0,1,'updated')")
        assert m.pending_cover_auto_targets('lib',.96)==[]
    finally:
        td.cleanup()


def test_cover_template_has_auto_button():
    text=Path('app/templates/covers.html').read_text()
    assert '/covers/auto-apply' in text
    assert 'Auto-update ≥' in text
    assert 'auto_count' in text


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print('PASS',fn.__name__)
    print(f'PASS {len(tests)} v0.5.1 tests total')
