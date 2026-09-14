import os
import tempfile
from pathlib import Path

os.environ.setdefault("ABS_API_KEY", "test")
os.environ.setdefault("AI_PROVIDER", "none")

from app import main as m


def item(i, kind, title="Book", author="Author", rel="Author/Book", is_file=False, ebook_name="Book.epub"):
    media = {"metadata": {"title": title, "authorName": author}, "audioFiles": [], "ebookFile": None}
    library = []
    if kind == "audio":
        media["audioFiles"] = [{"metadata": {"filename": "Book.m4b", "relPath": "Book.m4b", "ext": ".m4b"}}]
    else:
        media["ebookFile"] = {
            "metadata": {"filename": ebook_name, "relPath": ebook_name, "ext": ".epub", "size": 10},
            "ebookFormat": "epub",
        }
        library = [{"metadata": {"filename": ebook_name, "relPath": ebook_name, "ext": ".epub", "size": 10}, "fileType": "ebook"}]
    return {"id": i, "relPath": rel, "path": "/books/" + rel, "isFile": is_file, "media": media, "libraryFiles": library}


def test_pair_score_exact():
    a = item("a", "audio")
    e = item("e", "ebook", rel="ebooks/Book")
    score, reason = m.media_pair_score(a, e)
    assert score >= .96 and "title" in reason and "author" in reason


def test_pair_candidate_cross_format():
    a = item("a", "audio")
    e = item("e", "ebook", rel="ebooks/Book")
    pairs = m.build_media_pair_candidates([a, e])
    assert len(pairs) == 1 and pairs[0]["audio_item_id"] == "a" and pairs[0]["ebook_item_id"] == "e"


def test_author_mismatch_rejected():
    a = item("a", "audio", author="Alpha Writer")
    e = item("e", "ebook", author="Totally Different")
    score, _ = m.media_pair_score(a, e)
    assert score == 0


def test_safe_pair_file_move_path_guard():
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        (root / "Author/Book").mkdir(parents=True)
        (root / "Author/Book/Book.m4b").write_bytes(b"audio")
        old = m.settings.media_library_root
        m.settings.media_library_root = td
        try:
            assert m._safe_media_path("Author/Book/Book.m4b").is_file()
            try:
                m._safe_media_path("../escape")
            except Exception:
                pass
            else:
                raise AssertionError("path traversal accepted")
        finally:
            m.settings.media_library_root = old


def test_single_file_audio_is_identified():
    a = item("a", "audio", rel="Book.m4b", is_file=True)
    e = item("e", "ebook", rel="ebooks/Book")
    pairs = m.build_media_pair_candidates([a, e])
    assert pairs and pairs[0]["details"]["audio"]["storage"]["isFile"] is True


def test_pairing_template_parses():
    m.templates.get_template("pairings.html")


if __name__ == "__main__":
    tests = [(name, obj) for name, obj in sorted(globals().items()) if name.startswith("test_") and callable(obj)]
    for name, fn in tests:
        fn()
        print("PASS", name)
    print(f"PASS {len(tests)} v0.5.2 tests total")

async def _merge_integration_case():
    import asyncio
    import json
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as td:
        media = Path(td) / "media"
        (media / "Author/Book").mkdir(parents=True)
        (media / "ebooks/Book").mkdir(parents=True)
        (media / "Author/Book/Book.m4b").write_bytes(b"audio")
        (media / "ebooks/Book/Book.epub").write_bytes(b"ebook")
        dbfile = Path(td) / "test.db"
        old_db, old_root = m.DB_PATH, m.settings.media_library_root
        old_post = m.abs_post
        m.DB_PATH = str(dbfile)
        m.settings.media_library_root = str(media)
        async def fake_post(path, payload=None, params=None):
            return {"result":"UPDATED"}
        m.abs_post = fake_post
        try:
            m.init_db()
            a = item("a", "audio", rel="Author/Book")
            e = item("e", "ebook", rel="ebooks/Book")
            pair = m.build_media_pair_candidates([a,e])[0]
            with m.db() as con:
                con.execute("""INSERT INTO media_pairings(pair_key,library_id,scan_id,audio_item_id,ebook_item_id,score,reason,details,status,updated_at)
                               VALUES(?,?,?,?,?,?,?,?,?,?)""",
                            (pair["pair_key"],"lib",1,"a","e",pair["score"],pair["reason"],json.dumps(pair["details"]),"pending",1))
            out = await m.merge_media_pair(pair["pair_key"])
            assert out["status"] == "paired"
            assert (media / "Author/Book/Book.epub").is_file()
            assert not (media / "ebooks/Book/Book.epub").exists()
            with m.db() as con:
                row=con.execute("SELECT status FROM media_pairings WHERE pair_key=?",(pair["pair_key"],)).fetchone()
                assert row["status"] == "paired"
        finally:
            m.abs_post = old_post
            m.DB_PATH = old_db
            m.settings.media_library_root = old_root


def test_merge_integration():
    import asyncio
    asyncio.run(_merge_integration_case())
