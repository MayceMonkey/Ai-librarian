# v0.5.6

- Adds persistent per-book protection controls: Mark Complete, Full Lock, Metadata Lock, Cover Lock, and Scan Exempt.
- Protected state is enforced in backend write paths, including metadata apply, cover apply/auto-update, duplicate removal, narrator cleanup, genre bulk apply, author normalization safeguards, and audio/eBook pairing.
- Full/complete/scan-exempt books skip provider and AI work during scans; metadata+cover locked books are also skipped because no automatic change can be useful.
- Scan-exempt books preserve their previous health/provider/cover snapshot instead of erasing it.
- Adds a Protected Books screen with title/author search so already-correct books can be protected even when they have no pending suggestions.
- Adds per-cover Ignore actions. Ignored cover URLs remain suppressed across rescans.
- Adds Keep Current + Lock Cover to suppress all future cover suggestions/auto-updates for a book.
- Adds an Ignored / Locked Covers screen with restore/unlock controls.
- Mark Complete automatically protects metadata and cover, skips future provider/AI scans, clears pending metadata suggestions, and dismisses pending duplicate/pairing work for the book.
- Dashboard now reports Protected Books and Marked Complete counts.
- Adds v0.5.6 regression coverage for locks, ignored covers, protected apply paths, snapshot preservation, and zero-provider/AI scan behavior.
