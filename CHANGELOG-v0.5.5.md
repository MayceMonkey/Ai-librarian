# v0.5.5

- Adds local-AI metadata structure auditing in Smart mode.
- Detects common `Author - Title` imports and proposes separate author/title fields.
- Uses the structural hint to improve provider searches without silently mutating ABS.
- Deep mode can now save AI-detected title/author/subtitle corrections even when no genre issue exists.
- Unverified inferred author/title splits are capped at 95% confidence so the default 98% Auto Apply does not write them blindly.
- If the existing ABS author or external provider verifies the split, confidence can rise normally.
- Adds `AI_METADATA_CORRECTIONS` to Settings/.env and keeps it enabled by default.
