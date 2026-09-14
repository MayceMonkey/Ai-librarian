import os, tempfile, importlib
os.environ.setdefault("ABS_URL", "http://abs")
os.environ.setdefault("ABS_API_KEY", "x")
os.environ.setdefault("AI_PROVIDER", "ollama")
import app.main as m

def test_runtime_settings_roundtrip():
    old=m.DB_PATH
    try:
        with tempfile.TemporaryDirectory() as d:
            m.DB_PATH=d+"/test.db"; m.init_db()
            vals=m.settings.model_dump(); vals["scan_concurrency"]=7; vals["ollama_url"]="http://host.docker.internal:11434"; vals["allow_auto_apply"]=True
            m.persist_runtime_values(vals)
            m.settings.scan_concurrency=1; m.settings.allow_auto_apply=False
            m.load_runtime_settings()
            assert m.settings.scan_concurrency==7
            assert m.settings.allow_auto_apply is True
            assert m.settings.ollama_url.endswith(":11434")
    finally: m.DB_PATH=old

def test_local_only_normalizes_openai():
    vals=m.settings.model_dump(); vals["local_ai_only"]=True; vals["ai_provider"]="openai"
    out=m._normalize_runtime_values(vals)
    assert out["ai_provider"]=="ollama"

def test_runtime_bounds():
    vals=m.settings.model_dump(); vals.update(scan_concurrency=999, review_page_size=1, provider_min_score=2)
    out=m._normalize_runtime_values(vals)
    assert out["scan_concurrency"]==32
    assert out["review_page_size"]==10
    assert out["provider_min_score"]==1.0
