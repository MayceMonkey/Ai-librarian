from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePath
from typing import Any

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    abs_url: str = "http://host.docker.internal:13378"
    abs_api_key: str = ""
    library_id: str = ""

    # AI: Ollama-first. Cloud AI is blocked by default.
    ai_provider: str = "ollama"
    local_ai_only: bool = True
    ai_base_url: str = "https://api.openai.com/v1"
    ai_api_key: str = ""
    ai_model: str = "gpt-5-mini"
    # v0.5 AI cost controls. Smart mode calls AI only when local/provider evidence is insufficient.
    ai_scan_mode: str = "smart"  # smart | deep
    ai_metadata_corrections: bool = True
    ai_reasoning_effort: str = "minimal"
    ai_max_completion_tokens: int = 400
    ai_max_scan_cost_usd: float = 0.10
    # Pricing defaults for gpt-5-mini. Override these if AI_MODEL uses different pricing.
    ai_input_price_per_million: float = 0.25
    ai_cached_input_price_per_million: float = 0.025
    ai_output_price_per_million: float = 2.00
    ai_cache_days: int = 90
    ollama_url: str = "http://host.docker.internal:11434"
    ollama_model: str = "qwen3:4b-instruct"
    ollama_think: bool = False
    ollama_keep_alive: str = "15m"
    ollama_num_ctx: int = 8192
    ollama_temperature: float = 0.1
    ollama_concurrency: int = 1

    # Metadata providers
    audiosilo_enabled: bool = True
    audiosilo_url: str = "https://meta.audiosilo.app/abs"
    openlibrary_enabled: bool = True
    openlibrary_contact: str = ""
    hardcover_enabled: bool = False
    hardcover_url: str = "https://provider.vito0912.de/hardcover"
    provider_min_score: float = 0.86
    provider_cache_days: int = 30

    # Scanner
    scheduled_scans_enabled: bool = False
    scheduled_scan_hours: int = 24
    scheduled_use_ai: bool = False
    scan_concurrency: int = 4
    apply_concurrency: int = 4
    review_page_size: int = 50
    cover_page_size: int = 16

    # Optional direct media storage access for pairing separate audiobook/e-book items.
    # Mount the SAME ABS library root into this container and point this at the in-container mount.
    media_library_root: str = ""
    # Optional JSON mapping for libraries with multiple ABS folder roots, e.g.
    # {"/audiobooks":"/abs-audiobooks","/ebooks":"/abs-ebooks"}
    media_path_map: str = ""
    pair_min_score: float = 0.88

    # Safety
    auto_apply_threshold: float = 0.98
    allow_auto_apply: bool = False


settings = Settings()
ENV_SETTINGS = settings.model_dump()
# Upgrade-safe: an old AI_PROVIDER=openai is redirected to local Ollama while local-only mode is enabled.
if settings.local_ai_only and settings.ai_provider.casefold() == "openai":
    settings.ai_provider = "ollama"
app = FastAPI(title="AI Librarian", version="0.5.6.1")
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
DB_PATH = "/config/librarian.db"
SCAN_TASKS: dict[int, asyncio.Task] = {}
ACTION_TASKS: dict[str, asyncio.Task] = {}
ACTION_JOBS: dict[str, dict[str, Any]] = {}
SCHEDULED_SCAN_TASK: asyncio.Task | None = None
HTTP_CLIENT: httpx.AsyncClient | None = None
OLLAMA_SEMAPHORE = asyncio.Semaphore(max(1, settings.ollama_concurrency))

AUDIO_EXTENSIONS = {"mp3", "m4b", "m4a", "aac", "flac", "ogg", "opus", "wav", "wma"}
EBOOK_EXTENSIONS = {"epub", "pdf", "cbr", "cbz", "azw3", "mobi"}

DEFAULT_GENRES = [
    "Fantasy", "Epic Fantasy", "Urban Fantasy", "Dark Fantasy", "LitRPG",
    "Science Fiction", "Space Opera", "Dystopian", "Horror", "Thriller",
    "Mystery", "Romance", "MM Romance", "Fantasy Romance",
    "Historical Fiction", "Contemporary Fiction", "Nonfiction", "Biography",
    "Memoir", "History", "Science", "Technology", "Business", "Self-Help",
    "True Crime", "Humor"
]
DEFAULT_ALIASES = {
    "sci-fi": "Science Fiction", "sci fi": "Science Fiction",
    "science-fiction": "Science Fiction", "sf": "Science Fiction",
    "litrpg & gamelit": "LitRPG", "game lit": "LitRPG", "gamelit": "LitRPG",
    "gay romance": "MM Romance", "m/m romance": "MM Romance",
    "male/male romance": "MM Romance", "bio": "Biography",
    "autobiography": "Memoir", "non-fiction": "Nonfiction",
}

# Rules are intentionally additive. A book can satisfy several categories.
GENRE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Epic Fantasy": ("epic fantasy", "high fantasy"),
    "Urban Fantasy": ("urban fantasy",),
    "Dark Fantasy": ("dark fantasy",),
    "LitRPG": ("litrpg", "game lit", "gamelit", "role-playing game fiction"),
    "Fantasy Romance": ("fantasy romance", "romantic fantasy", "romantasy"),
    "Fantasy": ("fantasy", "fantasy fiction"),
    "Space Opera": ("space opera",),
    "Dystopian": ("dystopian", "dystopia"),
    "Science Fiction": ("science fiction", "sci fi", "sci-fi", "speculative fiction"),
    "Horror": ("horror", "supernatural horror", "ghost story"),
    "Thriller": ("thriller", "suspense"),
    "Mystery": ("mystery", "detective fiction", "crime fiction"),
    "MM Romance": ("mm romance", "m/m romance", "gay romance", "male male romance", "boys love"),
    "Romance": ("romance", "romantic fiction", "love story"),
    "Historical Fiction": ("historical fiction", "historical novel"),
    "Contemporary Fiction": ("contemporary fiction", "contemporary novel"),
    "Biography": ("biography", "biographical"),
    "Memoir": ("memoir", "autobiography"),
    "True Crime": ("true crime",),
    "History": ("history", "historical nonfiction"),
    "Technology": ("technology nonfiction", "computer science", "computing", "technology history"),
    "Science": ("popular science", "natural science", "biology", "physics", "chemistry", "astronomy", "scientific research"),
    "Business": ("business", "entrepreneurship", "management", "economics"),
    "Self-Help": ("self-help", "self help", "personal development"),
    "Humor": ("humor", "humour", "comedy", "comic fiction"),
    "Nonfiction": ("nonfiction", "non-fiction"),
}

NOISE_TOKEN_RE = re.compile(
    r"(?ix)\b(?:unabridged(?:\s+(?:audiobook|edition))?|retail|audiobook|ebook|epub|mp3|m4b|web[-_. ]?dl|webrip|"
    r"(?:64|96|128|192|256|320)\s?kbps)\b"
)
BRACKET_GROUP_RE = re.compile(r"(\([^()]*\)|\[[^\[\]]*\]|\{[^{}]*\})")
RELEASE_NOISE = NOISE_TOKEN_RE



RUNTIME_SETTING_PREFIX = "runtime:"
SECRET_SETTING_KEYS = {"abs_api_key", "ai_api_key"}

SETTINGS_UI = [
    ("Audiobookshelf", [
        {"key":"abs_url","label":"Audiobookshelf URL","kind":"text","help":"URL reachable from this container, e.g. http://host.docker.internal:13378."},
        {"key":"abs_api_key","label":"Audiobookshelf API key","kind":"password","help":"Stored in the local SQLite database. Leave blank to keep the currently saved key."},
        {"key":"library_id","label":"Default library","kind":"library","help":"Used by scheduled scans and as the default selection."},
    ]),
    ("Metadata providers", [
        {"key":"audiosilo_enabled","label":"Enable AudioSilo","kind":"bool","help":"Audiobook-specific narrator, duration, ASIN/ISBN, genres and series/order evidence."},
        {"key":"audiosilo_url","label":"AudioSilo URL","kind":"text"},
        {"key":"openlibrary_enabled","label":"Enable Open Library","kind":"bool","help":"Bibliographic and subject fallback, especially useful for e-books."},
        {"key":"openlibrary_contact","label":"Open Library contact","kind":"text","help":"Optional contact/email used to identify this application to Open Library."},
        {"key":"hardcover_enabled","label":"Enable Hardcover provider","kind":"bool","help":"Optional second provider for stronger metadata/series consensus."},
        {"key":"hardcover_url","label":"Hardcover provider URL","kind":"text"},
        {"key":"provider_min_score","label":"Provider minimum match score","kind":"number","step":"0.01","min":"0","max":"1"},
        {"key":"provider_cache_days","label":"Provider cache days","kind":"number","step":"1","min":"0"},
    ]),
    ("Local AI / Ollama", [
        {"key":"ai_provider","label":"AI provider","kind":"select","options":[("ollama","Ollama (local)"),("none","Off"),("openai","OpenAI cloud")]},
        {"key":"local_ai_only","label":"Local AI only","kind":"bool","help":"When enabled, cloud AI calls are blocked even if an OpenAI key exists."},
        {"key":"ai_scan_mode","label":"AI scan mode","kind":"select","options":[("smart","Smart — ambiguous/suspicious books"),("deep","Deep — audit every book")]},
        {"key":"ai_metadata_corrections","label":"Audit suspected metadata errors","kind":"bool","help":"Allows local AI to propose title/author/subtitle cleanup when formatting or provider evidence suggests the stored metadata is wrong."},
        {"key":"ai_max_completion_tokens","label":"Maximum AI output tokens","kind":"number","step":"1","min":"64"},
        {"key":"ai_cache_days","label":"AI cache days","kind":"number","step":"1","min":"0"},
        {"key":"ollama_url","label":"Ollama URL","kind":"text","help":"For Ollama on the Windows Docker host use http://host.docker.internal:11434, not localhost."},
        {"key":"ollama_model","label":"Ollama model","kind":"text"},
        {"key":"ollama_think","label":"Enable model thinking","kind":"bool","help":"Usually unnecessary for metadata classification and slower when enabled."},
        {"key":"ollama_keep_alive","label":"Ollama keep-alive","kind":"text","help":"Example: 15m."},
        {"key":"ollama_num_ctx","label":"Ollama context window","kind":"number","step":"1","min":"1024"},
        {"key":"ollama_temperature","label":"Ollama temperature","kind":"number","step":"0.05","min":"0","max":"2"},
        {"key":"ollama_concurrency","label":"Ollama concurrency","kind":"number","step":"1","min":"1","max":"16"},
    ]),
    ("Optional cloud AI", [
        {"key":"ai_base_url","label":"OpenAI-compatible API URL","kind":"text"},
        {"key":"ai_api_key","label":"Cloud AI API key","kind":"password","help":"Ignored while Local AI Only is enabled. Leave blank to preserve a saved key."},
        {"key":"ai_model","label":"Cloud AI model","kind":"text"},
        {"key":"ai_reasoning_effort","label":"Reasoning effort","kind":"select","options":[("minimal","Minimal"),("low","Low"),("medium","Medium"),("high","High")]},
        {"key":"ai_max_scan_cost_usd","label":"Maximum cloud AI cost per scan (USD)","kind":"number","step":"0.01","min":"0"},
        {"key":"ai_input_price_per_million","label":"Input $ / 1M tokens","kind":"number","step":"0.001","min":"0"},
        {"key":"ai_cached_input_price_per_million","label":"Cached input $ / 1M tokens","kind":"number","step":"0.001","min":"0"},
        {"key":"ai_output_price_per_million","label":"Output $ / 1M tokens","kind":"number","step":"0.001","min":"0"},
    ]),
    ("Performance", [
        {"key":"scan_concurrency","label":"Metadata scan concurrency","kind":"number","step":"1","min":"1","max":"32"},
        {"key":"apply_concurrency","label":"Apply concurrency","kind":"number","step":"1","min":"1","max":"32"},
        {"key":"review_page_size","label":"Review items per page","kind":"number","step":"1","min":"10","max":"250"},
        {"key":"cover_page_size","label":"Cover items per page","kind":"number","step":"1","min":"4","max":"100"},
    ]),
    ("Scheduling", [
        {"key":"scheduled_scans_enabled","label":"Enable scheduled scans","kind":"bool"},
        {"key":"scheduled_scan_hours","label":"Hours between scheduled scans","kind":"number","step":"1","min":"1"},
        {"key":"scheduled_use_ai","label":"Use AI on scheduled scans","kind":"bool"},
    ]),
    ("Automatic changes", [
        {"key":"allow_auto_apply","label":"Enable metadata Auto Apply","kind":"bool","help":"Only suggestions at or above the threshold are automatically written."},
        {"key":"auto_apply_threshold","label":"Auto Apply confidence threshold","kind":"number","step":"0.01","min":"0","max":"1"},
    ]),
    ("Audio + eBook pairing", [
        {"key":"media_library_root","label":"Media library root inside this container","kind":"text","help":"The Docker volume must already be mounted. Changing this setting cannot create a Docker volume mount."},
        {"key":"media_path_map","label":"Media path map (JSON)","kind":"textarea","help":"Example: {\"/audiobooks\":\"/abs-audiobooks\",\"/ebooks\":\"/abs-ebooks\"}"},
        {"key":"pair_min_score","label":"Minimum pair match score","kind":"number","step":"0.01","min":"0","max":"1"},
    ]),
]

RUNTIME_SETTING_KEYS = [f["key"] for _, fields in SETTINGS_UI for f in fields]


def _coerce_setting_value(key: str, raw: Any) -> Any:
    """Coerce UI/DB values to the type of the launch-time Settings value."""
    default = ENV_SETTINGS.get(key, getattr(settings, key, ""))
    if isinstance(default, bool):
        if isinstance(raw, bool):
            return raw
        return str(raw).strip().casefold() in {"1", "true", "yes", "on"}
    if isinstance(default, int) and not isinstance(default, bool):
        return int(raw)
    if isinstance(default, float):
        return float(raw)
    return str(raw or "")


def _normalize_runtime_values(values: dict[str, Any]) -> dict[str, Any]:
    out = dict(values)
    if out.get("local_ai_only") and str(out.get("ai_provider", "")).casefold() == "openai":
        out["ai_provider"] = "ollama"
    if str(out.get("ai_provider", "")).casefold() not in {"ollama", "openai", "none"}:
        out["ai_provider"] = "ollama"
    if str(out.get("ai_scan_mode", "")).casefold() not in {"smart", "deep"}:
        out["ai_scan_mode"] = "smart"
    # Bound values that can otherwise destabilize a home server/browser.
    out["scan_concurrency"] = max(1, min(32, int(out.get("scan_concurrency", 4))))
    out["apply_concurrency"] = max(1, min(32, int(out.get("apply_concurrency", 4))))
    out["ollama_concurrency"] = max(1, min(16, int(out.get("ollama_concurrency", 1))))
    out["review_page_size"] = max(10, min(250, int(out.get("review_page_size", 50))))
    out["cover_page_size"] = max(4, min(100, int(out.get("cover_page_size", 16))))
    out["provider_min_score"] = max(0.0, min(1.0, float(out.get("provider_min_score", .86))))
    out["auto_apply_threshold"] = max(0.0, min(1.0, float(out.get("auto_apply_threshold", .98))))
    out["pair_min_score"] = max(0.0, min(1.0, float(out.get("pair_min_score", .88))))
    out["scheduled_scan_hours"] = max(1, int(out.get("scheduled_scan_hours", 24)))
    out["ai_max_completion_tokens"] = max(64, int(out.get("ai_max_completion_tokens", 400)))
    out["ollama_num_ctx"] = max(1024, int(out.get("ollama_num_ctx", 8192)))
    out["ollama_temperature"] = max(0.0, min(2.0, float(out.get("ollama_temperature", .1))))
    return out


def apply_runtime_values(values: dict[str, Any]) -> None:
    global OLLAMA_SEMAPHORE
    values = _normalize_runtime_values(values)
    for key in RUNTIME_SETTING_KEYS:
        if key in values:
            setattr(settings, key, _coerce_setting_value(key, values[key]))
    OLLAMA_SEMAPHORE = asyncio.Semaphore(max(1, settings.ollama_concurrency))


def load_runtime_settings() -> None:
    with db() as con:
        rows = con.execute("SELECT key,value FROM app_settings WHERE key LIKE ?", (RUNTIME_SETTING_PREFIX + "%",)).fetchall()
    overrides: dict[str, Any] = {}
    for row in rows:
        key = str(row["key"])[len(RUNTIME_SETTING_PREFIX):]
        if key not in RUNTIME_SETTING_KEYS:
            continue
        try:
            overrides[key] = json.loads(row["value"])
        except Exception:
            overrides[key] = row["value"]
    if overrides:
        apply_runtime_values({**settings.model_dump(), **overrides})
    else:
        apply_runtime_values(settings.model_dump())


def persist_runtime_values(values: dict[str, Any]) -> None:
    values = _normalize_runtime_values(values)
    now = int(time.time())
    with db() as con:
        for key in RUNTIME_SETTING_KEYS:
            if key in values:
                con.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (RUNTIME_SETTING_PREFIX + key, json.dumps(values[key])))
        con.execute("INSERT INTO app_settings(key,value) VALUES('runtime:last_saved_at',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (json.dumps(now),))
    apply_runtime_values(values)


def runtime_override_keys() -> set[str]:
    with db() as con:
        rows = con.execute("SELECT key FROM app_settings WHERE key LIKE ?", (RUNTIME_SETTING_PREFIX + "%",)).fetchall()
    return {str(r["key"])[len(RUNTIME_SETTING_PREFIX):] for r in rows if str(r["key"]) != 'runtime:last_saved_at'}

class AIBudgetExceeded(RuntimeError):
    pass


def cleanup_release_title(title: str | None) -> str:
    """Remove known release noise and clean punctuation it leaves behind."""
    text = str(title or "").strip()
    if not text:
        return text
    if NOISE_TOKEN_RE.search(text):
        text = re.sub(r"[._]+", " ", text)

    def clean_group(match: re.Match) -> str:
        raw = match.group(0)
        inner = NOISE_TOKEN_RE.sub(" ", raw[1:-1])
        inner = re.sub(r"\s*[-–—|/,:;]+\s*", " ", inner)
        inner = re.sub(r"\s{2,}", " ", inner).strip()
        return f"{raw[0]}{inner}{raw[-1]}" if inner else " "

    previous = None
    while previous != text:
        previous = text
        text = BRACKET_GROUP_RE.sub(clean_group, text)
        text = NOISE_TOKEN_RE.sub(" ", text)
        text = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", " ", text)
        text = re.sub(r"\s+([,.:;!?])", r"\1", text)
        text = re.sub(r"(?:\s*[-–—|]+\s*)+$", "", text)
        text = re.sub(r"^(?:\s*[-–—|]+\s*)+", "", text)
        text = re.sub(r"\s{2,}", " ", text).strip(" ._-–—|")
    return text.strip()


SERIES_SEQUENCE_PATTERNS = [
    re.compile(r"(?i)(?:^|[/\\])\s*(\d+(?:\.\d+)?)\s*(?:[-.]\s+)") ,
    re.compile(r"(?i)\b(?:book|vol\.?|volume|#)\s*(\d+(?:\.\d+)?)\b"),
]


@contextmanager
def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()


def table_columns(con: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in con.execute(f"PRAGMA table_info({table})")}


def ensure_column(con: sqlite3.Connection, table: str, name: str, sql_type: str, default_sql: str | None = None):
    if name not in table_columns(con, table):
        suffix = f" DEFAULT {default_sql}" if default_sql is not None else ""
        con.execute(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}{suffix}")


def init_db():
    os.makedirs("/config", exist_ok=True)
    now = int(time.time())
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            library_id TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            finished_at INTEGER,
            item_count INTEGER DEFAULT 0,
            source TEXT DEFAULT 'manual',
            status TEXT DEFAULT 'running',
            processed_count INTEGER DEFAULT 0,
            flagged_count INTEGER DEFAULT 0,
            error_count INTEGER DEFAULT 0,
            current_title TEXT,
            progress_message TEXT,
            updated_at INTEGER,
            use_ai INTEGER DEFAULT 0,
            media_stats TEXT DEFAULT '{}',
            ai_mode TEXT DEFAULT 'smart',
            ai_requests INTEGER DEFAULT 0,
            ai_cache_hits INTEGER DEFAULT 0,
            ai_input_tokens INTEGER DEFAULT 0,
            ai_cached_input_tokens INTEGER DEFAULT 0,
            ai_output_tokens INTEGER DEFAULT 0,
            ai_reasoning_tokens INTEGER DEFAULT 0,
            ai_estimated_cost REAL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER,
            item_id TEXT NOT NULL,
            library_id TEXT NOT NULL,
            title TEXT,
            reasons TEXT NOT NULL,
            old_metadata TEXT NOT NULL,
            new_metadata TEXT NOT NULL,
            confidence REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL DEFAULT 'rules',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at INTEGER NOT NULL,
            applied_at INTEGER,
            error TEXT,
            evidence TEXT DEFAULT '[]',
            media_profile TEXT DEFAULT '{}',
            selected_fields TEXT DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS app_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS scan_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            level TEXT DEFAULT 'info',
            message TEXT NOT NULL,
            item_id TEXT,
            title TEXT
        );
        CREATE TABLE IF NOT EXISTS provider_cache (
            provider TEXT NOT NULL,
            cache_key TEXT NOT NULL,
            fetched_at INTEGER NOT NULL,
            payload TEXT NOT NULL,
            PRIMARY KEY(provider, cache_key)
        );
        CREATE TABLE IF NOT EXISTS item_health (
            library_id TEXT NOT NULL, item_id TEXT NOT NULL, scan_id INTEGER, title TEXT,
            metadata TEXT NOT NULL DEFAULT '{}', media_profile TEXT NOT NULL DEFAULT '{}',
            quality_score REAL NOT NULL DEFAULT 0, dimensions TEXT NOT NULL DEFAULT '{}',
            issues TEXT NOT NULL DEFAULT '[]', cover_candidates TEXT NOT NULL DEFAULT '[]',
            has_cover INTEGER NOT NULL DEFAULT 0, updated_at INTEGER NOT NULL,
            PRIMARY KEY(library_id,item_id)
        );
        CREATE TABLE IF NOT EXISTS duplicate_pairs (
            pair_key TEXT PRIMARY KEY, library_id TEXT NOT NULL, scan_id INTEGER,
            item_a TEXT NOT NULL, item_b TEXT NOT NULL, strength TEXT NOT NULL, reason TEXT NOT NULL,
            details TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending', updated_at INTEGER NOT NULL
        );
        CREATE TABLE IF NOT EXISTS media_pairings (
            pair_key TEXT PRIMARY KEY, library_id TEXT NOT NULL, scan_id INTEGER,
            audio_item_id TEXT NOT NULL, ebook_item_id TEXT NOT NULL, score REAL NOT NULL,
            reason TEXT NOT NULL, details TEXT NOT NULL DEFAULT '{}', status TEXT NOT NULL DEFAULT 'pending',
            updated_at INTEGER NOT NULL, paired_at INTEGER, error TEXT
        );
        CREATE TABLE IF NOT EXISTS genre_decisions (
            library_id TEXT NOT NULL, genre TEXT NOT NULL, decision TEXT NOT NULL, updated_at INTEGER NOT NULL,
            PRIMARY KEY(library_id,genre)
        );
        CREATE TABLE IF NOT EXISTS ai_cache (
            cache_key TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL, created_at INTEGER NOT NULL,
            payload TEXT NOT NULL, usage TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS cover_updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT, library_id TEXT NOT NULL, item_id TEXT NOT NULL, title TEXT,
            cover_url TEXT NOT NULL, provider TEXT, match_score REAL, applied_at INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'updated', error TEXT
        );
        CREATE TABLE IF NOT EXISTS book_controls (
            library_id TEXT NOT NULL, item_id TEXT NOT NULL, title TEXT,
            full_lock INTEGER NOT NULL DEFAULT 0, metadata_lock INTEGER NOT NULL DEFAULT 0,
            cover_lock INTEGER NOT NULL DEFAULT 0, scan_exempt INTEGER NOT NULL DEFAULT 0,
            complete INTEGER NOT NULL DEFAULT 0, reason TEXT, updated_at INTEGER NOT NULL,
            PRIMARY KEY(library_id,item_id)
        );
        CREATE TABLE IF NOT EXISTS cover_ignores (
            library_id TEXT NOT NULL, item_id TEXT NOT NULL, cover_url TEXT NOT NULL,
            provider TEXT, reason TEXT, ignored_at INTEGER NOT NULL,
            PRIMARY KEY(library_id,item_id,cover_url)
        );
        CREATE INDEX IF NOT EXISTS idx_cover_updates_item ON cover_updates(library_id,item_id,status,applied_at);
        CREATE INDEX IF NOT EXISTS idx_book_controls_library ON book_controls(library_id,complete,full_lock,scan_exempt);
        CREATE INDEX IF NOT EXISTS idx_cover_ignores_item ON cover_ignores(library_id,item_id,ignored_at);
        CREATE INDEX IF NOT EXISTS idx_suggestions_status ON suggestions(status);
        CREATE INDEX IF NOT EXISTS idx_suggestions_item ON suggestions(item_id);
        CREATE INDEX IF NOT EXISTS idx_scan_events_scan ON scan_events(scan_id,id);
        CREATE INDEX IF NOT EXISTS idx_health_library ON item_health(library_id,quality_score);
        CREATE INDEX IF NOT EXISTS idx_duplicates_library ON duplicate_pairs(library_id,status);
        CREATE INDEX IF NOT EXISTS idx_pairings_library ON media_pairings(library_id,status,score);
        """)
        # Migrate v0.1/v0.2 databases.
        ensure_column(con, "scans", "source", "TEXT", "'manual'")
        ensure_column(con, "scans", "status", "TEXT", "'completed'")
        ensure_column(con, "scans", "processed_count", "INTEGER", "0")
        ensure_column(con, "scans", "flagged_count", "INTEGER", "0")
        ensure_column(con, "scans", "error_count", "INTEGER", "0")
        ensure_column(con, "scans", "current_title", "TEXT")
        ensure_column(con, "scans", "progress_message", "TEXT")
        ensure_column(con, "scans", "updated_at", "INTEGER")
        ensure_column(con, "scans", "use_ai", "INTEGER", "0")
        ensure_column(con, "scans", "media_stats", "TEXT", "'{}'")
        ensure_column(con, "scans", "ai_mode", "TEXT", "'smart'")
        ensure_column(con, "scans", "ai_requests", "INTEGER", "0")
        ensure_column(con, "scans", "ai_cache_hits", "INTEGER", "0")
        ensure_column(con, "scans", "ai_input_tokens", "INTEGER", "0")
        ensure_column(con, "scans", "ai_cached_input_tokens", "INTEGER", "0")
        ensure_column(con, "scans", "ai_output_tokens", "INTEGER", "0")
        ensure_column(con, "scans", "ai_reasoning_tokens", "INTEGER", "0")
        ensure_column(con, "scans", "ai_estimated_cost", "REAL", "0")
        ensure_column(con, "suggestions", "evidence", "TEXT", "'[]'")
        ensure_column(con, "suggestions", "media_profile", "TEXT", "'{}'")
        ensure_column(con, "suggestions", "selected_fields", "TEXT", "'[]'")

        # Preserve v0.2 review queues: older pending suggestions did not store a
        # per-field selection. Seed them from the actual metadata diff so they
        # remain immediately reviewable/applicable after upgrading to v0.3.
        for row in con.execute("""SELECT id,old_metadata,new_metadata FROM suggestions
                                WHERE status='pending' AND (selected_fields IS NULL OR selected_fields='' OR selected_fields='[]')""").fetchall():
            try:
                old_md = json.loads(row["old_metadata"] or "{}")
                new_md = json.loads(row["new_metadata"] or "{}")
                changed = [k for k in new_md.keys() if old_md.get(k) != new_md.get(k)]
                con.execute("UPDATE suggestions SET selected_fields=? WHERE id=?", (json.dumps(changed), row["id"]))
            except Exception:
                pass

        con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('genres',?)", (json.dumps(DEFAULT_GENRES),))
        con.execute("INSERT OR IGNORE INTO app_settings(key,value) VALUES('genre_aliases',?)", (json.dumps(DEFAULT_ALIASES),))
        # A process restart cannot resume an in-memory scan task safely.
        con.execute("""UPDATE scans SET status='interrupted', finished_at=?, updated_at=?,
                       progress_message='Interrupted by application restart'
                       WHERE status='running'""", (now, now))


def get_setting(key: str, default: Any) -> Any:
    with db() as con:
        row = con.execute("SELECT value FROM app_settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except Exception:
        return default


def set_setting(key: str, value: Any):
    with db() as con:
        con.execute("INSERT INTO app_settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, json.dumps(value)))


@app.on_event("startup")
async def startup():
    global HTTP_CLIENT
    init_db()
    # Database-backed application settings override .env/container values.
    load_runtime_settings()
    HTTP_CLIENT = httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        follow_redirects=True,
        limits=httpx.Limits(max_connections=16, max_keepalive_connections=12, keepalive_expiry=45.0),
        headers={"User-Agent": "AI-Librarian/0.5.4"},
    )
    await restart_scheduled_scan_task()


@app.on_event("shutdown")
async def shutdown():
    global HTTP_CLIENT, SCHEDULED_SCAN_TASK
    if SCHEDULED_SCAN_TASK is not None:
        SCHEDULED_SCAN_TASK.cancel()
        SCHEDULED_SCAN_TASK = None
    if HTTP_CLIENT is not None:
        await HTTP_CLIENT.aclose()
        HTTP_CLIENT = None


def http_client() -> httpx.AsyncClient:
    if HTTP_CLIENT is None:
        raise RuntimeError("HTTP client is not initialized")
    return HTTP_CLIENT


def abs_headers() -> dict[str, str]:
    if not settings.abs_api_key:
        raise HTTPException(500, "ABS_API_KEY is not configured")
    return {"Authorization": f"Bearer {settings.abs_api_key}"}


async def abs_get(path: str, params: dict | None = None) -> Any:
    r = await http_client().get(settings.abs_url.rstrip("/") + path, headers=abs_headers(), params=params)
    r.raise_for_status()
    return r.json()


async def abs_patch(path: str, payload: dict) -> Any:
    r = await http_client().patch(settings.abs_url.rstrip("/") + path,
                                  headers={**abs_headers(), "Content-Type": "application/json"}, json=payload)
    r.raise_for_status()
    return r.json()


async def abs_post(path: str, payload: dict | None = None, params: dict | None = None) -> Any:
    r = await http_client().post(settings.abs_url.rstrip("/") + path,
                                 headers={**abs_headers(), "Content-Type": "application/json"}, json=payload or {},
                                 params=params, timeout=90.0)
    r.raise_for_status()
    return r.json() if "json" in r.headers.get("content-type", "") else {"ok": True, "text": r.text}


async def abs_get_bytes(path: str, params: dict | None = None) -> tuple[bytes, str]:
    r = await http_client().get(settings.abs_url.rstrip("/") + path, headers=abs_headers(), params=params)
    r.raise_for_status()
    return r.content, r.headers.get("content-type", "image/jpeg")


async def get_all_items(library_id: str) -> list[dict]:
    all_items: list[dict] = []
    page, limit = 0, 200
    while True:
        data = await abs_get(f"/api/libraries/{library_id}/items", {"limit": limit, "page": page, "minified": 0})
        results = data.get("results") or data.get("items") or []
        all_items.extend(results)
        if len(results) < limit:
            break
        page += 1
        if page > 10000:
            break
    return all_items


def _first_nonempty(*values):
    for v in values:
        if v not in (None, "", [], {}):
            return v
    return None


def metadata_of(item: dict) -> dict:
    media = item.get("media") or {}
    md = media.get("metadata") or {}
    author = _first_nonempty(md.get("authorName"), md.get("author"))
    narrator = _first_nonempty(md.get("narratorName"), md.get("narrator"), media.get("narratorName"))
    if isinstance(author, list):
        author = ", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in author)
    if isinstance(narrator, list):
        narrator = ", ".join(str(x.get("name") if isinstance(x, dict) else x) for x in narrator)
    return {
        "title": md.get("title"), "subtitle": md.get("subtitle"),
        "authorName": author, "narratorName": narrator,
        "publisher": md.get("publisher"), "publishedYear": md.get("publishedYear"),
        "description": md.get("description"), "genres": md.get("genres") or [],
        "series": md.get("series") or [], "language": md.get("language"),
        "isbn": md.get("isbn"), "asin": md.get("asin"),
    }


def extension_from_value(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, dict):
        for key in ("ext", "extension", "format", "path", "relPath", "filename", "name"):
            ext = extension_from_value(value.get(key))
            if ext:
                return ext
        return None
    s = str(value).strip().lower().split("?")[0]
    if s.startswith(".") and len(s) <= 8:
        return s[1:]
    if "." in s:
        ext = s.rsplit(".", 1)[-1]
        if 1 <= len(ext) <= 7:
            return ext
    if s in AUDIO_EXTENSIONS | EBOOK_EXTENSIONS:
        return s
    return None


def media_profile_of(item: dict) -> dict:
    media = item.get("media") or {}
    audio_files = media.get("audioFiles") or []
    ebook_file = media.get("ebookFile")
    audio_count = len(audio_files)
    try:
        audio_count = max(audio_count, int(media.get("numAudioFiles") or 0))
    except Exception:
        pass

    ebook_format = _first_nonempty(media.get("ebookFileFormat"), extension_from_value(ebook_file))

    # Fall back to the library file list when older/newer ABS response shapes omit expanded media files.
    for lf in item.get("libraryFiles") or []:
        ext = extension_from_value(lf)
        if ext in AUDIO_EXTENSIONS:
            audio_count = max(audio_count, 1)
        elif ext in EBOOK_EXTENSIONS and not ebook_format:
            ebook_format = ext

    has_audio = audio_count > 0
    has_ebook = bool(ebook_file or ebook_format)
    if has_audio and has_ebook:
        kind = "hybrid"
    elif has_audio:
        kind = "audiobook"
    elif has_ebook:
        kind = "ebook"
    else:
        kind = "unknown"

    try:
        duration = float(media.get("duration") or 0)
    except Exception:
        duration = 0.0

    return {
        "kind": kind,
        "label": {
            "audiobook": "Audiobook",
            "ebook": f"E-book ({str(ebook_format).upper()})" if ebook_format else "E-book",
            "hybrid": f"Audiobook + {str(ebook_format).upper()}" if ebook_format else "Audiobook + E-book",
            "unknown": "Unknown book format",
        }[kind],
        "hasAudio": has_audio,
        "hasEbook": has_ebook,
        "ebookFormat": str(ebook_format).lower() if ebook_format else None,
        "audioFileCount": audio_count,
        "durationSeconds": duration,
        "requiresNarrator": has_audio,
    }


def normalize_text(s: Any) -> str:
    s = str(s or "").casefold()
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_identifier(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def similarity(a: Any, b: Any) -> float:
    aa, bb = normalize_text(a), normalize_text(b)
    if not aa or not bb:
        return 0.0
    return difflib.SequenceMatcher(None, aa, bb).ratio()


def duration_similarity(local_seconds: float, remote_minutes: Any) -> float | None:
    try:
        remote_seconds = float(remote_minutes) * 60.0
    except (TypeError, ValueError):
        return None
    if local_seconds <= 0 or remote_seconds <= 0:
        return None
    return max(0.0, 1.0 - abs(local_seconds - remote_seconds) / max(local_seconds, remote_seconds))


def canonical_genres() -> list[str]:
    return get_setting("genres", DEFAULT_GENRES)


def genre_aliases() -> dict[str, str]:
    return get_setting("genre_aliases", DEFAULT_ALIASES)


def normalize_genres(genres: list[str]) -> list[str]:
    canon, aliases = canonical_genres(), genre_aliases()
    out, seen = [], set()
    for genre in genres or []:
        raw = str(genre or "").strip()
        if not raw:
            continue
        candidate = aliases.get(raw.casefold(), raw)
        if ">" in candidate:
            candidate = candidate.split(">")[-1].strip()
        canonical = next((g for g in canon if g.casefold() == candidate.casefold()), candidate)
        if canonical.casefold() not in seen:
            seen.add(canonical.casefold())
            out.append(canonical)
    return out


def normalize_person_name(name: str | None) -> str | None:
    if not name:
        return name
    name = re.sub(r"\s+", " ", name).strip()
    if name.isupper() and len(name) > 3:
        return name.title()
    return name


def normalized_series(series: Any) -> list[dict]:
    out = []
    if not isinstance(series, list):
        return out
    for s in series:
        if isinstance(s, str):
            if s.strip():
                out.append({"series": s.strip(), "sequence": ""})
            continue
        if isinstance(s, dict):
            name = _first_nonempty(s.get("series"), s.get("name"))
            if not name:
                continue
            out.append({"series": str(name).strip(), "sequence": str(s.get("sequence") or "").strip()})
    return out



AUTHOR_TITLE_SPLIT_RE = re.compile(r"^\s*(?P<left>.{2,100}?)\s+(?P<sep>[-–—|])\s+(?P<right>.{2,240}?)\s*$")


def _looks_like_person_name_segment(value: str) -> bool:
    """Conservative person-name heuristic for embedded `Author - Title` metadata."""
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .,-–—|:")
    if not text or any(ch.isdigit() for ch in text):
        return False
    words = text.split()
    if not 1 <= len(words) <= 6:
        return False
    blocked = {"book", "volume", "vol", "chapter", "part", "episode", "series", "audiobook", "unabridged"}
    if any(w.casefold().strip(".,") in blocked for w in words):
        return False
    # Names can contain apostrophes, hyphens, initials and periods, but should contain letters.
    return all(re.search(r"[A-Za-z]", w) and re.fullmatch(r"[A-Za-zÀ-ÖØ-öø-ÿ'’.\-]+", w) for w in words)


def title_author_structure_hint(md: dict) -> dict | None:
    """Return a conservative hint when the title appears to contain an embedded author prefix."""
    raw_title = cleanup_release_title(md.get("title"))
    author = str(md.get("authorName") or "").strip()
    if not raw_title:
        return None
    m = AUTHOR_TITLE_SPLIT_RE.match(raw_title)
    if not m:
        return None
    left = m.group("left").strip(" .,-–—|:")
    right = m.group("right").strip(" .,-–—|:")
    if not left or not right or not _looks_like_person_name_segment(left):
        return None
    if len(right.split()) > 30:
        return None

    if author:
        sim = similarity(left, author)
        if sim >= 0.92:
            return {
                "type": "author-title-split",
                "originalTitle": str(md.get("title") or ""),
                "suggestedAuthor": author,
                "suggestedTitle": right,
                "separator": m.group("sep"),
                "confidence": 0.995,
                "authorAlreadyVerified": True,
                "reason": "Title repeats the existing author before a separator",
            }
        # If ABS already has a different author, do not assume the left side is another author.
        return None

    return {
        "type": "author-title-split",
        "originalTitle": str(md.get("title") or ""),
        "suggestedAuthor": left,
        "suggestedTitle": right,
        "separator": m.group("sep"),
        "confidence": 0.86,
        "authorAlreadyVerified": False,
        "reason": "Title may contain an embedded author before a separator",
    }


def metadata_for_provider_lookup(md: dict) -> dict:
    """Use a structural hint only for discovery, without mutating stored metadata."""
    lookup = dict(md)
    hint = title_author_structure_hint(md)
    if hint:
        lookup["title"] = hint["suggestedTitle"]
        lookup["authorName"] = hint["suggestedAuthor"]
    return lookup


def structure_hint_verified(hint: dict | None, evidence: list[dict]) -> bool:
    if not hint:
        return False
    if hint.get("authorAlreadyVerified"):
        return True
    for e in evidence or []:
        if e.get("error"):
            continue
        if float(e.get("score") or 0) < 0.90:
            continue
        if similarity(e.get("title"), hint.get("suggestedTitle")) >= 0.92 and similarity(e.get("authorName"), hint.get("suggestedAuthor")) >= 0.90:
            return True
    return False


def metadata_suspicion_reasons(md: dict) -> list[str]:
    """Cheap local checks that decide whether Smart-mode AI should inspect metadata structure."""
    out: list[str] = []
    title = str(md.get("title") or "").strip()
    author = str(md.get("authorName") or "").strip()
    hint = title_author_structure_hint(md)
    if hint:
        out.append(hint["reason"])
    if title and author and normalize_text(title) == normalize_text(author):
        out.append("Title and author are identical")
    if author and re.search(r"\s[-–—|]\s", author):
        out.append("Author field contains a title-like separator")
    if re.search(r"(?i)\.(?:mp3|m4b|epub|pdf)$", title):
        out.append("Title contains a media file extension")
    return out


def preferred_author_spellings(items: list[dict]) -> dict[str, str]:
    buckets: dict[str, Counter] = {}
    for item in items:
        a = str(metadata_of(item).get("authorName") or "").strip()
        key = normalize_text(a)
        if not key:
            continue
        buckets.setdefault(key, Counter())[a] += 1
    return {k: c.most_common(1)[0][0] for k, c in buckets.items()}


def path_sequence_hint(item: dict) -> str | None:
    text = " / ".join(str(item.get(k) or "") for k in ("relPath", "path", "name", "title"))
    for pattern in SERIES_SEQUENCE_PATTERNS:
        m = pattern.search(text)
        if m:
            return m.group(1)
    return None


def rule_suggestion(md: dict, author_spellings: dict[str, str], profile: dict) -> tuple[list[str], dict, float]:
    reasons, new = [], dict(md)
    title = str(md.get("title") or "").strip()
    author = str(md.get("authorName") or "").strip()
    narrator = str(md.get("narratorName") or "").strip()
    genres = md.get("genres") or []
    confidence_floor = 0.0

    if not title:
        reasons.append("Missing title")
    else:
        cleaned = cleanup_release_title(title)
        if cleaned != title:
            reasons.append("Title contains removable release text or leftover punctuation")
            if cleaned and len(cleaned) >= 2:
                new["title"] = cleaned
    if title and title.isupper() and len(title) > 5:
        reasons.append("Title is all uppercase")
        new["title"] = str(new.get("title") or title).title()

    # Detect common imported filename structure such as `Author - Book Title`.
    structure_hint = title_author_structure_hint(new)
    if structure_hint:
        new["title"] = structure_hint["suggestedTitle"]
        if not author:
            new["authorName"] = structure_hint["suggestedAuthor"]
            reasons.append("Title may contain embedded author before separator")
        else:
            reasons.append("Title repeats author before separator")
        confidence_floor = max(confidence_floor, float(structure_hint["confidence"]))

    if not author and not (structure_hint and new.get("authorName")):
        reasons.append("Missing author")
    elif author:
        clean_author = normalize_person_name(author)
        preferred = author_spellings.get(normalize_text(author))
        if preferred and preferred != author:
            reasons.append("Author spelling/capitalization differs from library majority")
            new["authorName"] = preferred
        elif clean_author != author:
            reasons.append("Author capitalization can be normalized")
            new["authorName"] = clean_author

    # Narrator is applicable only when an audio recording exists.
    if profile["requiresNarrator"] and not narrator:
        reasons.append("Audiobook is missing narrator metadata")

    if not genres:
        reasons.append("Missing genres")
    else:
        ng = normalize_genres(genres)
        if ng != genres:
            reasons.append("Genres can be normalized")
            new["genres"] = ng
        if len(genres) == 1 and str(genres[0]).strip().casefold() in {"fiction", "non-fiction", "nonfiction"}:
            reasons.append("Genre is overly broad")

    series = normalized_series(md.get("series") or [])
    if series and any(not s["sequence"] for s in series):
        reasons.append("Series entry is missing a sequence")

    changed = new != md
    confidence = 0.97 if changed and all("Missing" not in r and "missing" not in r for r in reasons) else 0.72
    if reasons and not changed:
        confidence = 0.60
    confidence = max(confidence, confidence_floor)
    return reasons, new, confidence



# ---------- v0.5.6 book protection / cover decisions ----------
DEFAULT_BOOK_CONTROL = {
    "full_lock": False, "metadata_lock": False, "cover_lock": False,
    "scan_exempt": False, "complete": False, "reason": "",
}


def _control_dict(row: sqlite3.Row | None) -> dict:
    if not row:
        return dict(DEFAULT_BOOK_CONTROL)
    d = dict(DEFAULT_BOOK_CONTROL)
    for key in ("full_lock", "metadata_lock", "cover_lock", "scan_exempt", "complete"):
        d[key] = bool(row[key])
    d["reason"] = str(row["reason"] or "")
    d["title"] = str(row["title"] or "")
    d["updated_at"] = int(row["updated_at"] or 0)
    return d


def get_book_control(library_id: str, item_id: str) -> dict:
    with db() as con:
        row = con.execute("SELECT * FROM book_controls WHERE library_id=? AND item_id=?", (library_id, str(item_id))).fetchone()
    return _control_dict(row)


def get_book_controls(library_id: str) -> dict[str, dict]:
    if not library_id:
        return {}
    with db() as con:
        rows = con.execute("SELECT * FROM book_controls WHERE library_id=?", (library_id,)).fetchall()
    return {str(r["item_id"]): _control_dict(r) for r in rows}


def metadata_is_locked(library_id: str, item_id: str) -> bool:
    c = get_book_control(library_id, item_id)
    return bool(c["full_lock"] or c["metadata_lock"] or c["complete"])


def cover_is_locked(library_id: str, item_id: str) -> bool:
    c = get_book_control(library_id, item_id)
    return bool(c["full_lock"] or c["cover_lock"] or c["complete"])


def scan_is_exempt(control: dict) -> bool:
    # If neither metadata nor covers may change, provider/AI work has no value.
    return bool(control.get("full_lock") or control.get("complete") or control.get("scan_exempt") or
                (control.get("metadata_lock") and control.get("cover_lock")))


def set_book_control(library_id: str, item_id: str, title: str = "", action: str = "", reason: str = "") -> dict:
    valid = {"full-lock", "metadata-lock", "cover-lock", "scan-exempt", "mark-complete",
             "unlock", "unlock-metadata", "unlock-cover", "unlock-scan", "unmark-complete"}
    if action not in valid:
        raise HTTPException(400, "Invalid book protection action")
    current = get_book_control(library_id, item_id)
    values = {k: bool(current.get(k)) for k in ("full_lock", "metadata_lock", "cover_lock", "scan_exempt", "complete")}
    if action == "full-lock":
        values.update(full_lock=True, metadata_lock=True, cover_lock=True, scan_exempt=True)
    elif action == "metadata-lock": values["metadata_lock"] = True
    elif action == "cover-lock": values["cover_lock"] = True
    elif action == "scan-exempt": values["scan_exempt"] = True
    elif action == "mark-complete":
        values.update(full_lock=True, metadata_lock=True, cover_lock=True, scan_exempt=True, complete=True)
    elif action == "unlock":
        values = {k: False for k in values}
    elif action == "unlock-metadata":
        values["metadata_lock"] = False; values["full_lock"] = False; values["complete"] = False
    elif action == "unlock-cover":
        values["cover_lock"] = False; values["full_lock"] = False; values["complete"] = False
    elif action == "unlock-scan":
        values["scan_exempt"] = False; values["full_lock"] = False; values["complete"] = False
    elif action == "unmark-complete":
        values["complete"] = False; values["full_lock"] = False
    now = int(time.time())
    title = str(title or current.get("title") or item_id)
    default_reasons={"full-lock":"Fully locked","metadata-lock":"Metadata protected","cover-lock":"Current cover protected",
                     "scan-exempt":"Excluded from automatic/provider/AI scans","mark-complete":"Marked complete"}
    final_reason = str(reason).strip() if str(reason).strip() else (str(current.get("reason") or "") or default_reasons.get(action,""))
    with db() as con:
        con.execute("""INSERT INTO book_controls(library_id,item_id,title,full_lock,metadata_lock,cover_lock,scan_exempt,complete,reason,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(library_id,item_id) DO UPDATE SET title=excluded.title,full_lock=excluded.full_lock,
                       metadata_lock=excluded.metadata_lock,cover_lock=excluded.cover_lock,scan_exempt=excluded.scan_exempt,
                       complete=excluded.complete,reason=excluded.reason,updated_at=excluded.updated_at""",
                    (library_id,str(item_id),title,int(values["full_lock"]),int(values["metadata_lock"]),int(values["cover_lock"]),
                     int(values["scan_exempt"]),int(values["complete"]),final_reason,now))
        if values["full_lock"] or values["metadata_lock"] or values["complete"]:
            con.execute("UPDATE suggestions SET status='ignored',error='Protected by book lock' WHERE library_id=? AND item_id=? AND status='pending'",
                        (library_id,str(item_id)))
        if values["full_lock"] or values["complete"]:
            con.execute("UPDATE duplicate_pairs SET status='dismissed',updated_at=? WHERE library_id=? AND status='pending' AND (item_a=? OR item_b=?)",
                        (now,library_id,str(item_id),str(item_id)))
            con.execute("UPDATE media_pairings SET status='dismissed',updated_at=? WHERE library_id=? AND status='pending' AND (audio_item_id=? OR ebook_item_id=?)",
                        (now,library_id,str(item_id),str(item_id)))
    return get_book_control(library_id, item_id)


def ignored_cover_urls(library_id: str, item_id: str) -> set[str]:
    with db() as con:
        rows = con.execute("SELECT cover_url FROM cover_ignores WHERE library_id=? AND item_id=?", (library_id,str(item_id))).fetchall()
    return {str(r["cover_url"]) for r in rows}


def filter_cover_candidates(library_id: str, item_id: str, candidates: list[dict]) -> list[dict]:
    if cover_is_locked(library_id, item_id):
        return []
    ignored = ignored_cover_urls(library_id, item_id)
    return [c for c in candidates if str(c.get("url") or "") not in ignored]


def ignore_cover_candidate(library_id: str, item_id: str, cover_url: str, provider: str = "", reason: str = ""):
    if not cover_url.startswith(("http://", "https://")):
        raise HTTPException(400, "Invalid cover URL")
    with db() as con:
        con.execute("""INSERT INTO cover_ignores(library_id,item_id,cover_url,provider,reason,ignored_at)
                       VALUES(?,?,?,?,?,?) ON CONFLICT(library_id,item_id,cover_url) DO UPDATE SET
                       provider=excluded.provider,reason=excluded.reason,ignored_at=excluded.ignored_at""",
                    (library_id,str(item_id),cover_url,provider or None,reason or None,int(time.time())))


def pending_cover_rows(library_id: str) -> list[dict]:
    if not library_id:
        return []
    with db() as con:
        rows = con.execute("""SELECT h.* FROM item_health h
            WHERE h.library_id=? AND h.cover_candidates<>'[]'
            AND NOT EXISTS (SELECT 1 FROM cover_updates u WHERE u.library_id=h.library_id AND u.item_id=h.item_id AND u.status='updated')
            AND NOT EXISTS (SELECT 1 FROM book_controls b WHERE b.library_id=h.library_id AND b.item_id=h.item_id
                            AND (b.full_lock=1 OR b.cover_lock=1 OR b.complete=1))
            ORDER BY h.has_cover ASC,h.quality_score ASC,h.title""", (library_id,)).fetchall()
        ignored_rows = con.execute("SELECT item_id,cover_url FROM cover_ignores WHERE library_id=?", (library_id,)).fetchall()
    ignored_by_item=defaultdict(set)
    for r in ignored_rows:
        ignored_by_item[str(r["item_id"])].add(str(r["cover_url"]))
    out=[]
    for row in rows:
        d=dict(row)
        try: candidates=json.loads(d.get("cover_candidates") or "[]")
        except Exception: candidates=[]
        ignored=ignored_by_item.get(str(d["item_id"]),set())
        candidates=[c for c in candidates if str(c.get("url") or "") not in ignored]
        if candidates:
            d["filtered_covers"]=candidates
            out.append(d)
    return out


# ---------- Provider cache ----------

def provider_cache_key(md: dict, profile: dict) -> str:
    raw = "|".join([
        normalize_text(md.get("title")), normalize_text(md.get("authorName")),
        normalize_identifier(md.get("isbn")), normalize_identifier(md.get("asin")),
        str(round(float(profile.get("durationSeconds") or 0) / 60.0)), profile.get("kind") or "",
    ])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def cache_get(provider: str, key: str) -> list[dict] | None:
    cutoff = int(time.time()) - max(settings.provider_cache_days, 1) * 86400
    with db() as con:
        row = con.execute("SELECT fetched_at,payload FROM provider_cache WHERE provider=? AND cache_key=?", (provider, key)).fetchone()
    if not row or row["fetched_at"] < cutoff:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def cache_set(provider: str, key: str, payload: list[dict]):
    with db() as con:
        con.execute("""INSERT INTO provider_cache(provider,cache_key,fetched_at,payload) VALUES(?,?,?,?)
                       ON CONFLICT(provider,cache_key) DO UPDATE SET fetched_at=excluded.fetched_at,payload=excluded.payload""",
                    (provider, key, int(time.time()), json.dumps(payload)))


def candidate_match_score(md: dict, profile: dict, cand: dict) -> float:
    title_score = similarity(md.get("title"), cand.get("title"))
    author_score = similarity(md.get("authorName"), cand.get("authorName")) if md.get("authorName") else 0.75
    local_isbn, local_asin = normalize_identifier(md.get("isbn")), normalize_identifier(md.get("asin"))
    remote_isbns = {normalize_identifier(x) for x in ([cand.get("isbn")] + list(cand.get("isbns") or [])) if x}
    remote_asins = {normalize_identifier(x) for x in ([cand.get("asin")] + list(cand.get("asins") or [])) if x}
    identifier_exact = bool((local_isbn and local_isbn in remote_isbns) or
                            (local_asin and local_asin in remote_asins))
    dur_sim = duration_similarity(float(profile.get("durationSeconds") or 0), cand.get("duration"))
    if dur_sim is None:
        score = 0.70 * title_score + 0.30 * author_score
    else:
        score = 0.52 * title_score + 0.25 * author_score + 0.23 * dur_sim
    if identifier_exact:
        score = max(score, 0.997)
    cand["titleSimilarity"] = round(title_score, 4)
    cand["authorSimilarity"] = round(author_score, 4)
    cand["durationSimilarity"] = round(dur_sim, 4) if dur_sim is not None else None
    cand["identifierExact"] = identifier_exact
    cand["score"] = round(min(score, 0.999), 4)
    return cand["score"]


def map_custom_match(provider: str, m: dict, md: dict, profile: dict) -> dict:
    author = _first_nonempty(m.get("author"), m.get("authorName"))
    narrator = _first_nonempty(m.get("narrator"), m.get("narratorName"))
    cand = {
        "provider": provider,
        "title": m.get("title"), "subtitle": m.get("subtitle"),
        "authorName": author, "narratorName": narrator,
        "publisher": m.get("publisher"), "publishedYear": m.get("publishedYear"),
        "description": m.get("description"), "coverUrl": _first_nonempty(m.get("cover"), m.get("coverUrl")),
        "isbn": m.get("isbn"), "asin": m.get("asin"),
        "genres": m.get("genres") or [], "tags": m.get("tags") or [],
        "series": normalized_series(m.get("series") or []), "language": m.get("language"),
        "duration": m.get("duration"),
    }
    candidate_match_score(md, profile, cand)
    return cand



def _audiosilo_asins(recording: dict) -> list[str]:
    out = []
    for value in recording.get("asin") or []:
        if isinstance(value, dict):
            value = value.get("asin")
        if value:
            out.append(str(value))
    return out


def map_audiosilo_exact(work: dict, recording: dict, md: dict, profile: dict) -> dict:
    authors = [str(x.get("name") if isinstance(x, dict) else x) for x in (work.get("authors") or []) if x]
    narrators = [str(x.get("name") if isinstance(x, dict) else x) for x in (recording.get("narrators") or []) if x]
    isbns = [str(x) for x in (recording.get("isbn") or []) if x]
    asins = _audiosilo_asins(recording)
    local_isbn = normalize_identifier(md.get("isbn"))
    local_asin = normalize_identifier(md.get("asin"))
    isbn = next((x for x in isbns if normalize_identifier(x) == local_isbn), None) or (isbns[0] if isbns else None)
    asin = next((x for x in asins if normalize_identifier(x) == local_asin), None) or (asins[0] if asins else None)
    series = []
    for row in work.get("series") or []:
        if isinstance(row, dict) and row.get("name"):
            series.append({"series": str(row["name"]), "sequence": str(row.get("position") or "")})
    first_published = str(work.get("first_published") or "")
    published_year = first_published[:4] if re.match(r"^\\d{4}", first_published) else None
    cand = {
        "provider": "AudioSilo", "providerKey": work.get("id"), "recordingId": recording.get("id"),
        "matchMethod": "exact-identifier", "title": work.get("title"), "subtitle": work.get("subtitle"),
        "authorName": ", ".join(authors) or None, "narratorName": ", ".join(narrators) or None,
        "publisher": recording.get("publisher"), "publishedYear": published_year,
        "description": work.get("description"),
        "coverUrl": _first_nonempty(recording.get("cover_url"), work.get("cover_url")),
        "isbn": isbn, "isbns": isbns, "asin": asin, "asins": asins,
        "genres": work.get("genres") or [], "tags": [], "series": series,
        "language": work.get("language"), "duration": recording.get("runtime_min"),
    }
    candidate_match_score(md, profile, cand)
    return cand


async def audiosilo_exact_lookup(md: dict, profile: dict) -> list[dict]:
    """Resolve a local ASIN/ISBN to one AudioSilo recording before fuzzy title search.

    AudioSilo's lookup endpoint may fall back from a print ISBN to the work's first
    recording. We therefore only call a recording identifier 'exact' when the
    returned recording itself contains the local ISBN/ASIN; candidate_match_score
    computes that distinction from the recording's identifier lists.
    """
    asin = normalize_identifier(md.get("asin"))
    isbn = normalize_identifier(md.get("isbn"))
    if not asin and not isbn:
        return []
    key = hashlib.sha256(f"{asin}|{isbn}|{profile.get('durationSeconds',0)}".encode()).hexdigest()
    cached = cache_get("AudioSilo Exact Lookup", key)
    if cached is not None:
        for c in cached:
            c["cached"] = True
        return cached

    api_base = re.sub(r"/abs/?$", "", settings.audiosilo_url.rstrip("/"))
    params = {"asin": asin} if asin else {"isbn": isbn}
    client = http_client()
    lookup = await client.get(api_base + "/api/v1/lookup", params=params, timeout=30.0)
    if lookup.status_code == 404:
        cache_set("AudioSilo Exact Lookup", key, [])
        return []
    lookup.raise_for_status()
    hit = lookup.json()
    work_card = hit.get("work") or {}
    work_id = work_card.get("id")
    recording_id = hit.get("recording_id")
    if not work_id or not recording_id:
        return []
    full = await client.get(api_base + f"/api/v1/works/{work_id}", timeout=30.0)
    full.raise_for_status()
    work = full.json()

    recording = next((x for x in (work.get("recordings") or []) if str(x.get("id")) == str(recording_id)), None)
    if not recording:
        return []
    cand = map_audiosilo_exact(work, recording, md, profile)
    payload = [cand]
    cache_set("AudioSilo Exact Lookup", key, payload)
    return payload


async def custom_abs_provider_lookup(provider: str, base_url: str, md: dict, profile: dict) -> list[dict]:
    key = provider_cache_key(md, profile)
    cached = cache_get(provider, key)
    if cached is not None:
        for c in cached:
            c["cached"] = True
        return cached

    title = str(md.get("title") or "").strip()
    if not title:
        return []
    params = {
        "mediaType": "book", "query": title, "title": title,
        "author": str(md.get("authorName") or "").strip() or None,
        "isbn": str(md.get("isbn") or "").strip() or None,
    }
    params = {k: v for k, v in params.items() if v is not None}
    r = await http_client().get(base_url.rstrip("/") + "/search", params=params, timeout=30.0)
    r.raise_for_status()
    data = r.json()
    raw = data.get("matches") if isinstance(data, dict) else data
    candidates = [map_custom_match(provider, m, md, profile) for m in (raw or [])[:10] if isinstance(m, dict)]
    candidates.sort(key=lambda x: x["score"], reverse=True)
    cache_set(provider, key, candidates)
    return candidates


async def openlibrary_lookup(md: dict, profile: dict) -> list[dict]:
    provider = "Open Library"
    key = provider_cache_key(md, profile)
    cached = cache_get(provider, key)
    if cached is not None:
        for c in cached:
            c["cached"] = True
        return cached

    headers = {"User-Agent": "AI-Librarian/0.5.0" + (f" ({settings.openlibrary_contact})" if settings.openlibrary_contact else "")}
    params: dict[str, Any] = {"limit": 5, "fields": "key,title,author_name,first_publish_year,isbn,subject,cover_i,language,publisher"}
    isbn = normalize_identifier(md.get("isbn"))
    if isbn:
        params["isbn"] = isbn
    else:
        title = str(md.get("title") or "").strip()
        author = str(md.get("authorName") or "").strip()
        if not title:
            return []
        params["title"] = title
        if author:
            params["author"] = author
    r = await http_client().get("https://openlibrary.org/search.json", params=params, headers=headers, timeout=25.0)
    r.raise_for_status()
    data = r.json()

    candidates = []
    for doc in (data.get("docs") or [])[:5]:
        cand = {
            "provider": provider, "providerKey": doc.get("key"),
            "title": doc.get("title"), "authorName": ", ".join(doc.get("author_name") or []) or None,
            "publishedYear": str(doc.get("first_publish_year")) if doc.get("first_publish_year") else None,
            "isbn": (doc.get("isbn") or [None])[0], "isbns": doc.get("isbn") or [], "asin": None,
            "publisher": (doc.get("publisher") or [None])[0],
            "subjects": (doc.get("subject") or [])[:24], "genres": [], "series": [],
            "coverUrl": f"https://covers.openlibrary.org/b/id/{doc['cover_i']}-L.jpg" if doc.get("cover_i") else None,
            "languageCodes": (doc.get("language") or [])[:5], "duration": None,
        }
        candidate_match_score(md, profile, cand)
        candidates.append(cand)
    candidates.sort(key=lambda x: x["score"], reverse=True)
    cache_set(provider, key, candidates)
    return candidates


async def gather_provider_evidence(md: dict, profile: dict) -> list[dict]:
    """Gather metadata evidence with cache-first, connection-reused, bounded requests.

    Exact AudioSilo recording matches suppress the redundant fuzzy AudioSilo request.
    Independent secondary providers run concurrently.
    """
    evidence: list[dict] = []
    strong_audio_genres = False

    if profile["hasAudio"] and settings.audiosilo_enabled:
        try:
            exact = await audiosilo_exact_lookup(md, profile)
            exact_strong = bool(exact and exact[0].get("identifierExact") and
                                float(exact[0].get("score") or 0) >= settings.provider_min_score)
            results = list(exact)
            if not exact_strong:
                fuzzy = await custom_abs_provider_lookup("AudioSilo", settings.audiosilo_url, md, profile)
                results.extend(fuzzy)
            seen = set()
            unique = []
            for row in results:
                key = (row.get("recordingId"), normalize_identifier(row.get("asin")),
                       normalize_identifier(row.get("isbn")), normalize_text(row.get("title")),
                       normalize_text(row.get("narratorName")))
                if key in seen:
                    continue
                seen.add(key)
                unique.append(row)
            unique.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
            evidence.extend(unique[:3])
            if unique and float(unique[0].get("score") or 0) >= settings.provider_min_score and unique[0].get("genres"):
                strong_audio_genres = True
        except Exception as e:
            evidence.append({"provider": "AudioSilo", "error": f"{type(e).__name__}: {e}"})

    async def _hardcover():
        if not settings.hardcover_enabled:
            return []
        return (await custom_abs_provider_lookup("Hardcover", settings.hardcover_url, md, profile))[:3]

    async def _openlibrary():
        should = settings.openlibrary_enabled and (profile["kind"] in {"ebook", "unknown"} or not strong_audio_genres)
        if not should:
            return []
        return (await openlibrary_lookup(md, profile))[:3]

    tasks = []
    names = []
    if settings.hardcover_enabled:
        tasks.append(_hardcover()); names.append("Hardcover")
    if settings.openlibrary_enabled and (profile["kind"] in {"ebook", "unknown"} or not strong_audio_genres):
        tasks.append(_openlibrary()); names.append("Open Library")
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                evidence.append({"provider": name, "error": f"{type(result).__name__}: {result}"})
            else:
                evidence.extend(result)
    return evidence


def best_candidates_by_provider(evidence: list[dict]) -> list[dict]:
    best: dict[str, dict] = {}
    for e in evidence:
        if e.get("error") or not e.get("provider"):
            continue
        p = e["provider"]
        if p not in best or float(e.get("score") or 0) > float(best[p].get("score") or 0):
            best[p] = e
    return list(best.values())


def apply_provider_bibliography(md: dict, candidate: dict, evidence: list[dict]) -> tuple[dict, list[str], float]:
    new = dict(candidate)
    reasons: list[str] = []
    confidence = 0.0
    best = sorted((e for e in best_candidates_by_provider(evidence) if float(e.get("score") or 0) >= settings.provider_min_score),
                  key=lambda x: float(x.get("score") or 0), reverse=True)
    if not best:
        return new, reasons, confidence
    b = best[0]
    confidence = float(b.get("score") or 0)

    # Provider can safely repair missing bibliographic basics. Existing clean title/author are preserved.
    if not md.get("authorName") and b.get("authorName"):
        new["authorName"] = b["authorName"]
        reasons.append(f"Verified missing author from {b['provider']}")
    if (not md.get("publisher")) and b.get("publisher"):
        new["publisher"] = b["publisher"]
        reasons.append(f"Verified missing publisher from {b['provider']}")
    if (not md.get("publishedYear")) and b.get("publishedYear"):
        new["publishedYear"] = b["publishedYear"]
        reasons.append(f"Verified missing publication year from {b['provider']}")
    if md.get("title") and cleanup_release_title(str(md.get("title"))) != str(md.get("title")) and b.get("title") and similarity(md.get("title"), b.get("title")) >= 0.55:
        new["title"] = b["title"]
        reasons.append(f"Provider confirms cleaned title from {b['provider']}")
    return new, reasons, confidence


# ---------- Genre completeness ----------

def genre_candidates_from_text(text: str) -> set[str]:
    text_n = normalize_text(text)
    canon = {g.casefold(): g for g in canonical_genres()}
    found: set[str] = set()
    for genre, keywords in GENRE_KEYWORDS.items():
        if genre.casefold() not in canon:
            continue
        for kw in keywords:
            if normalize_text(kw) in text_n:
                found.add(canon[genre.casefold()])
                break
    # Compound categories imply their parent qualifiers too, when those are available.
    if "Fantasy Romance" in found:
        if "Fantasy".casefold() in canon: found.add(canon["Fantasy".casefold()])
        if "Romance".casefold() in canon: found.add(canon["Romance".casefold()])
    if "MM Romance" in found and "Romance".casefold() in canon:
        found.add(canon["Romance".casefold()])
    if any(x in found for x in {"Epic Fantasy", "Urban Fantasy", "Dark Fantasy", "LitRPG"}) and "Fantasy".casefold() in canon:
        found.add(canon["Fantasy".casefold()])
    if any(x in found for x in {"Space Opera", "Dystopian"}) and "Science Fiction".casefold() in canon:
        found.add(canon["Science Fiction".casefold()])
    return found


def infer_applicable_genres(md: dict, evidence: list[dict]) -> tuple[list[str], list[dict]]:
    canon = canonical_genres()
    canonical_lookup = {g.casefold(): g for g in canon}
    found: set[str] = set()
    genre_evidence: list[dict] = []

    # Existing metadata is not evidence that the set is complete, but it is retained.
    for g in normalize_genres(md.get("genres") or []):
        if g.casefold() in canonical_lookup:
            found.add(canonical_lookup[g.casefold()])

    local_text = " ".join(str(md.get(k) or "") for k in ("title", "subtitle", "description"))
    local_found = genre_candidates_from_text(local_text)
    found |= local_found
    if local_found:
        genre_evidence.append({"provider": "Local metadata", "genres": sorted(local_found)})

    for e in best_candidates_by_provider(evidence):
        if float(e.get("score") or 0) < max(0.72, settings.provider_min_score - 0.10):
            continue
        labels = list(e.get("genres") or []) + list(e.get("subjects") or []) + list(e.get("tags") or [])
        direct = set()
        for label in labels:
            n = normalize_genres([str(label)])
            for g in n:
                if g.casefold() in canonical_lookup:
                    direct.add(canonical_lookup[g.casefold()])
        inferred = genre_candidates_from_text(" ".join(str(x) for x in labels))
        combined = direct | inferred
        if combined:
            found |= combined
            genre_evidence.append({"provider": e.get("provider"), "score": e.get("score"), "genres": sorted(combined)})

    # Preserve canonical ordering so suggestions are stable between scans.
    ordered = [g for g in canon if g in found]
    return ordered, genre_evidence


# ---------- Series and narrator consensus ----------

def series_consensus(md: dict, item: dict, evidence: list[dict]) -> tuple[list[dict] | None, list[str], list[dict]]:
    current = normalized_series(md.get("series") or [])
    current_by_name = {normalize_text(s["series"]): s for s in current}
    provider_votes: dict[str, dict[str, list[dict]]] = defaultdict(lambda: defaultdict(list))
    series_name_display: dict[str, str] = {}

    for e in best_candidates_by_provider(evidence):
        if float(e.get("score") or 0) < settings.provider_min_score:
            continue
        for s in normalized_series(e.get("series") or []):
            name_key = normalize_text(s["series"])
            if not name_key:
                continue
            series_name_display[name_key] = s["series"]
            if s["sequence"]:
                provider_votes[name_key][s["sequence"]].append({
                    "source": e.get("provider"), "score": e.get("score"),
                    "identifierExact": bool(e.get("identifierExact")),
                })

    local_hint = path_sequence_hint(item)
    consensus_evidence: list[dict] = []
    proposed = [dict(s) for s in current]
    reasons: list[str] = []

    # If series metadata is absent, add a series only when an exact recording/edition match
    # provides it or two independent providers agree on the series name.
    if not current:
        provider_names = defaultdict(list)
        for e in best_candidates_by_provider(evidence):
            if float(e.get("score") or 0) < settings.provider_min_score:
                continue
            for s in normalized_series(e.get("series") or []):
                provider_names[normalize_text(s["series"])].append((e, s))
        for name_key, rows in provider_names.items():
            exact = any(bool(e.get("identifierExact")) for e, _ in rows)
            providers = {e.get("provider") for e, _ in rows}
            if exact or len(providers) >= 2:
                # Choose a sequence only when exact ID or providers agree.
                seq_counter = Counter(s["sequence"] for _, s in rows if s["sequence"])
                seq = ""
                if exact:
                    seq = next((s["sequence"] for e, s in rows if e.get("identifierExact") and s["sequence"]), "")
                elif seq_counter:
                    top_seq, count = seq_counter.most_common(1)[0]
                    if count >= 2:
                        seq = top_seq
                proposed.append({"series": rows[0][1]["series"], "sequence": seq})
                reasons.append("Verified missing series metadata from provider consensus")
                consensus_evidence.append({"type": "series", "series": rows[0][1]["series"], "sequence": seq,
                                           "sources": sorted(str(x) for x in providers if x), "exactIdentifier": exact})

    # Resolve sequence for known series. Missing values need exact provider evidence or two signals.
    for idx, s in enumerate(list(proposed)):
        name_key = normalize_text(s["series"])
        seq_votes = provider_votes.get(name_key, {})
        if local_hint:
            seq_votes = {k: list(v) for k, v in seq_votes.items()}
            seq_votes.setdefault(local_hint, []).append({"source": "Local folder/title structure", "score": 0.90, "identifierExact": False})
        if not seq_votes:
            continue
        ranked = sorted(seq_votes.items(), key=lambda kv: (sum(1 for _ in kv[1]), max(float(x.get("score") or 0) for x in kv[1])), reverse=True)
        best_seq, votes = ranked[0]
        sources = {v["source"] for v in votes}
        exact = any(v.get("identifierExact") for v in votes)
        current_seq = s.get("sequence") or ""

        if not current_seq:
            allowed = exact or len(sources) >= 2
        elif current_seq != best_seq:
            # Correcting an existing sequence is deliberately stricter than filling a blank.
            allowed = (exact and len(sources) >= 2) or len({x for x in sources if x != "Local folder/title structure"}) >= 2
        else:
            allowed = False

        if allowed:
            proposed[idx]["sequence"] = best_seq
            reasons.append(f"Series order consensus: {s['series']} #{best_seq}")
            consensus_evidence.append({"type": "series", "series": s["series"], "sequence": best_seq,
                                       "sources": sorted(sources), "exactIdentifier": exact})

    if proposed != current:
        return proposed, reasons, consensus_evidence
    return None, reasons, consensus_evidence


def narrator_consensus(md: dict, profile: dict, evidence: list[dict]) -> tuple[str | None, list[str], list[dict]]:
    if not profile["hasAudio"]:
        return None, [], []
    current = str(md.get("narratorName") or "").strip()
    candidates: list[dict] = []
    for e in best_candidates_by_provider(evidence):
        narrator = str(e.get("narratorName") or "").strip()
        if not narrator or float(e.get("score") or 0) < settings.provider_min_score:
            continue
        candidates.append(e)
    if not candidates:
        return None, [], []

    groups: dict[str, list[dict]] = defaultdict(list)
    for e in candidates:
        groups[normalize_text(e.get("narratorName"))].append(e)
    best_key, rows = max(groups.items(), key=lambda kv: (len({x.get('provider') for x in kv[1]}), max(float(x.get('score') or 0) for x in kv[1])))
    narrator = str(rows[0].get("narratorName") or "").strip()
    providers = {x.get("provider") for x in rows}
    exact = any(bool(x.get("identifierExact")) for x in rows)
    duration_ok = any((x.get("durationSimilarity") is not None and float(x["durationSimilarity"]) >= 0.96) for x in rows)
    high_recording_match = any(
        x.get("provider") == "AudioSilo"
        and float(x.get("score") or 0) >= 0.94
        and x.get("durationSimilarity") is not None
        and float(x["durationSimilarity"]) >= 0.96
        for x in rows
    )

    evidence_out = [{"type": "narrator", "narrator": narrator, "sources": sorted(str(x) for x in providers if x),
                     "exactIdentifier": exact, "durationVerified": duration_ok}]
    if not current and (exact or len(providers) >= 2 or high_recording_match):
        return narrator, ["Audiobook narrator verified from recording-specific metadata"], evidence_out
    if current and normalize_text(current) != best_key and ((exact and duration_ok) or len(providers) >= 2):
        return narrator, ["Current narrator conflicts with verified audiobook recording"], evidence_out
    return None, [], evidence_out


# ---------- Duplicate detection ----------

def build_duplicate_findings(items: list[dict]) -> dict[str, list[dict]]:
    findings: dict[str, list[dict]] = defaultdict(list)
    infos = []
    for item in items:
        md = metadata_of(item)
        p = media_profile_of(item)
        infos.append({"id": item.get("id"), "title": md.get("title"), "author": md.get("authorName"),
                      "isbn": normalize_identifier(md.get("isbn")), "asin": normalize_identifier(md.get("asin")),
                      "profile": p})

    for id_field in ("asin", "isbn"):
        groups: dict[str, list[dict]] = defaultdict(list)
        for info in infos:
            if info[id_field]:
                groups[info[id_field]].append(info)
        for ident, rows in groups.items():
            if len(rows) <= 1:
                continue
            for row in rows:
                others = [x for x in rows if x["id"] != row["id"]]
                findings[str(row["id"])].append({
                    "type": "duplicate", "strength": "exact", "identifier": id_field.upper(), "value": ident,
                    "message": f"Exact {id_field.upper()} appears on {len(rows)} library items",
                    "otherItemIds": [x["id"] for x in others],
                })

    work_groups: dict[str, list[dict]] = defaultdict(list)
    for info in infos:
        key = normalize_text(info["title"]) + "|" + normalize_text(info["author"])
        if key != "|":
            work_groups[key].append(info)
    for rows in work_groups.values():
        if len(rows) <= 1:
            continue
        for i, a in enumerate(rows):
            for b in rows[i + 1:]:
                pa, pb = a["profile"], b["profile"]
                # Separate ebook-only and audiobook-only copies are editions of the same work, not duplicates.
                if {pa["kind"], pb["kind"]} == {"ebook", "audiobook"}:
                    continue
                if pa["hasAudio"] and pb["hasAudio"]:
                    da, dbb = float(pa.get("durationSeconds") or 0), float(pb.get("durationSeconds") or 0)
                    if da > 0 and dbb > 0:
                        diff = abs(da - dbb)
                        close = diff <= 120 or diff / max(da, dbb) <= 0.02
                        if close:
                            for x, y in ((a, b), (b, a)):
                                findings[str(x["id"])].append({
                                    "type": "duplicate", "strength": "probable",
                                    "message": "Same title/author with nearly identical audiobook duration",
                                    "otherItemIds": [y["id"]], "durationDifferenceSeconds": round(diff, 1),
                                })
    return findings




# ---------- v0.5.2 audiobook + e-book pairing ----------
def _series_signature(md: dict) -> tuple[str, str]:
    series = md.get("series") or []
    if not series:
        return "", ""
    first = series[0] if isinstance(series, list) else series
    if isinstance(first, dict):
        return normalize_text(first.get("name")), normalize_text(first.get("sequence"))
    return normalize_text(first), ""


def _ebook_files_for_item(item: dict) -> list[dict]:
    """Return supported ebook files with paths relative to the library item folder."""
    media = item.get("media") or {}
    found: list[dict] = []
    seen: set[str] = set()
    candidates = []
    if media.get("ebookFile"):
        candidates.append(media.get("ebookFile"))
    candidates.extend(item.get("libraryFiles") or [])
    for entry in candidates:
        meta = (entry or {}).get("metadata") if isinstance(entry, dict) else None
        meta = meta or (entry if isinstance(entry, dict) else {})
        ext = extension_from_value(meta) or extension_from_value(entry)
        if ext not in EBOOK_EXTENSIONS:
            continue
        filename = str(meta.get("filename") or meta.get("name") or "").strip()
        rel = str(meta.get("relPath") or filename).replace("\\", "/").strip("/")
        key = f"{rel}|{ext}".casefold()
        if not rel or key in seen:
            continue
        seen.add(key)
        found.append({"filename": filename or PurePath(rel).name, "relPath": rel,
                      "path": str(meta.get("path") or ""), "ext": ext,
                      "size": int(meta.get("size") or 0)})
    return found


def _storage_snapshot(item: dict) -> dict:
    return {
        "path": str(item.get("path") or ""),
        "relPath": str(item.get("relPath") or "").replace("\\", "/").strip("/"),
        "isFile": bool(item.get("isFile")),
        "folderId": item.get("folderId"),
        "ebookFiles": _ebook_files_for_item(item),
    }


def media_pair_score(audio_item: dict, ebook_item: dict) -> tuple[float, str]:
    am, em = metadata_of(audio_item), metadata_of(ebook_item)
    at, et = normalize_text(am.get("title")), normalize_text(em.get("title"))
    aa, ea = normalize_text(am.get("authorName")), normalize_text(em.get("authorName"))
    if not at or not et:
        return 0.0, "Missing title"
    ts = similarity(at, et)
    aus = similarity(aa, ea) if aa and ea else 0.0
    if ts < 0.84:
        return 0.0, "Title mismatch"
    if aa and ea and aus < 0.68:
        return 0.0, "Author mismatch"

    score = ts * 0.64
    reasons = [f"title {ts:.0%}"]
    if aa and ea:
        score += aus * 0.25
        reasons.append(f"author {aus:.0%}")
    elif ts >= 0.97:
        score += 0.08

    # Identifiers are edition-specific, but an exact overlap is extremely strong evidence.
    identifier_match = False
    for key in ("isbn", "asin"):
        av, ev = normalize_identifier(am.get(key)), normalize_identifier(em.get(key))
        if av and ev and av == ev:
            identifier_match = True
            reasons.append(f"same {key.upper()}")
    if identifier_match:
        score += 0.12

    aser, aseq = _series_signature(am)
    eser, eseq = _series_signature(em)
    if aser and eser and similarity(aser, eser) >= 0.94:
        score += 0.05
        reasons.append("same series")
        if aseq and eseq and aseq == eseq:
            score += 0.025
            reasons.append("same series position")

    ay, ey = str(am.get("publishedYear") or "").strip(), str(em.get("publishedYear") or "").strip()
    if ay and ey and ay == ey:
        score += 0.015
        reasons.append("same year")

    if at == et and aa and ea and aa == ea:
        score = max(score, 0.97)
    elif ts >= 0.97 and aus >= 0.95:
        score = max(score, 0.955)
    return min(score, 0.995), ", ".join(reasons)


def build_media_pair_candidates(items: list[dict]) -> list[dict]:
    audios, ebooks = [], []
    by_title: dict[str, list[dict]] = defaultdict(list)
    by_author: dict[str, list[dict]] = defaultdict(list)
    for item in items:
        profile = media_profile_of(item)
        if profile["kind"] == "audiobook":
            audios.append(item)
            md = metadata_of(item)
            by_title[normalize_text(md.get("title"))].append(item)
            if normalize_text(md.get("authorName")):
                by_author[normalize_text(md.get("authorName"))].append(item)
        elif profile["kind"] == "ebook":
            ebooks.append(item)

    results: list[dict] = []
    threshold = max(0.70, min(float(settings.pair_min_score), 0.99))
    for ebook in ebooks:
        em = metadata_of(ebook)
        title_key, author_key = normalize_text(em.get("title")), normalize_text(em.get("authorName"))
        candidates: dict[str, dict] = {}
        for a in by_title.get(title_key, []):
            candidates[str(a.get("id"))] = a
        for a in by_author.get(author_key, [])[:120]:
            candidates[str(a.get("id"))] = a
        # If author metadata is absent, exact-title candidates are the only safe pool.
        scored = []
        for audio in candidates.values():
            score, reason = media_pair_score(audio, ebook)
            if score >= threshold:
                scored.append((score, reason, audio))
        scored.sort(key=lambda x: x[0], reverse=True)
        for score, reason, audio in scored[:3]:
            am = metadata_of(audio)
            key = hashlib.sha1(f"{audio.get('id')}|{ebook.get('id')}".encode()).hexdigest()[:24]
            results.append({
                "pair_key": key,
                "audio_item_id": str(audio.get("id")), "ebook_item_id": str(ebook.get("id")),
                "score": score, "reason": reason,
                "details": {
                    "audio": {"metadata": am, "profile": media_profile_of(audio), "storage": _storage_snapshot(audio)},
                    "ebook": {"metadata": em, "profile": media_profile_of(ebook), "storage": _storage_snapshot(ebook)},
                },
            })
    return results


def sync_media_pairings(library_id: str, scan_id: int, items: list[dict]):
    pairs = build_media_pair_candidates(items)
    now = int(time.time())
    current_keys = {p["pair_key"] for p in pairs}
    with db() as con:
        for p in pairs:
            old = con.execute("SELECT status,paired_at,error FROM media_pairings WHERE pair_key=?", (p["pair_key"],)).fetchone()
            status = old["status"] if old and old["status"] in {"dismissed", "paired", "moved-pending-scan"} else "pending"
            con.execute("""INSERT INTO media_pairings(pair_key,library_id,scan_id,audio_item_id,ebook_item_id,score,reason,details,status,updated_at,paired_at,error)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(pair_key) DO UPDATE SET scan_id=excluded.scan_id,score=excluded.score,
                           reason=excluded.reason,details=excluded.details,status=?,updated_at=excluded.updated_at""",
                        (p["pair_key"], library_id, scan_id, p["audio_item_id"], p["ebook_item_id"], p["score"],
                         p["reason"], json.dumps(p["details"]), status, now,
                         old["paired_at"] if old else None, old["error"] if old else None, status))
        # Stale unresolved suggestions disappear after a successful fresh scan; history remains.
        if current_keys:
            placeholders = ",".join("?" for _ in current_keys)
            con.execute(f"DELETE FROM media_pairings WHERE library_id=? AND status='pending' AND pair_key NOT IN ({placeholders})",
                        (library_id, *current_keys))
        else:
            con.execute("DELETE FROM media_pairings WHERE library_id=? AND status='pending'", (library_id,))


def media_path_mappings() -> dict[str, Path]:
    raw = str(settings.media_path_map or "").strip()
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    out = {}
    if isinstance(data, dict):
        for abs_prefix, local_root in data.items():
            try:
                root = Path(str(local_root)).expanduser().resolve()
                if root.is_dir():
                    out[str(abs_prefix).replace("\\", "/").rstrip("/")] = root
            except Exception:
                pass
    return out


def pairing_storage_enabled() -> bool:
    if media_path_mappings():
        return True
    if not settings.media_library_root:
        return False
    try:
        return Path(settings.media_library_root).expanduser().resolve().is_dir()
    except Exception:
        return False


def _safe_media_path(relative: str, abs_path: str = "") -> Path:
    if not pairing_storage_enabled():
        raise HTTPException(409, "Direct storage access is not configured. Set MEDIA_LIBRARY_ROOT or MEDIA_PATH_MAP and mount the ABS library into AI Librarian.")

    normalized_abs = str(abs_path or "").replace("\\", "/")
    mappings = media_path_mappings()
    if normalized_abs and mappings:
        for prefix, root in sorted(mappings.items(), key=lambda kv: len(kv[0]), reverse=True):
            if normalized_abs == prefix or normalized_abs.startswith(prefix + "/"):
                suffix = normalized_abs[len(prefix):].lstrip("/")
                if ".." in PurePath(suffix).parts:
                    raise HTTPException(400, "Unsafe media path")
                path = (root / suffix).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    raise HTTPException(400, "Path escaped the configured media mapping")
                return path

    if not settings.media_library_root:
        raise HTTPException(409, "This item is not covered by MEDIA_PATH_MAP and MEDIA_LIBRARY_ROOT is not configured")
    root = Path(settings.media_library_root).expanduser().resolve()
    rel = str(relative or "").replace("\\", "/").lstrip("/")
    if not rel or ".." in PurePath(rel).parts:
        raise HTTPException(400, "Unsafe or empty library path")
    path = (root / rel).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        raise HTTPException(400, "Path escaped the configured library root")
    return path


def _sanitize_filename_stem(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", str(value or ""))
    value = re.sub(r"\s{2,}", " ", value).strip(" .")
    return value[:180]


async def merge_media_pair(pair_key: str, filename_stem: str = "") -> dict:
    with db() as con:
        row = con.execute("SELECT * FROM media_pairings WHERE pair_key=?", (pair_key,)).fetchone()
    if not row:
        raise HTTPException(404, "Pairing suggestion not found")
    if row["status"] not in {"pending", "moved-pending-scan"}:
        raise HTTPException(400, f"Pairing is already {row['status']}")
    for protected_item in (str(row["audio_item_id"]), str(row["ebook_item_id"])):
        control = get_book_control(str(row["library_id"]), protected_item)
        if control.get("full_lock") or control.get("complete"):
            raise HTTPException(409, "One of these books is fully locked/complete. Unlock it before moving media files.")
    details = json.loads(row["details"] or "{}")
    audio = details.get("audio") or {}; ebook = details.get("ebook") or {}
    astorage, estorage = audio.get("storage") or {}, ebook.get("storage") or {}
    if astorage.get("isFile"):
        raise HTTPException(409, "The audiobook is a single file at the library root. Put it in its own folder first, then pair the e-book.")
    audio_rel = str(astorage.get("relPath") or "")
    ebook_item_rel = str(estorage.get("relPath") or "")
    files = estorage.get("ebookFiles") or []
    if not audio_rel or not files:
        raise HTTPException(409, "Could not resolve the audiobook folder or e-book file from the last scan")
    dest_dir = _safe_media_path(audio_rel, str(astorage.get("path") or ""))
    if not dest_dir.is_dir():
        raise HTTPException(409, f"Audiobook destination folder is not available in the mounted library: {audio_rel}")

    stem = _sanitize_filename_stem(filename_stem)
    moved: list[tuple[Path, Path]] = []
    try:
        for f in files:
            file_rel = str(f.get("relPath") or "").replace("\\", "/").strip("/")
            if estorage.get("isFile"):
                source_rel = ebook_item_rel
            else:
                source_rel = "/".join(x for x in (ebook_item_rel, file_rel) if x)
            source = _safe_media_path(source_rel, str(f.get("path") or ""))
            if not source.is_file():
                raise HTTPException(409, f"E-book file is missing from the mounted library: {source_rel}")
            ext = source.suffix.lower()
            dest_name = f"{stem}{ext}" if stem else source.name
            dest = (dest_dir / dest_name).resolve()
            try:
                dest.relative_to(dest_dir.resolve())
            except ValueError:
                raise HTTPException(400, "Unsafe destination filename")
            if dest.exists() and dest != source:
                raise HTTPException(409, f"Destination already exists: {dest.name}")
            if source != dest:
                shutil.move(str(source), str(dest))
                moved.append((source, dest))

        # Remove only the source book directory if it became completely empty.
        if not estorage.get("isFile") and ebook_item_rel:
            source_dir = _safe_media_path(ebook_item_rel, str(estorage.get("path") or ""))
            if source_dir.is_dir() and not any(source_dir.iterdir()):
                source_dir.rmdir()

        scan_errors = []
        for item_id in (str(row["audio_item_id"]), str(row["ebook_item_id"])):
            try:
                await abs_post(f"/api/items/{item_id}/scan")
            except Exception as e:
                scan_errors.append(str(e))
        if scan_errors:
            try:
                await abs_post(f"/api/libraries/{row['library_id']}/scan", params={"force": 1})
                scan_errors = []
            except Exception as e:
                scan_errors.append(str(e))

        now = int(time.time())
        status = "paired" if not scan_errors else "moved-pending-scan"
        with db() as con:
            con.execute("UPDATE media_pairings SET status=?,paired_at=?,updated_at=?,error=? WHERE pair_key=?",
                        (status, now, now, " | ".join(scan_errors) if scan_errors else None, pair_key))
        return {"status": status, "moved": [str(d.name) for _, d in moved],
                "message": "E-book moved into the audiobook folder and ABS rescan requested" if not scan_errors else
                           "E-book moved successfully, but ABS rescan needs attention"}
    except Exception:
        # Roll back filesystem moves if the move transaction itself failed before the ABS scan phase.
        for src, dst in reversed(moved):
            try:
                if dst.exists() and not src.exists():
                    src.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(src))
            except Exception:
                pass
        raise

# ---------- v0.4 library health, covers, duplicate queue ----------
def item_has_cover(item: dict) -> bool:
    media = item.get("media") or {}
    return bool(_first_nonempty(media.get("coverPath"), item.get("coverPath"), media.get("cover")))

def cover_candidates_from_evidence(evidence: list[dict]) -> list[dict]:
    out, seen = [], set()
    valid=[x for x in evidence if not x.get("error")]
    valid.sort(key=lambda x: float(x.get("score") or 0), reverse=True)
    for e in valid:
        url=_first_nonempty(e.get("coverUrl"), e.get("cover"))
        if not url or not str(url).startswith(("http://","https://")) or url in seen: continue
        seen.add(url); out.append({"url":str(url),"provider":str(e.get("provider") or "Provider"),"score":round(float(e.get("score") or 0),4),"title":e.get("title")})
    return out[:8]

def compute_quality(md: dict, candidate: dict, profile: dict, evidence: list[dict], item: dict) -> tuple[float, dict]:
    dims={}
    def add(name,score,max_score,status): dims[name]={"score":score,"max":max_score,"status":status}
    title=str(md.get("title") or "").strip(); clean=bool(title) and cleanup_release_title(title)==title
    add("Title",15 if clean else (6 if title else 0),15,"good" if clean else "needs attention")
    add("Author",15 if md.get("authorName") else 0,15,"good" if md.get("authorName") else "missing")
    oldg=normalize_genres(md.get("genres") or []); newg=normalize_genres(candidate.get("genres") or []); oldcf={x.casefold() for x in oldg}
    missing=[g for g in newg if g.casefold() not in oldcf]; broad=len(oldg)==1 and oldg[0].casefold() in {"fiction","nonfiction","non-fiction"}
    if not oldg: gp,gs=0,"missing"
    elif missing: gp,gs=12,f"{len(missing)} applicable genre(s) missing"
    elif broad: gp,gs=8,"too broad"
    else: gp,gs=20,"complete"
    add("Genres",gp,20,gs)
    desc=str(md.get("description") or "").strip(); add("Description",5 if len(desc)>=40 else (2 if desc else 0),5,"good" if len(desc)>=40 else "short/missing")
    ident=bool(md.get("isbn") or md.get("asin")); add("Identifiers",10 if ident else 0,10,"good" if ident else "missing")
    cover=item_has_cover(item); add("Cover",10 if cover else 0,10,"good" if cover else "missing")
    lang=bool(md.get("language")); add("Language",5 if lang else 0,5,"good" if lang else "missing")
    if profile.get("hasAudio"):
        narr=bool(md.get("narratorName")); add("Narrator",15 if narr else 0,15,"good" if narr else "missing")
    provider_series=any(normalized_series(e.get("series") or []) for e in best_candidates_by_provider(evidence)); series=normalized_series(md.get("series") or [])
    if series or provider_series:
        complete=bool(series) and all(x.get("sequence") for x in series); add("Series",10 if complete else (5 if series else 0),10,"good" if complete else "missing/order incomplete")
    possible=sum(d["max"] for d in dims.values()) or 1; earned=sum(d["score"] for d in dims.values())
    return round(earned*100/possible,1),dims

def save_item_health(library_id: str, scan_id: int, item: dict, md: dict, profile: dict, candidate: dict, reasons: list[str], evidence: list[dict]):
    score,dims=compute_quality(md,candidate,profile,evidence,item); covers=cover_candidates_from_evidence(evidence)
    with db() as con:
        con.execute("""INSERT INTO item_health(library_id,item_id,scan_id,title,metadata,media_profile,quality_score,dimensions,issues,cover_candidates,has_cover,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(library_id,item_id) DO UPDATE SET scan_id=excluded.scan_id,title=excluded.title,metadata=excluded.metadata,media_profile=excluded.media_profile,quality_score=excluded.quality_score,dimensions=excluded.dimensions,issues=excluded.issues,cover_candidates=excluded.cover_candidates,has_cover=excluded.has_cover,updated_at=excluded.updated_at""",
        (library_id,str(item.get("id")),scan_id,md.get("title") or "(untitled)",json.dumps(md),json.dumps(profile),score,json.dumps(dims),json.dumps(reasons),json.dumps(covers),1 if item_has_cover(item) else 0,int(time.time())))


def touch_protected_item_health(library_id: str, scan_id: int, item: dict, md: dict, profile: dict):
    """Refresh local ABS facts for a protected item without discarding its previous provider/quality snapshot."""
    item_id=str(item.get("id")); now=int(time.time())
    with db() as con:
        old=con.execute("SELECT item_id FROM item_health WHERE library_id=? AND item_id=?",(library_id,item_id)).fetchone()
        if old:
            con.execute("""UPDATE item_health SET scan_id=?,title=?,metadata=?,media_profile=?,has_cover=?,updated_at=?
                           WHERE library_id=? AND item_id=?""",
                        (scan_id,md.get("title") or "(untitled)",json.dumps(md),json.dumps(profile),1 if item_has_cover(item) else 0,now,library_id,item_id))
            return
    save_item_health(library_id,scan_id,item,md,profile,md,[],[])

def sync_duplicate_pairs(library_id: str, scan_id: int, items: list[dict], findings: dict[str,list[dict]]):
    by_id={str(x.get("id")):x for x in items}; pairs={}; ranks={"exact":3,"probable":2,"possible":1}
    for item_id,rows in findings.items():
        for f in rows:
            for other in f.get("otherItemIds") or []:
                a,b=sorted([str(item_id),str(other)])
                if a==b: continue
                key=hashlib.sha1(f"{library_id}|{a}|{b}".encode()).hexdigest(); prev=pairs.get(key)
                if not prev or ranks.get(f.get("strength"),0)>ranks.get(prev["strength"],0):
                    ia,ib=by_id.get(a,{}),by_id.get(b,{})
                    pairs[key]={"a":a,"b":b,"strength":f.get("strength","possible"),"reason":f.get("message","Possible duplicate"),"details":{"a":{"metadata":metadata_of(ia),"profile":media_profile_of(ia)},"b":{"metadata":metadata_of(ib),"profile":media_profile_of(ib)},"finding":f}}
    now=int(time.time())
    with db() as con:
        for key,pair in pairs.items():
            old=con.execute("SELECT status FROM duplicate_pairs WHERE pair_key=?",(key,)).fetchone(); status=old["status"] if old and old["status"] in {"dismissed","removed-a","removed-b"} else "pending"
            con.execute("""INSERT INTO duplicate_pairs(pair_key,library_id,scan_id,item_a,item_b,strength,reason,details,status,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(pair_key) DO UPDATE SET scan_id=excluded.scan_id,strength=excluded.strength,reason=excluded.reason,details=excluded.details,status=?,updated_at=excluded.updated_at""",
            (key,library_id,scan_id,pair["a"],pair["b"],pair["strength"],pair["reason"],json.dumps(pair["details"]),status,now,status))
        con.execute("DELETE FROM duplicate_pairs WHERE library_id=? AND status='pending' AND scan_id<>?",(library_id,scan_id))

def default_library_id() -> str:
    if settings.library_id: return settings.library_id
    with db() as con: row=con.execute("SELECT library_id FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    return str(row["library_id"]) if row else ""

def person_entity_key(name: str | None) -> str:
    return re.sub(r"[^a-z0-9]","",str(name or "").casefold())

def choose_preferred_name(variants: Counter) -> str:
    return max(variants,key=lambda name:(variants[name],0 if str(name).isupper() else 1,len(str(name))))

# ---------- AI ----------
def _compact_ai_metadata(md: dict) -> dict:
    # Do not send giant descriptions or unrelated ABS/provider objects to the model.
    keep = ("title", "subtitle", "authorName", "narratorName", "genres", "series", "language", "isbn", "asin")
    out = {k: md.get(k) for k in keep if md.get(k) not in (None, "", [], {})}
    description = str(md.get("description") or "").strip()
    if description:
        out["description"] = description[:3000]
    return out


def _ai_cache_material(md: dict, profile: dict, evidence: list[dict], original_md: dict | None = None) -> dict:
    # This exact compact packet is used both for the cache key and the API request.
    compact_evidence = []
    for e in best_candidates_by_provider(evidence)[:4]:
        compact_evidence.append({
            "provider": e.get("provider"), "title": e.get("title"), "authorName": e.get("authorName"),
            "narratorName": e.get("narratorName"), "series": e.get("series") or [],
            "genres": (e.get("genres") or [])[:20], "subjects": (e.get("subjects") or [])[:20],
            "description": str(e.get("description") or "")[:1200], "score": e.get("score"),
        })
    return {
        "prompt_version": "v0.5.5-metadata-audit-1", "provider": settings.ai_provider.casefold(), "model": (settings.ai_model if settings.ai_provider.casefold() == "openai" else settings.ollama_model),
        "genres": canonical_genres(), "metadata": _compact_ai_metadata(md),
        "original_metadata": _compact_ai_metadata(original_md or md),
        "profile": {"kind": profile.get("kind"), "label": profile.get("label")}, "evidence": compact_evidence,
    }


def ai_cache_key(md: dict, profile: dict, evidence: list[dict], original_md: dict | None = None) -> str:
    raw = json.dumps(_ai_cache_material(md, profile, evidence, original_md), sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cached_ai(cache_key: str) -> dict | None:
    cutoff = int(time.time()) - max(1, settings.ai_cache_days) * 86400
    with db() as con:
        row = con.execute("SELECT payload FROM ai_cache WHERE cache_key=? AND created_at>=?", (cache_key, cutoff)).fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def _save_ai_cache(cache_key: str, parsed: dict, usage: dict):
    with db() as con:
        con.execute("""INSERT INTO ai_cache(cache_key,provider,model,created_at,payload,usage) VALUES(?,?,?,?,?,?)
                       ON CONFLICT(cache_key) DO UPDATE SET provider=excluded.provider,model=excluded.model,
                       created_at=excluded.created_at,payload=excluded.payload,usage=excluded.usage""",
                    (cache_key, settings.ai_provider.casefold(), (settings.ai_model if settings.ai_provider.casefold() == "openai" else settings.ollama_model), int(time.time()),
                     json.dumps(parsed), json.dumps(usage)))


def _usage_numbers(data: dict) -> dict[str, int]:
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    completion = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    pdet = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
    cdet = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
    cached = int(pdet.get("cached_tokens") or 0)
    reasoning = int(cdet.get("reasoning_tokens") or 0)
    return {"input": prompt, "cached_input": cached, "output": completion, "reasoning": reasoning}


def estimate_ai_cost(usage: dict[str, int]) -> float:
    cached = min(int(usage.get("cached_input") or 0), int(usage.get("input") or 0))
    uncached = max(0, int(usage.get("input") or 0) - cached)
    output = int(usage.get("output") or 0)
    return (
        uncached * settings.ai_input_price_per_million / 1_000_000
        + cached * settings.ai_cached_input_price_per_million / 1_000_000
        + output * settings.ai_output_price_per_million / 1_000_000
    )


def record_ai_usage(scan_id: int, usage: dict[str, int], cost: float, cache_hit: bool = False):
    with db() as con:
        con.execute("""UPDATE scans SET
            ai_requests=COALESCE(ai_requests,0)+?, ai_cache_hits=COALESCE(ai_cache_hits,0)+?,
            ai_input_tokens=COALESCE(ai_input_tokens,0)+?, ai_cached_input_tokens=COALESCE(ai_cached_input_tokens,0)+?,
            ai_output_tokens=COALESCE(ai_output_tokens,0)+?, ai_reasoning_tokens=COALESCE(ai_reasoning_tokens,0)+?,
            ai_estimated_cost=COALESCE(ai_estimated_cost,0)+?, updated_at=? WHERE id=?""",
            (0 if cache_hit else 1, 1 if cache_hit else 0, int(usage.get("input") or 0),
             int(usage.get("cached_input") or 0), int(usage.get("output") or 0), int(usage.get("reasoning") or 0),
             float(cost), int(time.time()), scan_id))


def ai_scan_cost(scan_id: int) -> float:
    with db() as con:
        row = con.execute("SELECT ai_estimated_cost FROM scans WHERE id=?", (scan_id,)).fetchone()
    return float(row["ai_estimated_cost"] or 0) if row else 0.0


def ai_budget_remaining(scan_id: int) -> float:
    if settings.ai_max_scan_cost_usd <= 0:
        return -1.0
    return max(0.0, settings.ai_max_scan_cost_usd - ai_scan_cost(scan_id))


def needs_ai_assessment(md: dict, candidate: dict, evidence: list[dict], reasons: list[str]) -> bool:
    """Smart mode: local/provider checks run on every book; AI is reserved for ambiguity or suspicious metadata."""
    if settings.ai_scan_mode.casefold() == "deep":
        return True
    if settings.ai_metadata_corrections and metadata_suspicion_reasons(md):
        return True
    genres = normalize_genres(candidate.get("genres") or [])
    if not genres:
        return True
    genre_evidence = [e for e in evidence if e.get("genres") or e.get("subjects") or e.get("type") == "genre"]
    provider_names = {str(e.get("provider") or "") for e in genre_evidence if e.get("provider")}
    # A sparse genre set with little/no external evidence is exactly where AI adds value.
    if len(genres) <= 1 and len(provider_names) < 2 and len(str(md.get("description") or "")) >= 80:
        return True
    # Low-confidence metadata or explicitly broad/missing genre issues get an AI tie-breaker.
    reason_text = " ".join(reasons).casefold()
    if "overly broad" in reason_text or "missing genres" in reason_text:
        return True
    return False


async def ai_assess(scan_id: int, md: dict, profile: dict, evidence: list[dict], force_refresh: bool = False, original_md: dict | None = None) -> tuple[dict | None, list[str], float, list[str]]:
    provider = settings.ai_provider.casefold()
    if provider == "none":
        return None, [], 0.0, []

    system = f"""You are a conservative book metadata librarian. Return ONLY valid JSON.
This item is {profile['label']}.
Audit BOTH genre completeness and obvious metadata-structure mistakes.
Common import errors include an author embedded in the title (for example `Kassie Keegan - Savage Galaxy Rescue` should become authorName=`Kassie Keegan`, title=`Savage Galaxy Rescue`), title/author swaps, duplicated author text in the title, filename extensions, and release-format text in title/subtitle.
Only rearrange title/author when the formatting is strongly suggestive or provider evidence supports it. Never split an ordinary hyphenated title that does not use a spaced separator. If uncertain, leave the fields unchanged and note the suspicion.
If it is e-book only, narrator and audio duration are NOT applicable and must not be invented or flagged.
Never invent ISBN, ASIN, publisher, publication year, narrator, series, or series sequence.
Provider evidence is stronger than inference.
For applicableGenres, return EVERY well-supported genre from this exact vocabulary, not merely the single best genre:
{json.dumps(canonical_genres())}
Do not add a genre from a weak association. Compound genres may coexist with their parent genres.
You may suggest only safe corrections to title, subtitle, authorName, description, language and genres.
Keep notes extremely brief and describe the correction, not hidden reasoning.
Schema:
{{"metadata": object, "applicableGenres": [string], "confidence": number, "notes": [string]}}
"""
    material = _ai_cache_material(md, profile, evidence, original_md)
    user = {"original_metadata": material["original_metadata"], "current_metadata": material["metadata"], "media_profile": material["profile"], "provider_evidence": material["evidence"]}
    cache_key = ai_cache_key(md, profile, evidence, original_md)
    if not force_refresh:
        cached = _cached_ai(cache_key)
        if cached is not None:
            record_ai_usage(scan_id, {"input": 0, "cached_input": 0, "output": 0, "reasoning": 0}, 0.0, cache_hit=True)
            parsed = cached
            return _normalize_ai_result(md, parsed)

    if provider == "openai":
        if settings.local_ai_only:
            raise RuntimeError("Cloud AI is disabled. Set LOCAL_AI_ONLY=false to permit OpenAI.")
        if not settings.ai_api_key:
            return None, [], 0.0, []
        if settings.ai_max_scan_cost_usd > 0 and ai_scan_cost(scan_id) >= settings.ai_max_scan_cost_usd:
            raise AIBudgetExceeded(f"AI scan budget ${settings.ai_max_scan_cost_usd:.2f} reached")
        payload = {
            "model": settings.ai_model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user)}],
            "response_format": {"type": "json_object"},
            "max_completion_tokens": max(100, min(settings.ai_max_completion_tokens, 2000)),
        }
        if settings.ai_reasoning_effort:
            payload["reasoning_effort"] = settings.ai_reasoning_effort
        headers = {"Authorization": f"Bearer {settings.ai_api_key}", "Content-Type": "application/json"}
        r = await http_client().post(settings.ai_base_url.rstrip("/") + "/chat/completions", headers=headers, json=payload, timeout=90.0)
        r.raise_for_status()
        data = r.json()
        parsed = json.loads(data["choices"][0]["message"]["content"])
        usage = _usage_numbers(data)
        cost = estimate_ai_cost(usage)
        record_ai_usage(scan_id, usage, cost, cache_hit=False)
        _save_ai_cache(cache_key, parsed, usage)
    elif provider == "ollama":
        schema = {
            "type": "object",
            "properties": {
                "metadata": {"type": "object"},
                "applicableGenres": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number"},
                "notes": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["metadata", "applicableGenres", "confidence", "notes"],
        }
        payload = {
            "model": settings.ollama_model,
            "stream": False,
            "format": schema,
            "think": bool(settings.ollama_think),
            "keep_alive": settings.ollama_keep_alive,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(user)}],
            "options": {
                "num_predict": max(100, min(settings.ai_max_completion_tokens, 2000)),
                "num_ctx": max(2048, min(settings.ollama_num_ctx, 32768)),
                "temperature": max(0.0, min(settings.ollama_temperature, 2.0)),
            },
        }
        async with OLLAMA_SEMAPHORE:
            r = await http_client().post(settings.ollama_url.rstrip("/") + "/api/chat", json=payload, timeout=300.0)
        r.raise_for_status()
        data = r.json()
        content = (data.get("message") or {}).get("content") or "{}"
        parsed = json.loads(content)
        usage = {"input": int(data.get("prompt_eval_count") or 0), "cached_input": 0,
                 "output": int(data.get("eval_count") or 0), "reasoning": 0}
        record_ai_usage(scan_id, usage, 0.0, cache_hit=False)
        _save_ai_cache(cache_key, parsed, usage)
    else:
        return None, [], 0.0, []
    return _normalize_ai_result(md, parsed)


def _normalize_ai_result(md: dict, parsed: dict) -> tuple[dict | None, list[str], float, list[str]]:
    candidate = parsed.get("metadata") or {}
    safe = dict(md)
    allowed_ai_fields = {"title", "subtitle", "authorName", "description", "language", "genres"}
    for k in allowed_ai_fields:
        if k in candidate:
            safe[k] = candidate[k]
    safe["genres"] = normalize_genres(safe.get("genres") or [])
    applicable = normalize_genres(parsed.get("applicableGenres") or [])
    canon_cf = {g.casefold() for g in canonical_genres()}
    applicable = [g for g in applicable if g.casefold() in canon_cf]
    confidence = max(0.0, min(1.0, float(parsed.get("confidence", 0.0))))
    notes = [str(x) for x in (parsed.get("notes") or [])][:5]
    return safe, applicable, confidence, notes


async def ollama_status() -> dict[str, Any]:
    """Fast local connectivity/model check for the dashboard."""
    result = {
        "enabled": settings.ai_provider.casefold() == "ollama",
        "connected": False,
        "url": settings.ollama_url,
        "model": settings.ollama_model,
        "model_installed": False,
        "models": [],
        "error": None,
    }
    if not result["enabled"]:
        return result
    try:
        r = await http_client().get(settings.ollama_url.rstrip("/") + "/api/tags", timeout=3.0)
        r.raise_for_status()
        data = r.json()
        names = [str(x.get("name") or x.get("model") or "") for x in (data.get("models") or [])]
        names = [x for x in names if x]
        result["connected"] = True
        result["models"] = names
        wanted = settings.ollama_model.casefold()
        result["model_installed"] = any(n.casefold() == wanted for n in names)
    except Exception as e:
        result["error"] = str(e)
    return result


# ---------- Scan state ----------
def create_scan_record(library_id: str, source: str, use_ai: bool) -> int:
    now = int(time.time())
    with db() as con:
        cur = con.execute("""INSERT INTO scans(library_id,started_at,source,status,updated_at,use_ai,ai_mode,progress_message)
                           VALUES(?,?,?,'running',?,?,?,?)""",
                          (library_id, now, source, now, 1 if use_ai else 0, settings.ai_scan_mode, "Loading library items"))
        return int(cur.lastrowid)


def update_scan(scan_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    cols = ",".join(f"{k}=?" for k in fields)
    with db() as con:
        con.execute(f"UPDATE scans SET {cols} WHERE id=?", (*fields.values(), scan_id))


def add_scan_event(scan_id: int, message: str, level: str = "info", item_id: str | None = None, title: str | None = None):
    with db() as con:
        con.execute("INSERT INTO scan_events(scan_id,created_at,level,message,item_id,title) VALUES(?,?,?,?,?,?)",
                    (scan_id, int(time.time()), level, message, item_id, title))
        # Keep event history bounded per scan.
        con.execute("""DELETE FROM scan_events WHERE scan_id=? AND id NOT IN
                       (SELECT id FROM scan_events WHERE scan_id=? ORDER BY id DESC LIMIT 250)""", (scan_id, scan_id))


def scan_to_dict(row: sqlite3.Row | None) -> dict | None:
    if not row:
        return None
    d = dict(row)
    try:
        d["media_stats"] = json.loads(d.get("media_stats") or "{}")
    except Exception:
        d["media_stats"] = {}
    total = int(d.get("item_count") or 0)
    done = int(d.get("processed_count") or 0)
    d["percent"] = round(done * 100 / total, 1) if total else 0
    d["ai_budget"] = float(settings.ai_max_scan_cost_usd)
    d["ai_budget_remaining"] = (-1.0 if settings.ai_max_scan_cost_usd <= 0 else
                                max(0.0, float(settings.ai_max_scan_cost_usd) - float(d.get("ai_estimated_cost") or 0)))
    return d


def scan_status(scan_id: int) -> dict:
    with db() as con:
        row = con.execute("SELECT * FROM scans WHERE id=?", (scan_id,)).fetchone()
        events = con.execute("SELECT * FROM scan_events WHERE scan_id=? ORDER BY id DESC LIMIT 30", (scan_id,)).fetchall()
    if not row:
        raise HTTPException(404, "Scan not found")
    return {"scan": scan_to_dict(row), "events": [dict(x) for x in reversed(events)]}


def selected_changed_fields(old: dict, new: dict) -> list[str]:
    return [k for k in dict.fromkeys([*old.keys(), *new.keys()]) if old.get(k) != new.get(k)]


def save_or_update_suggestion(scan_id: int, item: dict, library_id: str, md: dict, candidate: dict,
                              reasons: list[str], confidence: float, sources: list[str], evidence: list[dict], profile: dict):
    item_id = str(item.get("id"))
    if metadata_is_locked(library_id, item_id):
        return None
    fields = selected_changed_fields(md, candidate)
    with db() as con:
        existing = con.execute("SELECT * FROM suggestions WHERE item_id=? AND status='pending' ORDER BY id DESC LIMIT 1", (item_id,)).fetchone()
        if existing and "+manual" in str(existing["source"] or ""):
            # Do not overwrite an in-progress human edit with a fresh scan.
            return int(existing["id"])
        payload = (
            scan_id, library_id, md.get("title") or "(untitled)", json.dumps(reasons), json.dumps(md),
            json.dumps(candidate), confidence, "+".join(dict.fromkeys(sources)), int(time.time()),
            json.dumps(evidence), json.dumps(profile), json.dumps(fields), item_id,
        )
        if existing:
            con.execute("""UPDATE suggestions SET scan_id=?,library_id=?,title=?,reasons=?,old_metadata=?,new_metadata=?,
                           confidence=?,source=?,created_at=?,evidence=?,media_profile=?,selected_fields=?,error=NULL
                           WHERE item_id=? AND status='pending'""", payload)
            return int(existing["id"])
        cur = con.execute("""INSERT INTO suggestions(scan_id,library_id,title,reasons,old_metadata,new_metadata,
                           confidence,source,status,created_at,evidence,media_profile,selected_fields,item_id)
                           VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?,?,?)""", payload)
        return int(cur.lastrowid)


async def _prepare_scan_item(original_item: dict, semaphore: asyncio.Semaphore, skip_deep: bool = False) -> dict:
    async with semaphore:
        item = original_item
        item_id = str(item.get("id") or "")
        md = metadata_of(item)
        profile = media_profile_of(item)
        title = str(md.get("title") or item.get("title") or "(untitled)")
        if profile["kind"] == "unknown" and item_id and not skip_deep:
            try:
                detailed = await abs_get(f"/api/items/{item_id}", {"expanded": 1})
                if detailed:
                    item = detailed
                    md = metadata_of(item)
                    profile = media_profile_of(item)
                    title = str(md.get("title") or title)
            except Exception:
                pass
        return {"item": item, "item_id": item_id, "md": md, "profile": profile, "title": title}


async def perform_scan(scan_id: int, library_id: str, use_ai: bool = False, source: str = "manual") -> int:
    try:
        add_scan_event(scan_id, "Loading Audiobookshelf library")
        originals = await get_all_items(library_id)
        controls = get_book_controls(library_id)
        protected_count = sum(1 for x in originals if scan_is_exempt(controls.get(str(x.get("id") or ""), DEFAULT_BOOK_CONTROL)))
        update_scan(scan_id, item_count=len(originals), progress_message="Preparing media profiles")

        # Resolve unknown media profiles concurrently, except for books explicitly exempted/fully protected.
        prep_sem = asyncio.Semaphore(max(1, min(settings.scan_concurrency * 2, 12)))
        prepared: list[dict] = []
        prep_chunk = max(8, settings.scan_concurrency * 4)
        for offset in range(0, len(originals), prep_chunk):
            chunk = originals[offset:offset + prep_chunk]
            prepared.extend(await asyncio.gather(*(_prepare_scan_item(x, prep_sem, scan_is_exempt(controls.get(str(x.get("id") or ""), DEFAULT_BOOK_CONTROL))) for x in chunk)))
            update_scan(scan_id, progress_message=f"Prepared {min(offset + len(chunk), len(originals))} of {len(originals)} books")

        items = [x["item"] for x in prepared]
        active_items = [x["item"] for x in prepared if not scan_is_exempt(controls.get(x["item_id"], DEFAULT_BOOK_CONTROL))]
        author_spellings = preferred_author_spellings(active_items)
        duplicates = build_duplicate_findings(active_items)
        sync_duplicate_pairs(library_id, scan_id, active_items, duplicates)
        sync_media_pairings(library_id, scan_id, active_items)
        media_stats = Counter(x["profile"]["kind"] for x in prepared)
        update_scan(scan_id, media_stats=json.dumps(dict(media_stats)), progress_message="Verifying metadata")
        add_scan_event(scan_id, f"Loaded {len(items)} books: {media_stats.get('audiobook',0)} audiobook, {media_stats.get('ebook',0)} e-book, {media_stats.get('hybrid',0)} hybrid; {protected_count} protected/exempt skipped before provider/AI work")

        flagged = 0
        errors = 0
        ai_budget_exhausted = False
        provider_sem = asyncio.Semaphore(max(1, min(settings.scan_concurrency, 8)))

        async def get_evidence(entry: dict) -> list[dict]:
            control = controls.get(entry["item_id"], DEFAULT_BOOK_CONTROL)
            if scan_is_exempt(control):
                return []
            async with provider_sem:
                try:
                    return await gather_provider_evidence(metadata_for_provider_lookup(entry["md"]), entry["profile"])
                except Exception as e:
                    return [{"provider": "Provider layer", "error": f"{type(e).__name__}: {e}"}]

        chunk_size = max(1, min(settings.scan_concurrency, 8))
        processed = 0
        for offset in range(0, len(prepared), chunk_size):
            chunk = prepared[offset:offset + chunk_size]
            names = ", ".join(x["title"][:45] for x in chunk[:2])
            update_scan(scan_id, current_title=chunk[0]["title"] if chunk else None,
                        processed_count=processed, flagged_count=flagged, error_count=errors,
                        progress_message=f"Verifying {len(chunk)} book(s): {names}")
            evidence_rows = await asyncio.gather(*(get_evidence(x) for x in chunk))

            for entry, evidence in zip(chunk, evidence_rows):
                item = entry["item"]
                item_id = entry["item_id"]
                md = entry["md"]
                profile = entry["profile"]
                title = entry["title"]
                control = controls.get(item_id, DEFAULT_BOOK_CONTROL)

                if scan_is_exempt(control):
                    touch_protected_item_health(library_id, scan_id, item, md, profile)
                    processed += 1
                    update_scan(scan_id, processed_count=processed, current_title=title, flagged_count=flagged,
                                error_count=errors, progress_message=f"Skipped protected book {processed} of {len(items)}")
                    continue

                # Metadata-locked books may still use providers for cover discovery, but never create metadata suggestions or invoke AI.
                if control.get("metadata_lock"):
                    provider_errors = [e for e in evidence if e.get("error")]
                    if provider_errors:
                        errors += len(provider_errors)
                    save_item_health(library_id, scan_id, item, md, profile, md, [], evidence)
                    processed += 1
                    update_scan(scan_id, processed_count=processed, current_title=title, flagged_count=flagged,
                                error_count=errors, progress_message=f"Checked protected metadata {processed} of {len(items)}")
                    continue

                reasons, candidate, confidence = rule_suggestion(md, author_spellings, profile)
                sources = ["rules"]
                structure_hint = title_author_structure_hint(md)
                if structure_hint:
                    evidence.append({"provider": "Local structure audit", **structure_hint, "score": structure_hint.get("confidence", 0)})
                provider_errors = [e for e in evidence if e.get("error")]
                if provider_errors:
                    errors += len(provider_errors)
                    if errors <= 15 or errors % 25 == 0:
                        add_scan_event(scan_id, f"{title}: {len(provider_errors)} metadata source error(s)", "error", item_id, title)

                candidate, provider_reasons, provider_conf = apply_provider_bibliography(md, candidate, evidence)
                if provider_reasons:
                    reasons.extend(provider_reasons)
                    confidence = max(confidence, provider_conf)
                    sources.append("providers")

                series_value, series_reasons, series_ev = series_consensus(md, item, evidence)
                if series_value is not None:
                    candidate["series"] = series_value
                if series_reasons:
                    reasons.extend(series_reasons)
                    sources.append("series-consensus")
                    confidence = max(confidence, 0.93)
                    evidence.extend(series_ev)

                narrator_value, narrator_reasons, narrator_ev = narrator_consensus(md, profile, evidence)
                if narrator_value is not None:
                    candidate["narratorName"] = narrator_value
                if narrator_reasons:
                    reasons.extend(narrator_reasons)
                    sources.append("recording-consensus")
                    confidence = max(confidence, 0.94)
                    evidence.extend(narrator_ev)

                inferred_genres, genre_ev = infer_applicable_genres(md, evidence)
                existing_genres = normalize_genres(candidate.get("genres") or [])
                known = {x.casefold() for x in existing_genres}
                for g in inferred_genres:
                    if g.casefold() not in known:
                        existing_genres.append(g)
                        known.add(g.casefold())
                original_genres = normalize_genres(md.get("genres") or [])
                if existing_genres != original_genres:
                    candidate["genres"] = existing_genres
                    original_set = {x.casefold() for x in original_genres}
                    missing = [g for g in existing_genres if g.casefold() not in original_set]
                    if missing:
                        reasons.append("Missing applicable genres: " + ", ".join(missing))
                        sources.append("genre-audit")
                        confidence = max(confidence, 0.86)
                evidence.extend({"type": "genre", **x} for x in genre_ev)

                if use_ai and not ai_budget_exhausted and needs_ai_assessment(md, candidate, evidence, reasons):
                    try:
                        ai_md, ai_genres, ai_conf, ai_notes = await ai_assess(scan_id, candidate, profile, evidence, original_md=md)
                        if ai_md is not None:
                            ai_changed_fields = []
                            for k in ("title", "subtitle", "authorName", "description", "language"):
                                if ai_md.get(k) != candidate.get(k) and ai_md.get(k) is not None:
                                    candidate[k] = ai_md[k]
                                    ai_changed_fields.append(k)
                            if ai_changed_fields:
                                labels = {"title":"title", "subtitle":"subtitle", "authorName":"author", "description":"description", "language":"language"}
                                reasons.append("AI metadata audit suggests correcting: " + ", ".join(labels.get(k, k) for k in ai_changed_fields))
                            merged_genres = normalize_genres(candidate.get("genres") or [])
                            merged_set = {x.casefold() for x in merged_genres}
                            ai_missing = []
                            for g in ai_genres:
                                if g.casefold() not in merged_set:
                                    merged_genres.append(g)
                                    merged_set.add(g.casefold())
                                    ai_missing.append(g)
                            if ai_missing:
                                candidate["genres"] = merged_genres
                                reasons.append("AI genre audit found additional applicable genres: " + ", ".join(ai_missing))
                            if candidate != md or ai_missing:
                                sources.append(settings.ai_provider.casefold())
                                confidence = max(confidence, ai_conf)
                                # An inferred `Author - Title` split with no existing author stays review-only
                                # unless an external provider independently validates the pair.
                                if structure_hint and not structure_hint_verified(structure_hint, evidence):
                                    if not structure_hint.get("authorAlreadyVerified"):
                                        confidence = min(confidence, 0.95)
                            if ai_notes:
                                evidence.append({"provider": f"{settings.ai_provider.title()} AI", "notes": ai_notes,
                                                 "genres": ai_genres, "score": ai_conf})
                    except AIBudgetExceeded as e:
                        ai_budget_exhausted = True
                        add_scan_event(scan_id, f"{e}. AI calls stopped; provider/rule scanning will continue.", "warning")
                    except Exception as e:
                        errors += 1
                        evidence.append({"provider": f"{settings.ai_provider.title()} AI",
                                         "error": f"{type(e).__name__}: {e}"})

                duplicate_rows = duplicates.get(item_id, [])
                if duplicate_rows:
                    for dup in duplicate_rows:
                        reasons.append(dup["message"])
                    evidence.extend(duplicate_rows)
                    sources.append("duplicate-audit")

                reasons = list(dict.fromkeys(reasons))
                save_item_health(library_id, scan_id, item, md, profile, candidate, reasons, evidence)
                if reasons:
                    saved_id = save_or_update_suggestion(scan_id, item, library_id, md, candidate, reasons,
                                                         min(confidence, 0.999), sources, evidence, profile)
                    if saved_id is not None:
                        flagged += 1
                        if flagged <= 12 or flagged % 50 == 0:
                            add_scan_event(scan_id, f"Flagged {title}: {reasons[0]}", "warning", item_id, title)

                processed += 1
                # One progress write per completed book is enough for a 2-second browser poll.
                update_scan(scan_id, processed_count=processed, current_title=title, flagged_count=flagged,
                            error_count=errors, progress_message=f"Checked {processed} of {len(items)} books")

            await asyncio.sleep(0)

        # Prune stale snapshots only after a successful full analysis.
        with db() as con:
            con.execute("DELETE FROM item_health WHERE library_id=? AND scan_id<>?", (library_id, scan_id))

        auto_result = None
        if settings.allow_auto_apply:
            with db() as con:
                rows = con.execute("""SELECT id FROM suggestions
                                      WHERE scan_id=? AND status='pending' AND confidence>=?
                                      AND selected_fields IS NOT NULL AND selected_fields NOT IN ('','[]')""",
                                   (scan_id, settings.auto_apply_threshold)).fetchall()
            auto_ids = [int(r["id"]) for r in rows]
            if auto_ids:
                update_scan(scan_id, progress_message=f"Auto-applying {len(auto_ids)} high-confidence change(s)")
                add_scan_event(scan_id, f"Auto Apply: {len(auto_ids)} suggestions at ≥ {settings.auto_apply_threshold:.0%}", "info")
                auto_result = await apply_many_suggestions(auto_ids, max(1, settings.apply_concurrency))
                add_scan_event(scan_id,
                               f"Auto Apply complete: {auto_result['applied']} applied, {auto_result['skipped']} skipped, {auto_result['failed']} failed",
                               "success" if not auto_result["failed"] else "warning")

        with db() as con:
            pending_review = con.execute("SELECT COUNT(*) c FROM suggestions WHERE scan_id=? AND status='pending'", (scan_id,)).fetchone()["c"]
        update_scan(scan_id, status="completed", finished_at=int(time.time()), current_title=None,
                    processed_count=len(items), flagged_count=int(pending_review), error_count=errors,
                    progress_message=f"Complete — {pending_review} books need review")
        with db() as con:
            airow = con.execute("SELECT ai_requests,ai_cache_hits,ai_estimated_cost FROM scans WHERE id=?", (scan_id,)).fetchone()
        ai_suffix = ""
        if airow and use_ai:
            ai_suffix = f", {int(airow['ai_requests'] or 0)} AI calls, {int(airow['ai_cache_hits'] or 0)} cache hits, ~${float(airow['ai_estimated_cost'] or 0):.4f}"
        add_scan_event(scan_id, f"Scan complete: {len(items)} checked, {pending_review} pending review, {errors} provider/AI errors{ai_suffix}", "success")
        return scan_id
    except Exception as e:
        update_scan(scan_id, status="failed", finished_at=int(time.time()), progress_message=f"Scan failed: {e}")
        add_scan_event(scan_id, f"Scan failed: {type(e).__name__}: {e}", "error")
        raise


def _task_done(scan_id: int, task: asyncio.Task):
    SCAN_TASKS.pop(scan_id, None)
    try:
        task.result()
    except Exception as e:
        print(f"Scan {scan_id} failed: {e}", flush=True)


def start_scan(library_id: str, use_ai: bool, source: str = "manual") -> int:
    with db() as con:
        existing = con.execute("SELECT id FROM scans WHERE library_id=? AND status='running' ORDER BY id DESC LIMIT 1", (library_id,)).fetchone()
        any_ai = con.execute("SELECT id FROM scans WHERE status='running' AND use_ai=1 ORDER BY id DESC LIMIT 1").fetchone() if use_ai else None
    if existing:
        return int(existing["id"])
    # Cost guardrail: never allow overlapping AI-enabled scans across libraries.
    if any_ai:
        return int(any_ai["id"])
    scan_id = create_scan_record(library_id, source, use_ai)
    task = asyncio.create_task(perform_scan(scan_id, library_id, use_ai, source))
    SCAN_TASKS[scan_id] = task
    task.add_done_callback(lambda t, sid=scan_id: _task_done(sid, t))
    return scan_id


async def scheduled_scan_loop():
    await asyncio.sleep(30)
    while True:
        try:
            scan_id = start_scan(settings.library_id, settings.scheduled_use_ai, source="scheduled")
            task = SCAN_TASKS.get(scan_id)
            if task:
                await task
        except Exception as e:
            print(f"Scheduled scan failed: {e}", flush=True)
        await asyncio.sleep(max(1, settings.scheduled_scan_hours) * 3600)


async def restart_scheduled_scan_task() -> None:
    global SCHEDULED_SCAN_TASK
    old = SCHEDULED_SCAN_TASK
    SCHEDULED_SCAN_TASK = None
    if old is not None and not old.done():
        old.cancel()
        try:
            await old
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    if settings.scheduled_scans_enabled and settings.library_id:
        SCHEDULED_SCAN_TASK = asyncio.create_task(scheduled_scan_loop())


# ---------- UI helpers ----------
def parse_suggestion(row: sqlite3.Row) -> dict:
    d = dict(row)
    for src, dst, default in (
        ("reasons", "reasons_list", []), ("old_metadata", "old", {}),
        ("new_metadata", "new", {}), ("evidence", "evidence_list", []),
        ("media_profile", "profile", {}), ("selected_fields", "selected_fields_list", []),
    ):
        try:
            d[dst] = json.loads(d.get(src) or json.dumps(default))
        except Exception:
            d[dst] = default
    return d


def wants_json(request: Request) -> bool:
    return request.headers.get("x-requested-with") == "fetch" or "application/json" in request.headers.get("accept", "")


def dashboard_stats() -> dict:
    lib=default_library_id()
    with db() as con:
        pending=con.execute("SELECT COUNT(*) c FROM suggestions WHERE status='pending'").fetchone()["c"]
        applied=con.execute("SELECT COUNT(*) c FROM suggestions WHERE status='applied'").fetchone()["c"]
        avg_conf=con.execute("SELECT AVG(confidence) a FROM suggestions WHERE status='pending'").fetchone()["a"] or 0
        latest=con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
        running=con.execute("SELECT * FROM scans WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        q=con.execute("SELECT AVG(quality_score) avg,COUNT(*) total,SUM(CASE WHEN quality_score>=90 THEN 1 ELSE 0 END) excellent,SUM(CASE WHEN quality_score>=75 AND quality_score<90 THEN 1 ELSE 0 END) good,SUM(CASE WHEN quality_score>=50 AND quality_score<75 THEN 1 ELSE 0 END) fair,SUM(CASE WHEN quality_score<50 THEN 1 ELSE 0 END) poor FROM item_health WHERE library_id=?",(lib,)).fetchone() if lib else None
        rows=con.execute("SELECT dimensions FROM item_health WHERE library_id=?",(lib,)).fetchall() if lib else []
        dup=con.execute("SELECT COUNT(*) c FROM duplicate_pairs WHERE library_id=? AND status='pending'",(lib,)).fetchone()["c"] if lib else 0
        cover_updated=con.execute("SELECT COUNT(DISTINCT item_id) c FROM cover_updates WHERE library_id=? AND status='updated'",(lib,)).fetchone()["c"] if lib else 0
        locked_count=con.execute("SELECT COUNT(*) c FROM book_controls WHERE library_id=? AND (full_lock=1 OR metadata_lock=1 OR cover_lock=1 OR scan_exempt=1 OR complete=1)",(lib,)).fetchone()["c"] if lib else 0
        complete_count=con.execute("SELECT COUNT(*) c FROM book_controls WHERE library_id=? AND complete=1",(lib,)).fetchone()["c"] if lib else 0
        ignored_cover_count=con.execute("SELECT COUNT(*) c FROM cover_ignores WHERE library_id=?",(lib,)).fetchone()["c"] if lib else 0
        pairing_pending=con.execute("SELECT COUNT(*) c FROM media_pairings WHERE library_id=? AND status='pending'",(lib,)).fetchone()["c"] if lib else 0
        hybrid_count=con.execute("SELECT COUNT(*) c FROM item_health WHERE library_id=? AND json_extract(media_profile,'$.kind')='hybrid'",(lib,)).fetchone()["c"] if lib else 0
    acc=defaultdict(lambda:[0.0,0.0])
    for r in rows:
        try: dims=json.loads(r["dimensions"] or "{}")
        except Exception: continue
        for name,d in dims.items(): acc[name][0]+=float(d.get("score") or 0); acc[name][1]+=float(d.get("max") or 0)
    quality={"avg":round(float(q["avg"] or 0),1) if q else 0,"total":int(q["total"] or 0) if q else 0,"excellent":int(q["excellent"] or 0) if q else 0,"good":int(q["good"] or 0) if q else 0,"fair":int(q["fair"] or 0) if q else 0,"poor":int(q["poor"] or 0) if q else 0,"dimensions":{k:round(v[0]*100/v[1],1) if v[1] else 0 for k,v in acc.items()},"library_id":lib}
    cover_pending=len(pending_cover_rows(lib)) if lib else 0
    return {"pending":pending,"applied":applied,"avg_conf":avg_conf,"latest":scan_to_dict(latest),"running":scan_to_dict(running),"quality":quality,"duplicate_count":dup,"cover_pending":int(cover_pending),"cover_updated":int(cover_updated),"pairing_pending":int(pairing_pending),"hybrid_count":int(hybrid_count),"locked_count":int(locked_count),"complete_count":int(complete_count),"ignored_cover_count":int(ignored_cover_count)}



# ---------- v0.5.6 protection / lock UI ----------
@app.get("/locks", response_class=HTMLResponse)
def locks_page(request: Request, library_id: str = "", page: int = 1, q: str = ""):
    lib=library_id or default_library_id(); page=max(1,page); per_page=50; q=str(q or "").strip()
    with db() as con:
        total=con.execute("""SELECT COUNT(*) c FROM book_controls WHERE library_id=?
                             AND (full_lock=1 OR metadata_lock=1 OR cover_lock=1 OR scan_exempt=1 OR complete=1)""",(lib,)).fetchone()["c"] if lib else 0
        complete_total=con.execute("SELECT COUNT(*) c FROM book_controls WHERE library_id=? AND complete=1",(lib,)).fetchone()["c"] if lib else 0
        pages=max(1,(int(total)+per_page-1)//per_page); page=min(page,pages)
        rows=con.execute("""SELECT b.*,h.metadata,h.media_profile,h.quality_score FROM book_controls b
                            LEFT JOIN item_health h ON h.library_id=b.library_id AND h.item_id=b.item_id
                            WHERE b.library_id=? AND (b.full_lock=1 OR b.metadata_lock=1 OR b.cover_lock=1 OR b.scan_exempt=1 OR b.complete=1)
                            ORDER BY b.complete DESC,b.updated_at DESC,b.title LIMIT ? OFFSET ?""",
                         (lib,per_page,(page-1)*per_page)).fetchall() if lib else []
        search_rows=[]
        if lib and q:
            like=f"%{q}%"
            search_rows=con.execute("""SELECT h.* FROM item_health h WHERE h.library_id=? AND (h.title LIKE ? OR h.metadata LIKE ?)
                                       ORDER BY h.title LIMIT 50""",(lib,like,like)).fetchall()
    items=[]
    for r in rows:
        d=dict(r)
        try:d["metadata_obj"]=json.loads(d.get("metadata") or "{}")
        except Exception:d["metadata_obj"]={}
        try:d["profile_obj"]=json.loads(d.get("media_profile") or "{}")
        except Exception:d["profile_obj"]={}
        d["updated_label"]=time.strftime("%Y-%m-%d %H:%M",time.localtime(int(d.get("updated_at") or 0)))
        items.append(d)
    search_results=[]
    for r in search_rows:
        d=dict(r)
        try:d["metadata_obj"]=json.loads(d.get("metadata") or "{}")
        except Exception:d["metadata_obj"]={}
        try:d["profile_obj"]=json.loads(d.get("media_profile") or "{}")
        except Exception:d["profile_obj"]={}
        d["control"]=get_book_control(lib,str(d["item_id"])); search_results.append(d)
    return templates.TemplateResponse("locks.html",{"request":request,"items":items,"search_results":search_results,"q":q,"library_id":lib,"page":page,
                                                     "pages":pages,"total":int(total),"complete_total":int(complete_total)})


@app.post("/books/{item_id}/control")
def update_book_control_route(request: Request, item_id: str, library_id: str = Form(""), title: str = Form(""),
                              action: str = Form(...), reason: str = Form("")):
    lib=library_id or default_library_id()
    if not lib: raise HTTPException(400,"No library selected")
    control=set_book_control(lib,item_id,title,action,reason)
    labels={"full-lock":"Book fully locked","metadata-lock":"Metadata locked","cover-lock":"Cover locked",
            "scan-exempt":"Book excluded from automatic scans","mark-complete":"Book marked complete and fully protected",
            "unlock":"All book protections removed","unlock-metadata":"Metadata lock removed","unlock-cover":"Cover lock removed",
            "unlock-scan":"Scan exemption removed","unmark-complete":"Complete flag removed"}
    if wants_json(request): return {"ok":True,"message":labels.get(action,"Protection updated"),"control":control}
    return RedirectResponse(f"/locks?library_id={lib}",status_code=303)


@app.get("/covers/ignored", response_class=HTMLResponse)
def ignored_covers_page(request: Request, library_id: str = "", page: int = 1):
    lib=library_id or default_library_id(); page=max(1,page); per_page=max(12,min(settings.cover_page_size,48))
    with db() as con:
        total=con.execute("SELECT COUNT(*) c FROM cover_ignores WHERE library_id=?",(lib,)).fetchone()["c"] if lib else 0
        locked_total=con.execute("SELECT COUNT(*) c FROM book_controls WHERE library_id=? AND (cover_lock=1 OR full_lock=1 OR complete=1)",(lib,)).fetchone()["c"] if lib else 0
        pages=max(1,(int(total)+per_page-1)//per_page); page=min(page,pages)
        rows=con.execute("""SELECT i.*,h.title,h.metadata FROM cover_ignores i
                            LEFT JOIN item_health h ON h.library_id=i.library_id AND h.item_id=i.item_id
                            WHERE i.library_id=? ORDER BY i.ignored_at DESC LIMIT ? OFFSET ?""",
                         (lib,per_page,(page-1)*per_page)).fetchall() if lib else []
        locked_rows=con.execute("""SELECT b.*,h.metadata FROM book_controls b LEFT JOIN item_health h
                                  ON h.library_id=b.library_id AND h.item_id=b.item_id
                                  WHERE b.library_id=? AND (b.cover_lock=1 OR b.full_lock=1 OR b.complete=1)
                                  ORDER BY b.updated_at DESC LIMIT 100""",(lib,)).fetchall() if lib else []
    items=[]
    for r in rows:
        d=dict(r); d["ignored_label"]=time.strftime("%Y-%m-%d %H:%M",time.localtime(int(d.get("ignored_at") or 0)))
        try:d["metadata_obj"]=json.loads(d.get("metadata") or "{}")
        except Exception:d["metadata_obj"]={}
        items.append(d)
    locked=[]
    for r in locked_rows:
        d=dict(r)
        try:d["metadata_obj"]=json.loads(d.get("metadata") or "{}")
        except Exception:d["metadata_obj"]={}
        locked.append(d)
    return templates.TemplateResponse("covers_ignored.html",{"request":request,"items":items,"locked":locked,"library_id":lib,
                                                              "page":page,"pages":pages,"total":int(total),"locked_total":int(locked_total),
                                                              "pending_total":len(pending_cover_rows(lib)) if lib else 0})


@app.post("/covers/{item_id}/ignore")
def ignore_cover_route(request: Request, item_id: str, cover_url: str = Form(...), library_id: str = Form(""),
                       provider: str = Form(""), reason: str = Form("Wrong cover")):
    lib=library_id or default_library_id(); ignore_cover_candidate(lib,item_id,cover_url,provider,reason)
    if wants_json(request): return {"ok":True,"message":"Cover suggestion ignored"}
    return RedirectResponse(f"/covers?library_id={lib}",status_code=303)


@app.post("/covers/{item_id}/restore-ignore")
def restore_cover_ignore(request: Request, item_id: str, cover_url: str = Form(...), library_id: str = Form("")):
    lib=library_id or default_library_id()
    with db() as con:
        con.execute("DELETE FROM cover_ignores WHERE library_id=? AND item_id=? AND cover_url=?",(lib,str(item_id),cover_url))
    if wants_json(request): return {"ok":True,"message":"Cover suggestion restored to review"}
    return RedirectResponse(f"/covers/ignored?library_id={lib}",status_code=303)


@app.post("/covers/{item_id}/keep-current")
def keep_current_cover(request: Request, item_id: str, library_id: str = Form(""), title: str = Form(""),
                       reason: str = Form("Prefer current cover")):
    lib=library_id or default_library_id(); control=set_book_control(lib,item_id,title,"cover-lock",reason)
    if wants_json(request): return {"ok":True,"message":"Current cover kept; future cover changes are locked","control":control}
    return RedirectResponse(f"/covers?library_id={lib}",status_code=303)

# ---------- Web UI ----------
@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    error, libraries = None, []
    try:
        data = await abs_get("/api/libraries")
        libraries = [x for x in data.get("libraries", []) if x.get("mediaType") == "book"]
    except Exception as e:
        error = str(e)
    stats = dashboard_stats()
    history = []
    with db() as con:
        history = [scan_to_dict(x) for x in con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 8").fetchall()]
    local_ai = await ollama_status() if settings.ai_provider.casefold() == "ollama" else {"enabled": False}
    return templates.TemplateResponse("index.html", {
        "request": request, "libraries": libraries, "error": error, "settings": settings,
        "local_ai": local_ai, "history": history, **stats,
    })


@app.post("/scan")
async def scan(request: Request, library_id: str = Form(...), use_ai: str | None = Form(None)):
    if use_ai and settings.ai_provider.casefold() == "ollama":
        st = await ollama_status()
        if not st.get("connected"):
            msg = f"Ollama is not reachable at {settings.ollama_url}. Start Ollama and make it reachable from Docker."
            if wants_json(request): return JSONResponse({"ok": False, "message": msg}, status_code=400)
            raise HTTPException(400, msg)
        if not st.get("model_installed"):
            msg = f"Ollama model {settings.ollama_model} is not installed. Run: ollama pull {settings.ollama_model}"
            if wants_json(request): return JSONResponse({"ok": False, "message": msg}, status_code=400)
            raise HTTPException(400, msg)
    scan_id = start_scan(library_id, bool(use_ai), source="manual")
    if wants_json(request):
        return JSONResponse({"ok": True, "scan_id": scan_id, "status": scan_status(scan_id)})
    return RedirectResponse(f"/?scan={scan_id}", status_code=303)


@app.get("/api/scans/{scan_id}")
def api_scan_status(scan_id: int):
    return scan_status(scan_id)


@app.get("/api/scans/latest")
def api_latest_scan():
    with db() as con:
        row = con.execute("SELECT * FROM scans WHERE status='running' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            row = con.execute("SELECT * FROM scans ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return {"scan": None, "events": []}
    return scan_status(int(row["id"]))


@app.get("/api/dashboard-stats")
def api_dashboard_stats():
    return dashboard_stats()


@app.get("/suggestions", response_class=HTMLResponse)
def suggestions(request: Request, status: str = "pending", page: int = 1):
    per_page = max(20, min(settings.review_page_size, 100))
    page = max(1, page)
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM suggestions WHERE status=?", (status,)).fetchone()["c"]
        pages = max(1, (int(total) + per_page - 1) // per_page)
        page = min(page, pages)
        rows = con.execute("""SELECT id,item_id,library_id,title,reasons,confidence,source,status,error,media_profile,selected_fields,created_at
                              FROM suggestions WHERE status=? ORDER BY confidence DESC,id DESC LIMIT ? OFFSET ?""",
                           (status, per_page, (page - 1) * per_page)).fetchall()
    # Load protection state once per library so rendering the review queue does not
    # open a new SQLite connection for every suggestion card.
    control_maps = {}
    for lib in {str(r["library_id"]) for r in rows}:
        control_maps[lib] = get_book_controls(lib)
    parsed = []
    for row in rows:
        d = dict(row)
        try: d["reasons_list"] = json.loads(d.get("reasons") or "[]")
        except Exception: d["reasons_list"] = []
        try: d["profile"] = json.loads(d.get("media_profile") or "{}")
        except Exception: d["profile"] = {}
        try: d["selected_fields_list"] = json.loads(d.get("selected_fields") or "[]")
        except Exception: d["selected_fields_list"] = []
        d["applicable"] = bool(d["selected_fields_list"])
        d["control"] = control_maps.get(str(d["library_id"]), {}).get(str(d["item_id"]), dict(DEFAULT_BOOK_CONTROL))
        parsed.append(d)
    return templates.TemplateResponse("suggestions.html", {"request": request, "rows": parsed, "status": status,
                                                             "page": page, "pages": pages, "total": int(total),
                                                             "per_page": per_page, "settings": settings})


@app.get("/suggestions/{sid}", response_class=HTMLResponse)
def suggestion_detail(request: Request, sid: int):
    with db() as con:
        row = con.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    parsed = parse_suggestion(row)
    control = get_book_control(str(row["library_id"]), str(row["item_id"]))
    return templates.TemplateResponse("detail.html", {"request": request, "s": parsed, "control": control})


@app.post("/suggestions/{sid}/edit")
async def edit_suggestion(
    request: Request, sid: int,
    title: str = Form(""), subtitle: str = Form(""), authorName: str = Form(""), narratorName: str = Form(""),
    publisher: str = Form(""), publishedYear: str = Form(""), description: str = Form(""),
    genres: str = Form(""), series_text: str = Form(""), language: str = Form(""),
    isbn: str = Form(""), asin: str = Form(""), apply_fields: list[str] = Form(default=[]),
):
    with db() as con:
        row = con.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    if not row or row["status"] != "pending":
        raise HTTPException(404 if not row else 400, "Suggestion is not editable")
    if metadata_is_locked(str(row["library_id"]), str(row["item_id"])):
        raise HTTPException(409, "Book metadata is locked")
    old = json.loads(row["old_metadata"])
    new = json.loads(row["new_metadata"])
    values = {
        "title": title.strip() or None, "subtitle": subtitle.strip() or None,
        "authorName": authorName.strip() or None, "narratorName": narratorName.strip() or None,
        "publisher": publisher.strip() or None, "publishedYear": publishedYear.strip() or None,
        "description": description.strip() or None, "language": language.strip() or None,
        "isbn": isbn.strip() or None, "asin": asin.strip() or None,
        "genres": normalize_genres([x.strip() for x in re.split(r"[\n,;]+", genres) if x.strip()]),
    }
    series = []
    for line in series_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, seq = [x.strip() for x in line.split("|", 1)]
        else:
            name, seq = line, ""
        if name:
            series.append({"series": name, "sequence": seq})
    values["series"] = series
    for k in old.keys():
        if k in values:
            new[k] = values[k]
    selected = [k for k in apply_fields if k in old]
    with db() as con:
        source = str(row["source"] or "rules")
        if "+manual" not in source:
            source += "+manual"
        con.execute("UPDATE suggestions SET new_metadata=?,selected_fields=?,source=?,error=NULL WHERE id=?",
                    (json.dumps(new), json.dumps(selected), source, sid))
    if wants_json(request):
        return {"ok": True, "message": "Edits saved", "selected_fields": selected}
    return RedirectResponse(f"/suggestions/{sid}", status_code=303)


@app.post("/suggestions/{sid}/ignore")
def ignore(request: Request, sid: int):
    with db() as con:
        con.execute("UPDATE suggestions SET status='ignored' WHERE id=? AND status='pending'", (sid,))
    if wants_json(request):
        return {"ok": True, "status": "ignored", "id": sid}
    return RedirectResponse("/suggestions", status_code=303)


async def apply_suggestion(sid: int, strict: bool = True) -> dict:
    with db() as con:
        row = con.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    if not row:
        if strict:
            raise HTTPException(404, "Suggestion not found")
        return {"id": sid, "status": "skipped", "reason": "Suggestion not found"}
    if row["status"] != "pending":
        return {"id": sid, "status": "skipped", "reason": f"Already {row['status']}"}
    if metadata_is_locked(str(row["library_id"]), str(row["item_id"])):
        return {"id": sid, "status": "skipped", "reason": "Book metadata is locked"}
    new_md, old_md = json.loads(row["new_metadata"]), json.loads(row["old_metadata"])
    selected = json.loads(row["selected_fields"] or "[]")
    changed = {k: new_md.get(k) for k in selected if k in old_md and old_md.get(k) != new_md.get(k)}
    if not changed:
        if strict:
            raise HTTPException(400, "No applicable metadata fields are selected for apply")
        return {"id": sid, "status": "skipped", "reason": "No applicable metadata fields selected"}
    await abs_patch(f"/api/items/{row['item_id']}/media", {"metadata": changed})
    with db() as con:
        con.execute("UPDATE suggestions SET status='applied',applied_at=?,error=NULL WHERE id=?", (int(time.time()), sid))
        # Keep the cached metadata view reasonably fresh without waiting for a full rescan.
        h = con.execute("SELECT metadata FROM item_health WHERE library_id=? AND item_id=?",
                        (row["library_id"], row["item_id"])).fetchone()
        if h:
            try:
                cached_md = json.loads(h["metadata"] or "{}")
                cached_md.update(changed)
                con.execute("UPDATE item_health SET metadata=?,updated_at=? WHERE library_id=? AND item_id=?",
                            (json.dumps(cached_md), int(time.time()), row["library_id"], row["item_id"]))
            except Exception:
                pass
    return {"id": sid, "status": "applied", "fields": sorted(changed)}


async def apply_many_suggestions(ids: list[int], concurrency: int | None = None, on_progress=None) -> dict:
    unique_ids = list(dict.fromkeys(int(x) for x in ids))
    result = {"total": len(unique_ids), "completed": 0, "applied": 0, "skipped": 0, "failed": 0, "errors": []}
    sem = asyncio.Semaphore(max(1, min(concurrency or settings.apply_concurrency, 8)))

    async def one(sid: int):
        async with sem:
            try:
                out = await apply_suggestion(sid, strict=False)
                status = out.get("status", "skipped")
                result[status] = result.get(status, 0) + 1
                if status == "skipped" and out.get("reason"):
                    result["errors"].append({"id": sid, "type": "skipped", "error": out["reason"]})
            except Exception as e:
                result["failed"] += 1
                result["errors"].append({"id": sid, "type": "failed", "error": str(e)})
                with db() as con:
                    con.execute("UPDATE suggestions SET error=? WHERE id=?", (str(e), sid))
            finally:
                result["completed"] += 1
                if on_progress:
                    maybe = on_progress(dict(result))
                    if asyncio.iscoroutine(maybe):
                        await maybe

    await asyncio.gather(*(one(sid) for sid in unique_ids))
    return result


def _trim_action_jobs():
    if len(ACTION_JOBS) <= 30:
        return
    done = sorted((j for j in ACTION_JOBS.values() if j.get("status") in {"completed", "failed"}),
                  key=lambda x: x.get("updated_at", 0))
    for row in done[:max(0, len(ACTION_JOBS) - 20)]:
        ACTION_JOBS.pop(row["id"], None)


async def _run_action_job(job_id: str, action: str, ids: list[int]):
    job = ACTION_JOBS[job_id]
    job.update(status="running", updated_at=time.time())
    try:
        if action == "apply":
            def progress(r):
                job.update(r)
                job["updated_at"] = time.time()
            result = await apply_many_suggestions(ids, settings.apply_concurrency, progress)
            job.update(result)
        else:
            unique_ids = list(dict.fromkeys(ids))
            job.update(total=len(unique_ids), completed=0, ignored=0, failed=0, errors=[])
            for offset in range(0, len(unique_ids), 200):
                chunk = unique_ids[offset:offset + 200]
                placeholders = ",".join("?" for _ in chunk)
                with db() as con:
                    cur = con.execute(f"UPDATE suggestions SET status='ignored' WHERE status='pending' AND id IN ({placeholders})", chunk)
                    job["ignored"] += cur.rowcount
                job["completed"] = min(offset + len(chunk), len(unique_ids))
                job["updated_at"] = time.time()
                await asyncio.sleep(0)
        job["status"] = "completed"
        job["updated_at"] = time.time()
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["updated_at"] = time.time()
    finally:
        ACTION_TASKS.pop(job_id, None)
        _trim_action_jobs()


def start_action_job(action: str, ids: list[int]) -> str:
    job_id = uuid.uuid4().hex[:12]
    ACTION_JOBS[job_id] = {
        "id": job_id, "action": action, "status": "queued", "total": len(ids), "completed": 0,
        "applied": 0, "skipped": 0, "ignored": 0, "failed": 0, "errors": [],
        "created_at": time.time(), "updated_at": time.time(),
    }
    task = asyncio.create_task(_run_action_job(job_id, action, ids))
    ACTION_TASKS[job_id] = task
    return job_id


@app.get("/api/actions/{job_id}")
def action_job_status(job_id: str):
    job = ACTION_JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Action job not found")
    return {"ok": True, "job": job}


async def apply_suggestion_and_protect(sid: int, protection_action: str) -> dict:
    """Apply a pending suggestion, then persist the requested protection state."""
    if protection_action not in {"metadata-lock", "mark-complete"}:
        raise HTTPException(400, "Invalid post-apply protection")
    with db() as con:
        row = con.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404, "Suggestion not found")
    out = await apply_suggestion(sid, strict=True)
    if out.get("status") != "applied":
        raise HTTPException(400, out.get("reason") or "Suggestion could not be applied")
    control = set_book_control(str(row["library_id"]), str(row["item_id"]), str(row["title"] or row["item_id"]), protection_action)
    return {"id": sid, "status": "applied", "fields": out.get("fields", []), "control": control}


@app.post("/suggestions/{sid}/apply")
async def apply(request: Request, sid: int):
    try:
        out = await apply_suggestion(sid, strict=True)
    except Exception as e:
        with db() as con:
            con.execute("UPDATE suggestions SET error=? WHERE id=?", (str(e), sid))
        if wants_json(request):
            return JSONResponse({"ok": False, "error": str(e)}, status_code=getattr(e, "status_code", 500))
        raise
    if wants_json(request):
        return {"ok": True, "status": "applied", "id": sid, "fields": out.get("fields", [])}
    return RedirectResponse("/suggestions", status_code=303)


@app.post("/suggestions/{sid}/apply-metadata-lock")
async def apply_metadata_lock(request: Request, sid: int):
    try:
        out = await apply_suggestion_and_protect(sid, "metadata-lock")
    except Exception as e:
        with db() as con:
            con.execute("UPDATE suggestions SET error=? WHERE id=?", (str(e), sid))
        if wants_json(request):
            return JSONResponse({"ok": False, "error": str(e)}, status_code=getattr(e, "status_code", 500))
        raise
    if wants_json(request):
        return {"ok": True, **out, "message": "Suggestion applied and metadata locked"}
    return RedirectResponse("/suggestions?status=applied", status_code=303)


@app.post("/suggestions/{sid}/apply-complete")
async def apply_complete(request: Request, sid: int):
    try:
        out = await apply_suggestion_and_protect(sid, "mark-complete")
    except Exception as e:
        with db() as con:
            con.execute("UPDATE suggestions SET error=? WHERE id=?", (str(e), sid))
        if wants_json(request):
            return JSONResponse({"ok": False, "error": str(e)}, status_code=getattr(e, "status_code", 500))
        raise
    if wants_json(request):
        return {"ok": True, **out, "message": "Suggestion applied and book marked complete"}
    return RedirectResponse("/suggestions?status=applied", status_code=303)


@app.post("/auto-apply")
async def auto_apply_pending(request: Request):
    if not settings.allow_auto_apply:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": "Auto Apply is disabled. Set ALLOW_AUTO_APPLY=true first."}, status_code=403)
        raise HTTPException(403, "Auto Apply is disabled")
    with db() as con:
        rows = con.execute("""SELECT id FROM suggestions WHERE status='pending' AND confidence>=?
                              AND selected_fields IS NOT NULL AND selected_fields NOT IN ('','[]')
                              ORDER BY confidence DESC""", (settings.auto_apply_threshold,)).fetchall()
    ids = [int(r["id"]) for r in rows]
    if not ids:
        if wants_json(request):
            return {"ok": True, "message": "No eligible high-confidence suggestions", "completed": 0}
        return RedirectResponse("/suggestions", status_code=303)
    job_id = start_action_job("apply", ids)
    if wants_json(request):
        return {"ok": True, "job_id": job_id, "action": "apply", "total": len(ids),
                "message": f"Auto Apply started for {len(ids)} suggestion(s) at ≥ {settings.auto_apply_threshold:.0%}"}
    return RedirectResponse(f"/suggestions?job={job_id}", status_code=303)


@app.post("/batch")
async def batch(request: Request):
    form = await request.form()
    action = str(form.get("action") or "")
    if action not in {"apply", "ignore"}:
        raise HTTPException(400, "Invalid batch action")
    ids = []
    for raw in form.getlist("ids"):
        try:
            ids.append(int(raw))
        except (TypeError, ValueError):
            continue
    ids = list(dict.fromkeys(ids))
    if not ids:
        if wants_json(request):
            return JSONResponse({"ok": False, "error": "Select at least one applicable suggestion"}, status_code=400)
        raise HTTPException(400, "Select at least one applicable suggestion")
    job_id = start_action_job(action, ids)
    if wants_json(request):
        return {"ok": True, "job_id": job_id, "action": action, "total": len(ids),
                "message": f"{action.title()} queued for {len(ids)} suggestion(s)"}
    return RedirectResponse(f"/suggestions?job={job_id}", status_code=303)


@app.post("/suggestions/{sid}/revert")
async def revert(request: Request, sid: int):
    with db() as con:
        row = con.execute("SELECT * FROM suggestions WHERE id=?", (sid,)).fetchone()
    if not row:
        raise HTTPException(404)
    if row["status"] != "applied":
        raise HTTPException(400, "Only applied suggestions can be reverted")
    if metadata_is_locked(str(row["library_id"]), str(row["item_id"])):
        raise HTTPException(409, "Book metadata is locked. Unlock metadata before reverting this suggestion.")
    old_md, new_md = json.loads(row["old_metadata"]), json.loads(row["new_metadata"])
    selected = json.loads(row["selected_fields"] or "[]")
    restore = {k: old_md.get(k) for k in selected if old_md.get(k) != new_md.get(k)}
    await abs_patch(f"/api/items/{row['item_id']}/media", {"metadata": restore})
    with db() as con:
        con.execute("UPDATE suggestions SET status='reverted' WHERE id=?", (sid,))
    if wants_json(request):
        return {"ok": True, "status": "reverted", "id": sid}
    return RedirectResponse("/suggestions?status=applied", status_code=303)


# ---------- v0.5.1 cover auto-apply ----------
def pending_cover_auto_targets(library_id: str, threshold: float = 0.96) -> list[dict]:
    """Return one best non-ignored, unlocked cover candidate per pending item."""
    targets=[]
    for row in pending_cover_rows(library_id):
        candidates=row.get("filtered_covers") or []
        valid=[]
        for c in candidates:
            try: score=float(c.get("score") or 0)
            except Exception: score=0.0
            url=str(c.get("url") or "")
            if score >= threshold and url.startswith(("https://","http://")):
                valid.append((score,c))
        if not valid: continue
        score,best=max(valid,key=lambda x:x[0])
        targets.append({"item_id":str(row["item_id"]),"title":str(row.get("title") or row["item_id"]),
                        "cover_url":str(best.get("url")),"provider":str(best.get("provider") or "Provider"),
                        "match_score":score})
    targets.sort(key=lambda x:(-x["match_score"],x["title"].casefold()))
    return targets


async def apply_cover_candidate(item_id: str, library_id: str, cover_url: str,
                                provider: str = "", match_score: float | None = None) -> dict:
    if cover_is_locked(library_id, item_id):
        raise HTTPException(409, "Cover is locked for this book")
    if not cover_url.startswith(("https://", "http://")):
        raise HTTPException(400, "Cover URL must use http or https")
    result = await abs_post(f"/api/items/{item_id}/cover", {"url": cover_url})
    now = int(time.time())
    with db() as con:
        health = con.execute("SELECT title FROM item_health WHERE item_id=? AND library_id=?",
                             (item_id, library_id)).fetchone()
        title = str(health["title"] if health else item_id)
        con.execute("UPDATE item_health SET has_cover=1,updated_at=? WHERE item_id=? AND library_id=?",
                    (now, item_id, library_id))
        con.execute("UPDATE cover_updates SET status='superseded' WHERE library_id=? AND item_id=? AND status='updated'",
                    (library_id, item_id))
        con.execute("""INSERT INTO cover_updates(library_id,item_id,title,cover_url,provider,match_score,applied_at,status)
                       VALUES(?,?,?,?,?,?,?,'updated')""",
                    (library_id, item_id, title, cover_url, provider or None, match_score, now))
    return result


async def _run_cover_auto_job(job_id: str, library_id: str, targets: list[dict], threshold: float):
    job = ACTION_JOBS[job_id]
    job.update(status="running", updated_at=time.time())
    sem = asyncio.Semaphore(max(1, min(settings.apply_concurrency, 8)))

    async def one(target: dict):
        async with sem:
            try:
                await apply_cover_candidate(
                    target["item_id"], library_id, target["cover_url"],
                    target.get("provider", ""), float(target.get("match_score") or 0)
                )
                job["applied"] += 1
            except Exception as e:
                job["failed"] += 1
                job["errors"].append({"item_id": target.get("item_id"), "title": target.get("title"), "error": str(e)})
            finally:
                job["completed"] += 1
                job["updated_at"] = time.time()

    try:
        if targets:
            await asyncio.gather(*(one(t) for t in targets))
        job["status"] = "completed"
        job["updated_at"] = time.time()
    except Exception as e:
        job["status"] = "failed"
        job["error"] = str(e)
        job["updated_at"] = time.time()
    finally:
        ACTION_TASKS.pop(job_id, None)
        _trim_action_jobs()


def start_cover_auto_job(library_id: str, threshold: float = 0.96) -> tuple[str, int]:
    targets = pending_cover_auto_targets(library_id, threshold)
    job_id = uuid.uuid4().hex[:12]
    ACTION_JOBS[job_id] = {
        "id": job_id, "action": "cover_auto", "status": "queued",
        "total": len(targets), "completed": 0, "applied": 0, "skipped": 0,
        "ignored": 0, "failed": 0, "errors": [], "threshold": threshold,
        "created_at": time.time(), "updated_at": time.time(),
    }
    task = asyncio.create_task(_run_cover_auto_job(job_id, library_id, targets, threshold))
    ACTION_TASKS[job_id] = task
    return job_id, len(targets)



# ---------- v0.5.2 audio + e-book pairing UI ----------
@app.get("/pairings", response_class=HTMLResponse)
def pairings_page(request: Request, library_id: str = "", status: str = "pending", page: int = 1):
    lib = library_id or default_library_id(); page = max(1, page); per_page = 30
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM media_pairings WHERE library_id=? AND status=?", (lib, status)).fetchone()["c"] if lib else 0
        pages = max(1, (int(total)+per_page-1)//per_page); page=min(page,pages)
        rows = con.execute("""SELECT * FROM media_pairings WHERE library_id=? AND status=?
                              ORDER BY score DESC,updated_at DESC LIMIT ? OFFSET ?""",
                           (lib,status,per_page,(page-1)*per_page)).fetchall() if lib else []
        hybrid = con.execute("SELECT COUNT(*) c FROM item_health WHERE library_id=? AND json_extract(media_profile,'$.kind')='hybrid'", (lib,)).fetchone()["c"] if lib else 0
    pairs=[]
    for r in rows:
        d=dict(r)
        try:d["details_obj"]=json.loads(d.get("details") or "{}")
        except Exception:d["details_obj"]={}
        pairs.append(d)
    return templates.TemplateResponse("pairings.html", {"request":request,"pairs":pairs,"library_id":lib,
        "status":status,"page":page,"pages":pages,"total":int(total),"hybrid_count":int(hybrid),
        "storage_enabled":pairing_storage_enabled(),"media_library_root":settings.media_library_root})


@app.post("/pairings/{pair_key}/dismiss")
def dismiss_pairing(request: Request, pair_key: str):
    with db() as con:
        con.execute("UPDATE media_pairings SET status='dismissed',updated_at=?,error=NULL WHERE pair_key=?", (int(time.time()),pair_key))
    if wants_json(request): return {"ok":True,"message":"Pairing suggestion dismissed"}
    return RedirectResponse("/pairings",status_code=303)


@app.post("/pairings/{pair_key}/merge")
async def merge_pairing_route(request: Request, pair_key: str, filename_stem: str = Form("")):
    try:
        result = await merge_media_pair(pair_key, filename_stem)
    except Exception as e:
        with db() as con:
            con.execute("UPDATE media_pairings SET error=?,updated_at=? WHERE pair_key=?", (str(e),int(time.time()),pair_key))
        if wants_json(request):
            return JSONResponse({"ok":False,"error":str(e)},status_code=getattr(e,"status_code",500))
        raise
    if wants_json(request): return {"ok":True,**result}
    return RedirectResponse("/pairings",status_code=303)

# ---------- v0.4 management screens ----------
@app.get("/covers", response_class=HTMLResponse)
def covers(request: Request, library_id: str = "", page: int = 1):
    lib = library_id or default_library_id()
    per_page = max(12, min(settings.cover_page_size, 48)); page=max(1,page)
    pending = pending_cover_rows(lib) if lib else []
    total=len(pending); pages=max(1,(total+per_page-1)//per_page); page=min(page,pages)
    rows=pending[(page-1)*per_page:page*per_page]
    with db() as con:
        updated_total=con.execute("SELECT COUNT(DISTINCT item_id) c FROM cover_updates WHERE library_id=? AND status='updated'",(lib,)).fetchone()["c"] if lib else 0
        ignored_total=con.execute("SELECT COUNT(*) c FROM cover_ignores WHERE library_id=?",(lib,)).fetchone()["c"] if lib else 0
        cover_locked=con.execute("SELECT COUNT(*) c FROM book_controls WHERE library_id=? AND (cover_lock=1 OR full_lock=1 OR complete=1)",(lib,)).fetchone()["c"] if lib else 0
    items=[]
    for r in rows:
        d=dict(r); d["metadata_obj"]=json.loads(d.get("metadata") or "{}"); d["profile_obj"]=json.loads(d.get("media_profile") or "{}")
        d["covers"]=(d.get("filtered_covers") or [])[:3]; d["control"]=get_book_control(lib,str(d["item_id"])); items.append(d)
    auto_threshold=.96; auto_count=len(pending_cover_auto_targets(lib,auto_threshold)) if lib else 0
    return templates.TemplateResponse("covers.html", {"request":request,"items":items,"library_id":lib,"page":page,"pages":pages,
        "total":total,"updated_total":int(updated_total),"ignored_total":int(ignored_total),"cover_locked":int(cover_locked),
        "auto_threshold":auto_threshold,"auto_count":auto_count})


@app.get("/covers/updated", response_class=HTMLResponse)
def updated_covers(request: Request, library_id: str = "", page: int = 1):
    lib = library_id or default_library_id()
    per_page = max(12, min(settings.cover_page_size, 48)); page = max(1, page)
    pending_total = len(pending_cover_rows(lib)) if lib else 0
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM cover_updates WHERE library_id=? AND status='updated'", (lib,)).fetchone()["c"] if lib else 0
        pages=max(1,(int(total)+per_page-1)//per_page); page=min(page,pages)
        rows=con.execute("""SELECT u.*,h.metadata,h.media_profile,h.quality_score FROM cover_updates u
                            LEFT JOIN item_health h ON h.library_id=u.library_id AND h.item_id=u.item_id
                            WHERE u.library_id=? AND u.status='updated' ORDER BY u.applied_at DESC,u.id DESC LIMIT ? OFFSET ?""",
                         (lib,per_page,(page-1)*per_page)).fetchall() if lib else []
    items=[]
    for r in rows:
        d=dict(r)
        try:d["metadata_obj"]=json.loads(d.get("metadata") or "{}")
        except Exception:d["metadata_obj"]={}
        d["applied_label"] = time.strftime("%Y-%m-%d %H:%M", time.localtime(int(d.get("applied_at") or 0)))
        items.append(d)
    return templates.TemplateResponse("covers_updated.html", {"request":request,"items":items,"library_id":lib,
        "page":page,"pages":pages,"total":int(total),"pending_total":int(pending_total)})


@app.get("/covers/current/{item_id}")
async def current_cover(item_id: str):
    try:
        content, ctype = await abs_get_bytes(f"/api/items/{item_id}/cover", {"width": 280})
        return Response(content=content, media_type=ctype, headers={"Cache-Control": "private,max-age=60"})
    except Exception:
        raise HTTPException(404, "No current cover")


@app.post("/covers/{item_id}/apply")
async def apply_cover(request: Request, item_id: str, cover_url: str = Form(...), library_id: str = Form(""),
                      provider: str = Form(""), match_score: str = Form("")):
    lib = library_id or default_library_id()
    try:
        score = float(match_score) if match_score else None
    except Exception:
        score = None
    result = await apply_cover_candidate(item_id, lib, cover_url, provider, score)
    if wants_json(request):
        return {"ok": True, "message": "Cover applied and moved to Updated Covers", "result": result, "redirect": "/covers"}
    return RedirectResponse(f"/covers?library_id={lib}", status_code=303)


@app.post("/covers/auto-apply")
async def auto_apply_covers(request: Request, library_id: str = Form(""), threshold: float = Form(0.96)):
    lib = library_id or default_library_id()
    threshold = max(0.0, min(float(threshold), 1.0))
    job_id, count = start_cover_auto_job(lib, threshold)
    message = (f"Updating {count} cover{'s' if count != 1 else ''} at ≥{threshold*100:.0f}% match"
               if count else f"No pending covers are at or above {threshold*100:.0f}% match")
    if wants_json(request):
        return {"ok": True, "job_id": job_id, "count": count, "message": message}
    return RedirectResponse(f"/covers?library_id={lib}", status_code=303)


@app.get("/duplicates", response_class=HTMLResponse)
def duplicates_page(request: Request, library_id: str = "", status: str = "pending", page: int = 1):
    lib = library_id or default_library_id()
    per_page = 30
    page = max(1, page)
    with db() as con:
        total = con.execute("SELECT COUNT(*) c FROM duplicate_pairs WHERE library_id=? AND status=?", (lib, status)).fetchone()["c"] if lib else 0
        pages = max(1, (int(total)+per_page-1)//per_page)
        page = min(page, pages)
        rows = con.execute("""SELECT * FROM duplicate_pairs WHERE library_id=? AND status=?
                              ORDER BY CASE strength WHEN 'exact' THEN 0 ELSE 1 END,updated_at DESC LIMIT ? OFFSET ?""",
                           (lib, status, per_page, (page-1)*per_page)).fetchall() if lib else []
    pairs = []
    for r in rows:
        d = dict(r); d["details_obj"] = json.loads(d["details"] or "{}"); pairs.append(d)
    return templates.TemplateResponse("duplicates.html", {"request": request, "pairs": pairs, "library_id": lib,
                                                            "status": status, "page": page, "pages": pages, "total": int(total)})


@app.post("/duplicates/{pair_key}/dismiss")
def dismiss_duplicate(request: Request, pair_key: str):
    with db() as con:
        con.execute("UPDATE duplicate_pairs SET status='dismissed',updated_at=? WHERE pair_key=?", (int(time.time()), pair_key))
    if wants_json(request):
        return {"ok": True, "message": "Duplicate pair dismissed"}
    return RedirectResponse("/duplicates", status_code=303)


@app.post("/duplicates/{pair_key}/remove")
async def remove_duplicate_item(request: Request, pair_key: str, side: str = Form(...)):
    if side not in {"a", "b"}:
        raise HTTPException(400, "Invalid duplicate side")
    with db() as con:
        row = con.execute("SELECT * FROM duplicate_pairs WHERE pair_key=?", (pair_key,)).fetchone()
    if not row:
        raise HTTPException(404)
    item_id = row["item_a"] if side == "a" else row["item_b"]
    control = get_book_control(str(row["library_id"]), str(item_id))
    if control.get("full_lock") or control.get("complete"):
        raise HTTPException(409, "This book is fully locked/complete and cannot be removed")
    await abs_post("/api/items/batch/delete", {"libraryItemIds": [item_id]})
    with db() as con:
        con.execute("UPDATE duplicate_pairs SET status=?,updated_at=? WHERE pair_key=?",
                    (f"removed-{side}", int(time.time()), pair_key))
        con.execute("DELETE FROM item_health WHERE item_id=? AND library_id=?", (item_id, row["library_id"]))
    if wants_json(request):
        return {"ok": True, "message": "Removed item from Audiobookshelf database only", "item_id": item_id}
    return RedirectResponse("/duplicates", status_code=303)


@app.get("/people", response_class=HTMLResponse)
async def people_page(request: Request, library_id: str = ""):
    lib = library_id or default_library_id()
    author_groups, narrator_groups = [], []
    if lib:
        try:
            data = await abs_get(f"/api/libraries/{lib}/authors")
            buckets = defaultdict(list)
            for a in data.get("authors", []):
                name = str(a.get("name") or "").strip()
                key = person_entity_key(name)
                if name and key:
                    buckets[key].append(a)
            for key, rows in buckets.items():
                names = Counter(str(x.get("name") or "").strip() for x in rows)
                if len(names) > 1:
                    author_groups.append({"key": key, "preferred": choose_preferred_name(names),
                                          "variants": sorted(names.items(), key=lambda x: -x[1]), "rows": rows})
        except Exception:
            pass
        with db() as con:
            hrows = con.execute("SELECT item_id,title,metadata FROM item_health WHERE library_id=?", (lib,)).fetchall()
        buckets = defaultdict(list)
        for r in hrows:
            md = json.loads(r["metadata"] or "{}")
            name = str(md.get("narratorName") or "").strip()
            key = person_entity_key(name)
            if name and key:
                buckets[key].append({"name": name, "item_id": r["item_id"], "title": r["title"]})
        for key, rows in buckets.items():
            names = Counter(x["name"] for x in rows)
            if len(names) > 1:
                narrator_groups.append({"key": key, "preferred": choose_preferred_name(names),
                                        "variants": sorted(names.items(), key=lambda x: -x[1]), "rows": rows})
    return templates.TemplateResponse("people.html", {"request": request, "library_id": lib,
                                                       "author_groups": author_groups, "narrator_groups": narrator_groups})


@app.post("/people/authors/merge")
async def merge_author_variants(request: Request, preferred: str = Form(...), author_ids: list[str] = Form(default=[]),
                                library_id: str = Form(""), variant_name: list[str] = Form(default=[])):
    lib=library_id or default_library_id()
    names={str(x).strip() for x in variant_name if str(x).strip()} | {str(preferred).strip()}
    if lib and names:
        controls=get_book_controls(lib)
        with db() as con:
            rows=con.execute("SELECT item_id,metadata FROM item_health WHERE library_id=?",(lib,)).fetchall()
        protected=[]
        for r in rows:
            c=controls.get(str(r["item_id"]),DEFAULT_BOOK_CONTROL)
            if not (c.get("full_lock") or c.get("metadata_lock") or c.get("complete")): continue
            try: name=str(json.loads(r["metadata"] or "{}").get("authorName") or "").strip()
            except Exception: name=""
            if name in names: protected.append(str(r["item_id"]))
        if protected:
            raise HTTPException(409,f"Author merge would affect {len(protected)} metadata-locked book(s). Unlock them first.")
    completed = 0
    for aid in author_ids:
        try:
            await abs_patch(f"/api/authors/{aid}", {"name": preferred})
            completed += 1
        except Exception as e:
            if wants_json(request):
                return JSONResponse({"ok": False, "error": str(e), "completed": completed}, status_code=500)
            raise
    return {"ok": True, "message": f"Normalized/merged {completed} author entries"}


@app.post("/people/narrators/merge")
async def merge_narrator_variants(request: Request, library_id: str = Form(...), preferred: str = Form(...), variant: list[str] = Form(default=[])):
    if not variant:
        raise HTTPException(400, "No narrator variants selected")
    completed = 0
    with db() as con:
        rows = con.execute("SELECT item_id,metadata FROM item_health WHERE library_id=?", (library_id,)).fetchall()
    variants = set(variant)
    for r in rows:
        md = json.loads(r["metadata"] or "{}")
        current = str(md.get("narratorName") or "").strip()
        if current in variants and current != preferred:
            if metadata_is_locked(library_id, str(r["item_id"])):
                continue
            await abs_patch(f"/api/items/{r['item_id']}/media", {"metadata": {"narratorName": preferred}})
            completed += 1
            md["narratorName"] = preferred
            with db() as con:
                con.execute("UPDATE item_health SET metadata=?,updated_at=? WHERE library_id=? AND item_id=?",
                            (json.dumps(md), int(time.time()), library_id, r["item_id"]))
    return {"ok": True, "message": f"Normalized narrator metadata on {completed} book(s)"}


@app.get("/genres", response_class=HTMLResponse)
def genre_decisions_page(request: Request, library_id: str = ""):
    lib = library_id or default_library_id()
    agg = defaultdict(lambda: {"count": 0, "suggestion_ids": [], "titles": []})
    with db() as con:
        rows = con.execute("SELECT * FROM suggestions WHERE library_id=? AND status='pending'", (lib,)).fetchall() if lib else []
        decisions = {r["genre"]: r["decision"] for r in con.execute("SELECT genre,decision FROM genre_decisions WHERE library_id=?", (lib,)).fetchall()} if lib else {}
    for row in rows:
        old = json.loads(row["old_metadata"])
        new = json.loads(row["new_metadata"])
        oldset = {x.casefold() for x in normalize_genres(old.get("genres") or [])}
        for g in normalize_genres(new.get("genres") or []):
            if g.casefold() not in oldset:
                a = agg[g]
                a["count"] += 1
                a["suggestion_ids"].append(row["id"])
                a["titles"].append(row["title"])
    groups = [{"genre": g, **v, "decision": decisions.get(g)} for g, v in sorted(agg.items(), key=lambda kv: (-kv[1]["count"], kv[0]))]
    return templates.TemplateResponse("genres.html", {"request": request, "library_id": lib, "groups": groups})


@app.post("/genres/bulk")
async def bulk_genre_decision(request: Request, library_id: str = Form(...), genre: str = Form(...), action: str = Form(...)):
    if action not in {"queue", "reject", "apply"}:
        raise HTTPException(400, "Invalid genre action")
    touched = 0
    with db() as con:
        rows = con.execute("SELECT * FROM suggestions WHERE library_id=? AND status='pending'", (library_id,)).fetchall()
    for row in rows:
        old = json.loads(row["old_metadata"])
        new = json.loads(row["new_metadata"])
        oldg = normalize_genres(old.get("genres") or [])
        newg = normalize_genres(new.get("genres") or [])
        if genre.casefold() not in {g.casefold() for g in newg} or genre.casefold() in {g.casefold() for g in oldg}:
            continue
        if metadata_is_locked(library_id, str(row["item_id"])):
            continue
        selected = json.loads(row["selected_fields"] or "[]")
        if action == "queue":
            if "genres" not in selected:
                selected.append("genres")
            with db() as con:
                con.execute("UPDATE suggestions SET selected_fields=? WHERE id=?", (json.dumps(selected), row["id"]))
        elif action == "reject":
            new["genres"] = [g for g in newg if g.casefold() != genre.casefold()]
            if normalize_genres(new.get("genres") or []) == oldg and "genres" in selected:
                selected.remove("genres")
            with db() as con:
                con.execute("UPDATE suggestions SET new_metadata=?,selected_fields=? WHERE id=?",
                            (json.dumps(new), json.dumps(selected), row["id"]))
        else:
            applied = oldg + [genre]
            await abs_patch(f"/api/items/{row['item_id']}/media", {"metadata": {"genres": applied}})
            old["genres"] = applied
            new["genres"] = [g for g in newg if g.casefold() != genre.casefold()] + [genre]
            if normalize_genres(new["genres"]) == normalize_genres(old["genres"]) and "genres" in selected:
                selected.remove("genres")
            remaining = selected_changed_fields(old, new)
            new_status = "pending" if remaining else "applied"
            with db() as con:
                con.execute("UPDATE suggestions SET old_metadata=?,new_metadata=?,selected_fields=?,status=?,applied_at=? WHERE id=?",
                            (json.dumps(old), json.dumps(new), json.dumps(selected), new_status,
                             int(time.time()) if new_status == "applied" else None, row["id"]))
        touched += 1
    with db() as con:
        con.execute("INSERT INTO genre_decisions(library_id,genre,decision,updated_at) VALUES(?,?,?,?) ON CONFLICT(library_id,genre) DO UPDATE SET decision=excluded.decision,updated_at=excluded.updated_at",
                    (library_id, genre, action, int(time.time())))
    return {"ok": True, "message": f"{action.title()} complete for {touched} suggestion(s)"}


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    libraries = []
    abs_error = ""
    try:
        data = await abs_get("/api/libraries")
        libraries = [x for x in data.get("libraries", []) if x.get("mediaType") == "book"]
    except Exception as e:
        abs_error = str(e)
    groups = []
    overrides = runtime_override_keys()
    for group_name, fields in SETTINGS_UI:
        rendered = []
        for field in fields:
            f = dict(field)
            key = f["key"]
            value = getattr(settings, key)
            f["value"] = "" if key in SECRET_SETTING_KEYS else value
            f["configured"] = bool(value) if key in SECRET_SETTING_KEYS else False
            f["overridden"] = key in overrides
            rendered.append(f)
        groups.append((group_name, rendered))
    return templates.TemplateResponse("settings.html", {
        "request": request, "groups": groups, "libraries": libraries, "abs_error": abs_error,
        "settings": settings, "override_count": len(overrides),
    })


@app.post("/settings")
async def save_application_settings(request: Request):
    form = await request.form()
    current = settings.model_dump()
    values = dict(current)
    bool_keys = {f["key"] for _, fields in SETTINGS_UI for f in fields if f.get("kind") == "bool"}
    for key in bool_keys:
        values[key] = key in form
    for _, fields in SETTINGS_UI:
        for field in fields:
            key = field["key"]
            if key in bool_keys:
                continue
            if key in SECRET_SETTING_KEYS:
                if form.get(f"clear_{key}"):
                    values[key] = ""
                elif str(form.get(key, "")).strip():
                    values[key] = str(form.get(key)).strip()
                continue
            if key in form:
                raw = form.get(key)
                try:
                    values[key] = _coerce_setting_value(key, raw)
                except Exception as e:
                    return JSONResponse({"ok": False, "error": f"Invalid value for {key}: {e}"}, status_code=400)
    try:
        values = _normalize_runtime_values(values)
        # Validate JSON path mapping early so bad mappings don't become a pairing surprise later.
        if str(values.get("media_path_map") or "").strip():
            parsed = json.loads(str(values["media_path_map"]))
            if not isinstance(parsed, dict):
                raise ValueError("Media Path Map must be a JSON object")
        persist_runtime_values(values)
        await restart_scheduled_scan_task()
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return {"ok": True, "message": "Settings saved and applied immediately. No container rebuild required."}


@app.post("/settings/reset")
async def reset_application_settings(request: Request):
    with db() as con:
        con.execute("DELETE FROM app_settings WHERE key LIKE ?", (RUNTIME_SETTING_PREFIX + "%",))
    defaults = dict(ENV_SETTINGS)
    if defaults.get("local_ai_only") and str(defaults.get("ai_provider", "")).casefold() == "openai":
        defaults["ai_provider"] = "ollama"
    apply_runtime_values(defaults)
    await restart_scheduled_scan_task()
    return {"ok": True, "message": "Application overrides cleared; current container/.env defaults restored."}


@app.post("/settings/test/ollama")
async def test_ollama_connection():
    st = await ollama_status()
    if not st.get("connected"):
        return JSONResponse({"ok": False, "error": f"Ollama is not reachable at {settings.ollama_url}"}, status_code=502)
    if not st.get("model_installed"):
        return JSONResponse({"ok": False, "error": f"Ollama connected, but model {settings.ollama_model} is not installed"}, status_code=409)
    return {"ok": True, "message": f"Ollama connected — {settings.ollama_model} is installed"}


@app.post("/settings/test/abs")
async def test_abs_connection():
    try:
        data = await abs_get("/api/libraries")
        count = len(data.get("libraries", []))
        return {"ok": True, "message": f"Audiobookshelf connected — {count} librar{'y' if count == 1 else 'ies'} found"}
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"Audiobookshelf connection failed: {e}"}, status_code=502)


@app.get("/rules", response_class=HTMLResponse)
def rules(request: Request):
    return templates.TemplateResponse("rules.html", {"request": request, "genres": "\n".join(canonical_genres()),
        "aliases": "\n".join(f"{k} = {v}" for k, v in sorted(genre_aliases().items()))})


@app.post("/rules")
def save_rules(request: Request, genres: str = Form(...), aliases: str = Form("")):
    g = [x.strip() for x in genres.splitlines() if x.strip()]
    a = {}
    for line in aliases.splitlines():
        if "=" in line:
            left, right = [x.strip() for x in line.split("=", 1)]
            if left and right:
                a[left.casefold()] = right
    set_setting("genres", g or DEFAULT_GENRES)
    set_setting("genre_aliases", a)
    if wants_json(request):
        return {"ok": True, "message": "Genre rules saved"}
    return RedirectResponse("/rules", status_code=303)


@app.get("/api/ollama/status")
async def api_ollama_status():
    return await ollama_status()


@app.get("/health")
def health():
    return {
        "ok": True, "version": "0.5.4", "abs_url": settings.abs_url,
        "ai_provider": settings.ai_provider, "audiosilo_enabled": settings.audiosilo_enabled,
        "openlibrary_enabled": settings.openlibrary_enabled, "hardcover_enabled": settings.hardcover_enabled,
        "scheduled_scans_enabled": settings.scheduled_scans_enabled,
        "auto_apply_enabled": settings.allow_auto_apply,
        "local_ai_only": settings.local_ai_only, "ollama_url": settings.ollama_url, "ollama_model": settings.ollama_model,
    }
