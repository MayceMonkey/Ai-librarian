import asyncio, json, tempfile, os
from pathlib import Path
import app.main as m

class FakeResponse:
    def __init__(self,data): self._data=data
    def raise_for_status(self): pass
    def json(self): return self._data

class FakeOllama:
    def __init__(self): self.posts=[]; self.gets=[]
    async def get(self,url,**kwargs):
        self.gets.append((url,kwargs))
        return FakeResponse({'models':[{'name':'qwen3:4b-instruct'}]})
    async def post(self,url,**kwargs):
        self.posts.append((url,kwargs))
        result={'metadata': {'genres':['Fantasy','Romance']}, 'applicableGenres':['Fantasy','Romance'], 'confidence':0.96, 'notes':['supported']}
        return FakeResponse({'message':{'content':json.dumps(result)},'prompt_eval_count':321,'eval_count':77})

def fresh_db():
    td=tempfile.TemporaryDirectory(); m.DB_PATH=str(Path(td.name)/'t.db'); m.init_db(); return td

def test_ollama_defaults():
    # Isolate class defaults from environment variables set by other regression modules.
    keys=['AI_PROVIDER','LOCAL_AI_ONLY','OLLAMA_MODEL','OLLAMA_CONCURRENCY']
    saved={k:os.environ.pop(k,None) for k in keys}
    try:
        cfg=m.Settings(_env_file=None)
        assert cfg.ai_provider == 'ollama'
        assert cfg.local_ai_only is True
        assert cfg.ollama_model == 'qwen3:4b-instruct'
        assert cfg.ollama_concurrency == 1
    finally:
        for k,v in saved.items():
            if v is not None: os.environ[k]=v

def test_ollama_status_and_model_detection():
    fake=FakeOllama(); old=m.HTTP_CLIENT; m.HTTP_CLIENT=fake
    oldp,oldm=m.settings.ai_provider,m.settings.ollama_model
    m.settings.ai_provider='ollama'; m.settings.ollama_model='qwen3:4b-instruct'
    try:
        st=asyncio.run(m.ollama_status())
        assert st['connected'] and st['model_installed']
        assert '/api/tags' in fake.gets[0][0]
    finally:
        m.HTTP_CLIENT=old; m.settings.ai_provider, m.settings.ollama_model=oldp,oldm

def test_ollama_request_is_local_bounded_and_non_thinking():
    td=fresh_db(); fake=FakeOllama(); old=m.HTTP_CLIENT; m.HTTP_CLIENT=fake
    vals=(m.settings.ai_provider,m.settings.ollama_model,m.settings.ollama_think,m.settings.ai_max_completion_tokens,m.settings.ollama_num_ctx,m.settings.ollama_keep_alive)
    m.settings.ai_provider='ollama'; m.settings.ollama_model='qwen3:4b-instruct'; m.settings.ollama_think=False; m.settings.ai_max_completion_tokens=400; m.settings.ollama_num_ctx=8192; m.settings.ollama_keep_alive='15m'
    try:
        scan=m.create_scan_record('lib','test',True)
        md={'title':'Book','authorName':'Author','description':'A fantasy romance story. '*20,'genres':['Fantasy']}
        profile={'kind':'audiobook','label':'Audiobook'}
        result=asyncio.run(m.ai_assess(scan,md,profile,[]))
        assert result[2] == .96
        assert len(fake.posts)==1
        url,kw=fake.posts[0]; payload=kw['json']
        assert url.endswith('/api/chat')
        assert payload['think'] is False
        assert payload['keep_alive']=='15m'
        assert payload['options']['num_predict']==400
        assert payload['options']['num_ctx']==8192
        assert isinstance(payload['format'],dict) and payload['format']['type']=='object'
        with m.db() as con:
            row=con.execute('select * from scans where id=?',(scan,)).fetchone()
            assert row['ai_input_tokens']==321 and row['ai_output_tokens']==77
            assert float(row['ai_estimated_cost'] or 0)==0
    finally:
        m.HTTP_CLIENT=old
        (m.settings.ai_provider,m.settings.ollama_model,m.settings.ollama_think,m.settings.ai_max_completion_tokens,m.settings.ollama_num_ctx,m.settings.ollama_keep_alive)=vals
        td.cleanup()

def test_cloud_path_blocked_in_local_only_mode():
    td=fresh_db(); old=(m.settings.ai_provider,m.settings.local_ai_only,m.settings.ai_api_key)
    m.settings.ai_provider='openai'; m.settings.local_ai_only=True; m.settings.ai_api_key='should-not-be-used'
    try:
        scan=m.create_scan_record('lib','test',True)
        try:
            asyncio.run(m.ai_assess(scan,{'title':'Book'},{'kind':'audiobook','label':'Audiobook'},[],force_refresh=True))
            raise AssertionError('cloud path should be blocked')
        except RuntimeError as e:
            assert 'Cloud AI is disabled' in str(e)
    finally:
        m.settings.ai_provider,m.settings.local_ai_only,m.settings.ai_api_key=old; td.cleanup()

if __name__=='__main__':
    tests=[v for k,v in sorted(globals().items()) if k.startswith('test_') and callable(v)]
    for fn in tests: fn(); print('PASS',fn.__name__)
    print('PASS',len(tests),'v0.5.3 tests total')
