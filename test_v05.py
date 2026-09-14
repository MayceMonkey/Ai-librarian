import asyncio
import json
import tempfile
from pathlib import Path
from fastapi.testclient import TestClient

import app.main as m


def fresh_db():
    td = tempfile.TemporaryDirectory()
    m.DB_PATH = str(Path(td.name) / 'test.db')
    m.init_db()
    return td


class FakeResponse:
    def __init__(self, data): self._data = data
    def raise_for_status(self): return None
    def json(self): return self._data


class FakeHTTP:
    def __init__(self): self.calls = []
    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        parsed = {"metadata": {"genres": ["Fantasy", "Romance"]}, "applicableGenres": ["Fantasy", "Romance"], "confidence": .94, "notes": ["supported"]}
        return FakeResponse({
            "choices": [{"message": {"content": json.dumps(parsed)}}],
            "usage": {
                "prompt_tokens": 1000,
                "completion_tokens": 200,
                "prompt_tokens_details": {"cached_tokens": 400},
                "completion_tokens_details": {"reasoning_tokens": 50},
            }
        })


def test_cost_math_and_usage_fields():
    old = (m.settings.ai_input_price_per_million, m.settings.ai_cached_input_price_per_million, m.settings.ai_output_price_per_million)
    m.settings.ai_input_price_per_million=.25; m.settings.ai_cached_input_price_per_million=.025; m.settings.ai_output_price_per_million=2.0
    try:
        cost=m.estimate_ai_cost({"input":1000,"cached_input":400,"output":200,"reasoning":50})
        assert abs(cost - 0.00056) < 1e-10, cost
    finally:
        m.settings.ai_input_price_per_million, m.settings.ai_cached_input_price_per_million, m.settings.ai_output_price_per_million = old


def test_ai_request_is_bounded_cached_and_metered():
    td=fresh_db(); fake=FakeHTTP(); old_http=m.HTTP_CLIENT
    old=(m.settings.ai_provider,m.settings.ai_api_key,m.settings.ai_model,m.settings.ai_reasoning_effort,m.settings.ai_max_completion_tokens,m.settings.ai_max_scan_cost_usd,m.settings.local_ai_only)
    m.HTTP_CLIENT=fake; m.settings.ai_provider='openai'; m.settings.local_ai_only=False; m.settings.ai_api_key='test'; m.settings.ai_model='gpt-5-mini'; m.settings.ai_reasoning_effort='minimal'; m.settings.ai_max_completion_tokens=400; m.settings.ai_max_scan_cost_usd=1.0
    try:
        scan=m.create_scan_record('lib','test',True)
        md={'title':'Test Book','authorName':'A','description':'x'*10000,'genres':['Fantasy']}
        profile={'kind':'audiobook','label':'Audiobook'}
        evidence=[{'provider':'P','title':'Test Book','description':'y'*10000,'genres':['Fantasy'],'score':.9}]
        asyncio.run(m.ai_assess(scan,md,profile,evidence))
        assert len(fake.calls)==1
        payload=fake.calls[0][1]['json']
        assert payload['reasoning_effort']=='minimal'
        assert payload['max_completion_tokens']==400
        user=json.loads(payload['messages'][1]['content'])
        assert len(user['current_metadata']['description'])==3000
        assert len(user['provider_evidence'][0]['description'])==1200
        with m.db() as con:
            row=con.execute('SELECT * FROM scans WHERE id=?',(scan,)).fetchone()
            assert row['ai_requests']==1 and row['ai_input_tokens']==1000 and row['ai_output_tokens']==200 and row['ai_reasoning_tokens']==50
        asyncio.run(m.ai_assess(scan,md,profile,evidence))
        assert len(fake.calls)==1, 'second identical AI audit should come from cache'
        with m.db() as con:
            row=con.execute('SELECT * FROM scans WHERE id=?',(scan,)).fetchone()
            assert row['ai_requests']==1 and row['ai_cache_hits']==1
    finally:
        m.HTTP_CLIENT=old_http
        (m.settings.ai_provider,m.settings.ai_api_key,m.settings.ai_model,m.settings.ai_reasoning_effort,m.settings.ai_max_completion_tokens,m.settings.ai_max_scan_cost_usd,m.settings.local_ai_only)=old
        td.cleanup()


def test_budget_stops_new_openai_call():
    td=fresh_db(); fake=FakeHTTP(); old_http=m.HTTP_CLIENT
    old=(m.settings.ai_provider,m.settings.ai_api_key,m.settings.ai_max_scan_cost_usd,m.settings.local_ai_only)
    m.HTTP_CLIENT=fake; m.settings.ai_provider='openai'; m.settings.local_ai_only=False; m.settings.ai_api_key='test'; m.settings.ai_max_scan_cost_usd=.01
    try:
        scan=m.create_scan_record('lib','test',True)
        with m.db() as con: con.execute('UPDATE scans SET ai_estimated_cost=.01 WHERE id=?',(scan,))
        try:
            asyncio.run(m.ai_assess(scan,{'title':'Unique Budget Book'},{'kind':'audiobook','label':'Audiobook'},[]))
            raise AssertionError('expected AIBudgetExceeded')
        except m.AIBudgetExceeded:
            pass
        assert not fake.calls
    finally:
        m.HTTP_CLIENT=old_http; m.settings.ai_provider,m.settings.ai_api_key,m.settings.ai_max_scan_cost_usd,m.settings.local_ai_only=old; td.cleanup()


def test_smart_ai_does_not_call_every_well_supported_book():
    td=fresh_db(); old=m.settings.ai_scan_mode; m.settings.ai_scan_mode='smart'
    try:
        md={'title':'B','description':'long '*30,'genres':['Fantasy','Romance']}
        candidate=dict(md)
        evidence=[{'provider':'A','genres':['Fantasy']},{'provider':'B','genres':['Romance']}]
        assert m.needs_ai_assessment(md,candidate,evidence,[]) is False
        assert m.needs_ai_assessment({'title':'B','description':'long '*30,'genres':[]},{'genres':[]},[],[]) is True
    finally:
        m.settings.ai_scan_mode=old; td.cleanup()


def test_cover_apply_moves_to_updated_screen():
    td=fresh_db(); original=m.abs_post
    async def fake_post(path,payload): return {'ok':True}
    m.abs_post=fake_post
    try:
        with m.db() as con:
            con.execute('''INSERT INTO item_health(library_id,item_id,scan_id,title,metadata,media_profile,quality_score,dimensions,issues,cover_candidates,has_cover,updated_at)
                VALUES('lib','item1',1,'Book One',?,?,80,'{}','[]',?,1,1)''',
                (json.dumps({'title':'Book One','authorName':'Author'}),json.dumps({'kind':'audiobook','label':'Audiobook'}),json.dumps([{'url':'https://example.com/c.jpg','provider':'Test','score':.99}])))
        # Calling the handler directly avoids startup replacing the test DB/HTTP fixtures.
        class Req:
            headers={'x-requested-with':'fetch','accept':'application/json'}
        result=asyncio.run(m.apply_cover(Req(),'item1','https://example.com/c.jpg','lib','Test','0.99'))
        assert result['ok'] is True
        with m.db() as con:
            row=con.execute("SELECT * FROM cover_updates WHERE library_id='lib' AND item_id='item1' AND status='updated'").fetchone()
            assert row and row['provider']=='Test'
            pending=con.execute("""SELECT COUNT(*) c FROM item_health h WHERE h.library_id='lib' AND h.cover_candidates<>'[]'
                AND NOT EXISTS (SELECT 1 FROM cover_updates u WHERE u.library_id=h.library_id AND u.item_id=h.item_id AND u.status='updated')""").fetchone()['c']
            assert pending==0
    finally:
        m.abs_post=original; td.cleanup()


def test_v05_templates_parse():
    from jinja2 import Environment, FileSystemLoader
    env=Environment(loader=FileSystemLoader('app/templates'))
    for name in ['index.html','covers.html','covers_updated.html','base.html']:
        env.get_template(name)


if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests:
        fn(); print('PASS',fn.__name__)
    print(f'PASS {len(tests)} v0.5 tests total')
