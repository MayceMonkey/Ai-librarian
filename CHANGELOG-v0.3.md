# AI Librarian v0.3 changelog

- Responsive AJAX buttons with pressed/loading/success/error states.
- Live dashboard scan progress, current book, event feed, counters, and scan history.
- Persistent scan telemetry in SQLite; interrupted scans are marked on restart.
- Audiobook / e-book / hybrid media detection.
- E-book-only items skip narrator/audio-only requirements.
- Every-book multi-genre completeness pass against the canonical genre vocabulary.
- Optional AI genre audit runs on every book when enabled for a scan.
- AudioSilo exact ASIN/ISBN recording lookup before fuzzy matching.
- Narrator verification using recording identifiers and duration agreement.
- Series-order consensus using exact identifiers, provider agreement, and local sequence hints.
- Exact ASIN/ISBN duplicate detection plus same-title/author audiobook duration matching.
- E-book and audiobook editions are not considered duplicates solely because title/author match.
- Field-by-field editable proposals with independent apply checkboxes.
- Batch apply/ignore actions.
- v0.2 database migration keeps existing pending suggestions reviewable.
