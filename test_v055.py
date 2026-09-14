import os
os.environ.setdefault("ABS_URL", "http://abs")
os.environ.setdefault("ABS_API_KEY", "x")
os.environ.setdefault("AI_PROVIDER", "ollama")
import app.main as m
import pytest

@pytest.fixture(autouse=True)
def _temp_db(tmp_path):
    old=m.DB_PATH
    m.DB_PATH=str(tmp_path / "test.db")
    m.init_db()
    try:
        yield
    finally:
        m.DB_PATH=old


def profile_audio():
    return {"requiresNarrator": True, "label": "Audiobook", "kind": "audiobook", "durationSeconds": 0}


def test_author_dash_title_blank_author_is_detected():
    md={"title":"Kassie Keegan - Savage Galaxy Rescue","authorName":"","genres":["Science Fiction"]}
    hint=m.title_author_structure_hint(md)
    assert hint
    assert hint["suggestedAuthor"] == "Kassie Keegan"
    assert hint["suggestedTitle"] == "Savage Galaxy Rescue"
    assert hint["authorAlreadyVerified"] is False
    reasons,new,confidence=m.rule_suggestion(md,{},profile_audio())
    assert new["authorName"] == "Kassie Keegan"
    assert new["title"] == "Savage Galaxy Rescue"
    assert "Title may contain embedded author before separator" in reasons
    assert confidence < .98


def test_existing_author_makes_prefix_removal_high_confidence():
    md={"title":"Kassie Keegan - Savage Galaxy Rescue","authorName":"Kassie Keegan","genres":["Science Fiction"]}
    reasons,new,confidence=m.rule_suggestion(md,{m.normalize_text("Kassie Keegan"):"Kassie Keegan"},profile_audio())
    assert new["title"] == "Savage Galaxy Rescue"
    assert new["authorName"] == "Kassie Keegan"
    assert "Title repeats author before separator" in reasons
    assert confidence >= .99


def test_provider_lookup_uses_split_without_mutating_original():
    md={"title":"Kassie Keegan - Savage Galaxy Rescue","authorName":"","genres":[]}
    lookup=m.metadata_for_provider_lookup(md)
    assert lookup["title"] == "Savage Galaxy Rescue"
    assert lookup["authorName"] == "Kassie Keegan"
    assert md["title"] == "Kassie Keegan - Savage Galaxy Rescue"
    assert md["authorName"] == ""


def test_normal_hyphenated_title_is_not_split():
    md={"title":"A Long-Distance Love Story","authorName":"","genres":[]}
    assert m.title_author_structure_hint(md) is None


def test_different_existing_author_blocks_inference():
    md={"title":"Kassie Keegan - Savage Galaxy Rescue","authorName":"Someone Else","genres":[]}
    assert m.title_author_structure_hint(md) is None


def test_smart_ai_runs_for_structural_suspicion():
    old_mode=m.settings.ai_scan_mode
    old_enabled=m.settings.ai_metadata_corrections
    try:
        m.settings.ai_scan_mode="smart"
        m.settings.ai_metadata_corrections=True
        md={"title":"Kassie Keegan - Savage Galaxy Rescue","authorName":"","genres":["Science Fiction","Romance"],"description":""}
        assert m.needs_ai_assessment(md,md,[],[]) is True
        m.settings.ai_metadata_corrections=False
        assert m.needs_ai_assessment(md,md,[],[]) is False
    finally:
        m.settings.ai_scan_mode=old_mode
        m.settings.ai_metadata_corrections=old_enabled


def test_provider_can_verify_inferred_split():
    hint={"suggestedTitle":"Savage Galaxy Rescue","suggestedAuthor":"Kassie Keegan","authorAlreadyVerified":False}
    evidence=[{"provider":"Test","title":"Savage Galaxy Rescue","authorName":"Kassie Keegan","score":.97}]
    assert m.structure_hint_verified(hint,evidence) is True


class _FakeResponse:
    def raise_for_status(self): pass
    def json(self):
        import json
        result={
            "metadata":{"title":"Savage Galaxy Rescue","authorName":"Kassie Keegan","genres":["Science Fiction"]},
            "applicableGenres":["Science Fiction"],
            "confidence":0.97,
            "notes":["Separated embedded author from title"]
        }
        return {"message":{"content":json.dumps(result)},"prompt_eval_count":150,"eval_count":45}

class _FakeOllama:
    def __init__(self): self.posts=[]
    async def post(self,url,**kwargs):
        self.posts.append((url,kwargs)); return _FakeResponse()


def test_ollama_can_return_title_author_correction():
    import asyncio
    old_http=m.HTTP_CLIENT
    old_provider=m.settings.ai_provider
    old_model=m.settings.ollama_model
    fake=_FakeOllama(); m.HTTP_CLIENT=fake; m.settings.ai_provider="ollama"; m.settings.ollama_model="gemma4:26b"
    try:
        scan=m.create_scan_record("lib","test",True)
        md={"title":"Kassie Keegan - Savage Galaxy Rescue","authorName":"","genres":["Science Fiction"],"description":""}
        profile={"kind":"audiobook","label":"Audiobook","requiresNarrator":True}
        corrected,genres,conf,notes=asyncio.run(m.ai_assess(scan,md,profile,[],force_refresh=True))
        assert corrected["title"]=="Savage Galaxy Rescue"
        assert corrected["authorName"]=="Kassie Keegan"
        assert conf==.97
        payload=fake.posts[0][1]["json"]
        system=payload["messages"][0]["content"]
        assert "Kassie Keegan - Savage Galaxy Rescue" in system
        assert payload["model"]=="gemma4:26b"
    finally:
        m.HTTP_CLIENT=old_http; m.settings.ai_provider=old_provider; m.settings.ollama_model=old_model
