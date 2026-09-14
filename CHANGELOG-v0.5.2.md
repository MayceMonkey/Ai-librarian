# v0.5.2

- Adds Audio + eBook Pairing screen.
- Detects separate audiobook/e-book copies of the same work using conservative title/author/series/year scoring.
- Keeps legitimate audio+ebook pairs out of duplicate deletion workflows.
- Shows current ABS relative storage paths and target combined folder.
- Optional direct storage merge moves only supported e-book files into the existing audiobook folder.
- Requests ABS item rescans after a successful move, with library-scan fallback.
- Direct media mutation is disabled unless MEDIA_LIBRARY_ROOT is explicitly configured and mounted.
- Single-file root audiobooks are detected and blocked from automatic pairing rather than moving audio files automatically.
