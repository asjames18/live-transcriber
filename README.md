# Live Transcriber

Real-time, on-device speech-to-text for this PC's audio and microphone, with an optional Meeting Notetaker.

![Platform](https://img.shields.io/badge/platform-Windows-0078D6?logo=windows&logoColor=white)
![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?logo=python&logoColor=white)
![Speech](https://img.shields.io/badge/speech-faster--whisper-informational)
![Status](https://img.shields.io/badge/status-public-2ea44f)

## Download and install

Most people should grab the ready-made installer, not this source. It sets up everything the app needs:

1. Download **`LiveTranscriber-Setup-<version>.zip`** from the latest release on this repository's Releases page.
2. Open the zip and double-click **`Install-LiveTranscriber.cmd`** (it unpacks itself if you run it from inside the zip). Windows SmartScreen may warn because the installer isn't code-signed: click **More info**, then **Run anyway**.
3. It installs, asking before anything is downloaded:
   - Python 3.12+ (if missing, the installer asks, then installs the official python.org Python 3.13.17 for your account: SHA-256 and signature checked, no admin).
   - The Microsoft Visual C++ 2015-2022 runtime if it's missing (official Microsoft download, signature checked; Windows asks for admin approval once).
   - A private virtual environment in `%LOCALAPPDATA%\LiveTranscriber\venv` with the pinned packages from `requirements.txt` (about 85 MB download, about 300 MB on disk).
   - The Whisper `small.en` model (about 465 MB) from Hugging Face into `%LOCALAPPDATA%\LiveTranscriber\models`, with progress and retries.
   - Optional, offered when an NVIDIA GPU is present: the CUDA libraries in `requirements-gpu.txt` (about 2 GB) plus the `large-v3-turbo` model (1.55 GB).
   - No ffmpeg needed: PyAV ships its own decoders.
   Total: about 1 GB on disk and 3-10 minutes on a typical connection (about 5 GB with the GPU option).
4. It adds Desktop and Start menu shortcuts and opens the app. Code goes to `%LOCALAPPDATA%\LiveTranscriber\app`, your data to `%LOCALAPPDATA%\LiveTranscriber\data`. Running it again is safe (repair or upgrade, data kept); offline or a failed download stops safely. Uninstall from the Start menu or with `Uninstall-*.cmd`.

The end-user guide is [`packaging/README.md`](packaging/README.md) (it ships inside the zip). `python packaging/build_dist.py . dist` builds the zip and its `.sha256` and refuses to build if it finds keys or personal strings.

## Overview

Live Transcriber captures PC / call audio (WASAPI loopback) and/or the default room microphone, transcribes speech locally with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), and shows a live transcript in its own Microsoft Edge window. A private server listens on `127.0.0.1` only (default port `48241`).

Transcription uses the GPU when CUDA is available (`large-v3-turbo`, float16) and falls back to the CPU (`small.en`, int8). If a GPU error occurs mid-session, the app switches to the CPU model and keeps recording. Speech recognition is English-only.

Transcripts are saved under `transcripts/` (gitignored). Optional meeting notes are saved under `transcripts/notes/`.

## Key Features

- **Live transcription** — Partial results while you speak, then finalized lines, with separate labels for PC / call audio and the room mic.
- **Meeting mode** — Record call audio and the room microphone together (`Ctrl+M`), without a screen recorder.
- **On-device models** — GPU when available, CPU fallback, including an automatic mid-session switch if CUDA fails.
- **Vocabulary** — Optional comma-separated names and terms (people, products, acronyms) used as Whisper hotwords on the next sentence.
- **Meeting Notetaker (optional, on by default)** — Builds structured notes (title, summary, key points, decisions, action items, and questions) from the transcript. It can call an OpenAI-compatible endpoint (OpenRouter by default) on a timer while you record, and again when you stop. If the model is unavailable, a local outline is used instead. You can also generate, copy, or export notes by hand.
- **Session history** — Browse past sessions, copy the transcript, and export Markdown or plain text.
- **Keyboard shortcuts** — Start/stop, source toggles, export, notes, and more. Press `?` in the app for the full list. A suggested desktop shortcut key is `Ctrl+Alt+L`.

## Screenshots

<!-- ![Screenshot](docs/screenshot.png) -->

## Requirements

- Windows
- Python 3.12 or newer (developed on 3.14, installer uses 3.13)
- A virtual environment with the packages in `requirements.txt`
- A local Whisper model in `models/` (`large-v3-turbo` on GPU, `small.en` as the CPU fallback)
- A microphone and/or a loopback audio device (via PyAudioWPatch)
- Microsoft Edge (used to host the app window; the system browser is used as a fallback)
- A working NVIDIA CUDA driver and the optional NVIDIA packages in `requirements.txt` (optional, for GPU transcription)

## Running from source

From this folder, create a virtual environment and install dependencies:

```bat
C:\Python314\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For GPU transcription, also install the commented NVIDIA packages in `requirements.txt`, and have a working CUDA driver.

Models download into `models/` on first use. The app sets `HF_HUB_OFFLINE=1` because models are expected to already be present. For the first download, start the app once with `HF_HUB_OFFLINE` unset, then you can leave the default.

Copy `settings.example.json` to `settings.json` if you want a starter config, or let the app create one.

## Configuration

Live settings are stored in `settings.json` (gitignored). `settings.example.json` shows the shape:

- `vocabulary` — optional names and jargon, comma separated
- `notetaker.enabled` — Meeting Notetaker on or off (default: on)
- `notetaker.interval_sec` — auto-refresh interval while recording (default: 180 seconds)
- `notetaker.base_url` — OpenAI-compatible endpoint (default: `https://openrouter.ai/api/v1`)
- `notetaker.model` — model name (default: `openrouter/free`)
- `notetaker.key_env` — **name** of the environment variable that holds the API key (default: `OPENROUTER_API_KEY`)
- `notetaker.timeout` — request timeout in seconds (default: 60)

The API key itself is never accepted in settings and is never stored in `settings.json` or logs. Set a User environment variable whose name matches `key_env`, typically:

```bat
OPENROUTER_API_KEY
```

The key is read from that environment variable at call time.

Optional environment variables:

| Variable | Purpose |
| --- | --- |
| `TX_PORT` | Server port (default: `48241`) |
| `TX_MODEL` | GPU model name (default: `large-v3-turbo`) |
| `TX_CPU_MODEL` | CPU fallback model name (default: `small.en`) |
| `TX_DEVICE` | Set to `cpu` to skip the GPU and load the CPU model immediately |
| `TX_NO_WINDOW` | Set to `1` to start the server without opening a window |

## Usage

Use the virtual-environment interpreter (not the system `pythonw`), from this folder:

```bat
.venv\Scripts\pythonw.exe transcriber.pyw
```

Create a Desktop shortcut:

- **Target:** `C:\path\to\live-transcriber\.venv\Scripts\pythonw.exe`
- **Arguments:** `"C:\path\to\live-transcriber\transcriber.pyw"`
- **Start in:** the `live-transcriber` folder
- **Icon:** `transcriber.ico`
- **Suggested shortcut key:** `Ctrl+Alt+L`

In the window:

1. Wait until the speech model shows as ready.
2. Choose **PC / call audio**, **Room mic**, or **Meeting mode** (both).
3. Press Space (or the record button) to start and stop.
4. Use **Copy** or **Export** for the transcript. **Clear** clears the live view only; the saved file is kept. **Transcripts** opens the output folder.
5. Use the Meeting notes panel to toggle the notetaker, generate notes, copy Markdown, or export a notes file.

`test/bench.py` and `test/export_check.py` are optional developer checks.

## Privacy & Safety

- The server binds to `127.0.0.1` only and rejects requests whose `Host` header is not `127.0.0.1` or `localhost` on the app port.
- Speech is processed locally with faster-whisper. Audio is not uploaded for transcription.
- When the Meeting Notetaker is off, nothing is sent off the PC for notes either. The window states this as “Runs on this PC. Nothing leaves your computer.”
- When the Meeting Notetaker is on, only transcript **text** is sent to the configured OpenAI-compatible endpoint. Audio is not sent. If the endpoint is unavailable, notes fall back to a local outline with no network call.
- The API key is read from an environment variable at call time and is never stored in `settings.json` or logs.
- Transcripts, exported files, notes, the local log (`transcriber.log`), models, and the Edge window profile stay on this PC and are gitignored.

## Project Structure

| File | Description |
| --- | --- |
| `transcriber.pyw` | Main app: local server, audio capture, Whisper transcription, UI, and Meeting Notetaker |
| `requirements.txt` | Python package pins, including optional commented NVIDIA CUDA libraries |
| `settings.example.json` | Example settings (vocabulary and notetaker). Copy to `settings.json` if you want a starter file |
| `transcriber.ico` | Application icon |
| `test/bench.py` | Optional local benchmark against a WAV file |
| `test/export_check.py` | Optional check that session export works |
| `.gitignore` | Excludes transcripts, notes, models, the virtual environment, logs, live settings, and secrets |

Runtime folders such as `transcripts/`, `models/`, `.venv/`, and `window-profile/` are gitignored and are not part of this repository.

## Author

[Antonio James](https://github.com/asjames18) · Melanated in Tech
