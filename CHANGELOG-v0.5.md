# v0.5.0

## AI cost controls
- Smart AI mode is now the default: local/provider evidence checks every book; AI is reserved for ambiguous/sparse cases.
- Added 90-day AI-result cache keyed to compact book/provider evidence.
- Added `reasoning_effort=minimal` and a hard `max_completion_tokens` request cap.
- Added per-scan estimated-dollar budget. AI stops at the budget while non-AI scanning continues.
- Added actual API usage capture: requests, cache hits, input/cached-input/output/reasoning tokens, estimated cost.
- Added live AI usage/cost fields and recent-scan cost history to the dashboard.
- AI metadata is compacted/truncated before submission to prevent large provider payloads from inflating token usage.
- Prevents overlapping AI-enabled scans.

## Covers
- Cover Art is now the Pending Covers queue.
- A successful cover apply moves the item to a dedicated Updated Covers screen.
- Cover history stores selected URL, provider, match score, and application time.
- Rescans preserve the reviewed/updated state.

## Compatibility
- Migrates v0.1–v0.4.1 databases in place.
- Keeps v0.4.1 background Apply Selected and Auto Apply behavior.
