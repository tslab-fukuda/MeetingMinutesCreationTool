# Web App

Start the local UI app with:

```powershell
python -m uvicorn webapp.server:app --reload
```

Then open:

```text
http://127.0.0.1:8000
```

The app provides:

- top-bar controls for recording, stopping, saving, and compiling
- a left TeX editor bound to `Texテンプレート2026/tmplate.tex`
- a right live transcript panel
- automatic transcript-to-TeX reflection inside an auto-managed block

Notes:

- local monitoring uses `faster-whisper`
- API monitoring uses the same OpenAI key loading as `tools/transcribe_audio.py`
- auto-reflected transcript lines are appended inside a protected TeX block so manual edits outside that block are preserved
