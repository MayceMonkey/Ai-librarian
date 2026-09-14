# AI Librarian v0.5.6.2



## v0.5.6.2 review workflow

- Adds **Apply + Metadata Lock** and **Apply + Complete** directly to pending review cards and suggestion detail pages.
- Applied suggestions now have visually distinct states for ordinary applied, metadata locked, and complete/full-locked books.
- Revert is disabled in the UI and blocked by the API while metadata is protected; unlock metadata first if you intentionally need to revert.


## v0.5.6.1 hotfix

Fixes AJAX forms that contain a control named `action`. Browsers can shadow the form's native `action` property with that input, causing requests to be sent to `/%5Bobject%20HTMLInputElement%5D`. The shared form handler now reads the HTML `action` attribute explicitly, fixing Mark Complete, book locks, scan exemptions, genre action buttons, and other affected controls.

## v0.5.6 protection and cover decisions

v0.5.6 adds persistent book-level protection. Use **Mark Complete** for a fully curated book, or independently lock metadata, the cover, or automatic scanning. Full/complete/scan-exempt books are filtered before provider and AI calls so completed books stop consuming scan resources. The **Protected Books** page includes title/author search, so a book does not need a pending suggestion before you can protect it.

Cover review now supports **Ignore this cover** for a single bad candidate and **Keep current + lock cover** when you prefer the existing artwork. Ignored candidates remain suppressed after rescans and can be restored from **Covers → Ignored / Locked**. Cover locks can be removed there or from **Protected Books**.

Protection is enforced in the server write paths, not only in the UI. Metadata locks block Apply/Auto Apply, cover locks block manual and ≥96% auto-cover changes, and full/complete locks also prevent duplicate removal and audio/eBook file pairing until unlocked.

---

## Settings tab

Most runtime configuration is now editable at **Settings** in the web application. Saved values live in `/config/librarian.db`, override the container/.env values, and apply without rebuilding the container. `.env` is now best treated as first-run/fallback configuration. Docker-level volume mounts still have to be configured in Compose.

If Ollama runs on the Windows host, use `http://host.docker.internal:11434` from the Dockerized AI Librarian; `http://localhost:11434` points to the AI Librarian container itself.

# AI Librarian for Audiobookshelf — v0.5.3

## Ollama-first local AI

v0.5.3 defaults to a local Ollama model. With `LOCAL_AI_ONLY=true` (the default), AI Librarian will not use the OpenAI path even if an old API key remains in `.env`. Metadata-provider lookups such as AudioSilo/Open Library are still internet services; only the AI inference is local.

On the Windows host, install/start Ollama and pull the default model:

```powershell
ollama pull qwen3:4b-instruct
ollama list
```

The Docker container reaches the Windows Ollama service through:

```env
AI_PROVIDER=ollama
LOCAL_AI_ONLY=true
OLLAMA_URL=http://host.docker.internal:11434
OLLAMA_MODEL=qwen3:4b-instruct
OLLAMA_THINK=false
OLLAMA_KEEP_ALIVE=15m
OLLAMA_NUM_CTX=8192
OLLAMA_TEMPERATURE=0.1
OLLAMA_CONCURRENCY=1
AI_SCAN_MODE=smart
```

Ollama serves its Windows API on port 11434. By default Ollama binds only to `127.0.0.1`, which a Docker container may not be able to reach through `host.docker.internal`. If the dashboard says Ollama is unreachable:

1. Quit Ollama from the Windows system tray.
2. Open **Edit environment variables for your account** in Windows.
3. Add `OLLAMA_HOST` with value `0.0.0.0:11434`.
4. Start Ollama again.
5. Reload the AI Librarian dashboard.

The dashboard checks `/api/tags` and tells you separately whether Ollama is reachable and whether the configured model is installed.

---


A self-hosted metadata-quality, verification, and library-cleanup layer for Audiobookshelf.

## v0.5: bounded AI usage + cover workflow

v0.5 focuses on cost predictability and clear cover-review state. It keeps the v0.4.1 performance/apply fixes.

### AI cost controls

OpenAI scans now default to **Smart** mode. Every book is still audited with local rules and metadata providers, but the AI is called only when genre evidence is missing, sparse, or ambiguous. Set `AI_SCAN_MODE=deep` if you deliberately want AI to inspect every book.

AI decisions are cached by the compact metadata/evidence packet for 90 days by default. If the relevant book/provider data is unchanged, a later scan reuses the classification without another paid request.

The OpenAI request is intentionally bounded:

```env
AI_SCAN_MODE=smart
AI_REASONING_EFFORT=minimal
AI_MAX_COMPLETION_TOKENS=400
AI_MAX_SCAN_COST_USD=0.10
AI_CACHE_DAYS=90
```

Book descriptions are truncated to 3,000 characters and provider descriptions to 1,200 characters before AI submission. Only classification-relevant fields are sent. This prevents unexpectedly large provider objects from inflating input-token use.

The live dashboard records actual API usage returned by OpenAI: paid requests, cache hits, input tokens, cached-input tokens, output tokens, reasoning tokens, and estimated spend. When the configured estimated budget is reached, additional AI calls stop while the provider/rule scan continues normally. Only one AI-enabled scan may run at once.

The default estimator values correspond to `gpt-5-mini` pricing at the time v0.5 was built:

```env
AI_INPUT_PRICE_PER_MILLION=0.25
AI_CACHED_INPUT_PRICE_PER_MILLION=0.025
AI_OUTPUT_PRICE_PER_MILLION=2.00
```

If you change `AI_MODEL`, update those three estimator rates too. Set `AI_MAX_SCAN_COST_USD=0` only if you intentionally want to disable the budget guard.

### Pending Covers / Updated Covers

`Covers` is now a real review queue. Once Audiobookshelf confirms a selected cover was applied, that book is removed from **Pending Covers** and written to **Updated Covers**. The updated screen records the book, provider, match score, source URL, and application time while showing the current Audiobookshelf cover.

A later metadata scan does not put an already-updated book back into Pending merely because the provider returns the same candidate again. Cover choices made before v0.5 cannot be reconstructed automatically, so they will not appear in Updated Covers until a v0.5 cover is applied.

## v0.4.1 performance and apply fixes

v0.4.1 keeps the v0.4 library-management features but makes large libraries much lighter on the browser and faster on the network. Review, Cover Art, and Duplicate screens are paginated; scan polling is reduced; provider requests reuse persistent HTTP connections; metadata providers are checked with bounded concurrency; and strong exact AudioSilo matches skip redundant fuzzy lookups.

`Apply selected` runs as a background action job and reports applied, skipped, and failed counts. Suggestions that contain warnings but no writable metadata fields are review-only rather than causing the batch to fail. Auto Apply is implemented when `ALLOW_AUTO_APPLY=true`.


## What's new in v0.4

### Safer automatic title cleanup
The scanner now runs a dedicated cleanup pass after removing known release markers. It removes leftover empty `()`, `[]`, `{}`, dangling hyphens/separators, and repeated whitespace. Meaningful text in parentheses is preserved.

Examples:

```text
Wreck Me Forever (Unabridged) -> Wreck Me Forever
Fourth Wing [Unabridged] -> Fourth Wing
Red Rising (MP3) [Retail] -> Red Rising
It (Novel) -> It (Novel)
Something (Special Edition) -> Something (Special Edition)
```

### Whole-library metadata quality score
Every scanned item gets a persistent health snapshot and 0–100 score. The dashboard reports an overall library score, quality bands, and dimension-level completeness for title, author, genres, cover, identifiers, language, description, narrator (audio only), and series when applicable.

The score is format-aware: EPUB/e-book-only items do not lose points for missing narrator metadata.

### Cover Art screen
Provider cover candidates discovered during normal verification are retained. **Covers** shows the existing cover beside candidate art and provides a one-click **Use this cover** action. The app asks Audiobookshelf to download/store the selected URL through the ABS cover endpoint; it does not edit the audio/e-book file itself.

### Duplicate Resolution screen
Duplicates found by exact ASIN, exact ISBN, or near-identical audiobook duration now become persistent resolution pairs. The screen compares format, identifiers, duration, title, author, and current cover.

You can:
- dismiss an intentional pair
- remove either record from the Audiobookshelf database

The removal action uses ABS database removal only. It does **not** delete the underlying media files.

### Author & narrator entity cleanup
**People** groups punctuation/capitalization variants such as `R. C. Bray`, `R.C. Bray`, and `RC Bray`.

- Authors use Audiobookshelf's author update/merge behavior.
- Narrator variants are normalized across affected item metadata.
- The tool only groups strongly equivalent normalized spellings; it does not guess that differently named people are the same person.

### Bulk genre decisions
**Genres** aggregates every proposed genre addition across pending suggestions. For each genre you can:
- queue it on all matching suggestions
- apply only that genre to all matching books immediately
- reject that genre across the current pending proposals

The underlying genre audit remains additive and checks every scanned book for all supported applicable genres.

## v0.3 features retained

- responsive buttons, loading spinners, success/error toasts
- live scan progress and event feed
- audiobook / e-book / hybrid recognition
- every-book multi-genre completeness audit
- AudioSilo recording-specific narrator verification
- series-order consensus
- exact identifier + duration duplicate detection
- per-field editing/approval
- OpenAI or local Ollama genre auditing
- scheduled scans

## Upgrade from v0.4.1 / v0.4 / v0.3 / v0.2

Keep your existing `.env` and `config` directory, replace the application/project files, then rebuild:

```powershell
cd C:\AI-Librarian
docker compose down
docker compose up -d --build
```

The SQLite schema migrates automatically. v0.5 adds AI usage/cache tables and cover-update history without replacing your existing suggestions or health snapshots.

Open:

```text
http://localhost:13379
```

After upgrading, run one full scan so the new quality dashboard, cover candidates, duplicate queue, and People screen have complete snapshots.

## Providers

AudioSilo is enabled by default for recording-oriented audiobook evidence. Open Library is enabled by default for bibliographic/subject fallback. Optional Hardcover/abs-agg consensus remains disabled by default.

See `.env.example` for configuration.

## Safety defaults

Normal metadata scans remain review-first. The new **Apply genre only**, **Use this cover**, people-cleanup, and duplicate-removal controls are explicit user actions.

The duplicate screen's remove button removes the item from the Audiobookshelf database only; it intentionally does not delete media files.

## Development tests

```powershell
python test_v03.py
python test_v04.py
python test_v041.py
python test_v05.py
```

The regression suites cover punctuation cleanup, format-aware quality scoring, cover/duplicate workflows, people normalization, old-database migration, batch apply/auto apply, AI token/cost metering, AI caching/budget enforcement, compact AI payloads, and the Pending → Updated cover transition.

## v0.5.1 cover auto-update

On **Covers → Pending Covers**, `Auto-update ≥96%` applies the single highest-scoring cover candidate for every pending book whose best match is at least 96%. It runs as a background job, reports progress, and successful items move into **Updated Covers**. Books already in Updated Covers are excluded from future automatic cover runs.
\n\n## v0.5.2 — Audio + eBook Pairing\n\nAudiobookshelf combines audio and e-book formats when the files are in the same book folder. v0.5.2 detects separate audiobook-only and e-book-only items that appear to be the same work and lists them under **Audio + eBook**.\n\nDetection works without filesystem access. To let AI Librarian move the e-book file for you, mount the same book-library host directory that Audiobookshelf uses:\n\n```yaml\nservices:\n  ai-librarian:\n    environment:\n      MEDIA_LIBRARY_ROOT: /abs-library\n      PAIR_MIN_SCORE: 0.88\n    volumes:\n      - ./config:/config\n      - D:/Audiobooks:/abs-library\n```\n\nOn Linux, replace `D:/Audiobooks` with the host directory ABS uses, for example `/mnt/media/audiobooks`. The right-hand container path can remain `/abs-library`.\n\nThe pairing action moves **only supported e-book files** into the existing audiobook folder and then requests Audiobookshelf rescans. It refuses automatic pairing when the audiobook itself is a single file in the library root, because that would require moving the audio file too. No existing destination file is overwritten.\n\n**Progress caveat:** Audiobookshelf reading/listening progress belongs to library items. Moving a separately tracked e-book into an audiobook item may not migrate the original e-book reading progress.\n
### Multiple ABS media roots
If one ABS library includes more than one folder root (for example audio and e-books are mounted separately), mount both into AI Librarian and set `MEDIA_PATH_MAP` to map ABS's server-side path prefixes to the corresponding AI Librarian mounts. Example:

```yaml
environment:
  MEDIA_PATH_MAP: '{"/audiobooks":"/abs-audiobooks","/ebooks":"/abs-ebooks"}'
volumes:
  - D:/Audiobooks:/abs-audiobooks
  - D:/Ebooks:/abs-ebooks
```

`MEDIA_PATH_MAP` takes precedence for items whose absolute ABS paths match a configured prefix; `MEDIA_LIBRARY_ROOT` remains a convenient fallback for single-root libraries.

## v0.5.5 metadata correction audit

With **Audit suspected metadata errors** enabled, Smart-mode local AI inspects structural anomalies such as `Author - Title`, title/author swaps, duplicated author text, file extensions and release text. Example: `Kassie Keegan - Savage Galaxy Rescue` can be proposed as author `Kassie Keegan` and title `Savage Galaxy Rescue`. An inferred split with a blank existing author remains review-only unless provider evidence verifies it.
