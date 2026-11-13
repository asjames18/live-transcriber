# Live Transcriber

Real-time speech-to-text for this PC's audio and microphone, plus an optional Meeting Notetaker. Speech is processed locally with faster-whisper (GPU when available, CPU fallback). It runs a private server on `127.0.0.1` and opens in Microsoft Edge app mode.

Transcripts are saved under `transcripts/` (gitignored). Meeting notes go to `transcripts/notes/`.

## Requirements

- Windows
- Python 3.14
- A virtual environment with the packages in `requirements.txt`
- A local Whisper model in `models/` (large-v3-turbo on GPU, small.en as the CPU fallback)
- A microphone / loopback audio device (PyAudioWPatch)

## Setup

From this folder:

```bat
C:\Python314\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For GPU transcription, also install the commented NVIDIA packages in `requirements.txt` (and have a working CUDA driver). Models download into `models/` on first use; the app sets `HF_HUB_OFFLINE=1` because models are expected to already be present. For the first download, start it once with `HF_HUB_OFFLINE` unset, then you can leave the default.

Copy `settings.example.json` to `settings.json` if you want a starter config (or let the app create one).

## Run on Windows

Use the venv interpreter (not the system `pythonw`), from this folder:

```bat
.venv\Scripts\pythonw.exe transcriber.pyw
```

Create a Desktop shortcut:

- Target: `C:\path\to\live-transcriber\.venv\Scripts\pythonw.exe`
- Arguments: `"C:\path\to\live-transcriber\transcriber.pyw"`
- Start in: the `live-transcriber` folder
- Icon: `transcriber.ico`
- Suggested shortcut key: `Ctrl+Alt+L`

`test/bench.py` and `test/export_check.py` are optional developer checks.

## AI notes (optional)

The Meeting Notetaker sends transcript text only to an OpenAI-compatible endpoint (OpenRouter by default) and falls back to a local outline if AI is unavailable. The API key is read at call time from the environment variable named in settings (`key_env`), typically:

```bat
OPENROUTER_API_KEY
```

The key is never stored in `settings.json` or logs.
