# v0.5.4

- Added database-backed Settings tab.
- Settings saved in `/config/librarian.db` override `.env` and apply immediately.
- Added Audiobookshelf and Ollama connection tests.
- Added provider, Ollama, cloud AI, performance, scheduling, auto-apply, and pairing controls.
- Added runtime scheduler restart when scheduling settings change.
- Added explicit Docker localhost/Ollama warning.
- `.env` remains a first-run/fallback configuration source.
