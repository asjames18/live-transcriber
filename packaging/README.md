# Live Transcriber for Windows

Real-time speech-to-text for your PC's audio (calls, videos) and your microphone, done on this PC with Whisper. Optional meeting notes.

## Install (about 5-10 minutes, mostly downloads)

1. Open the downloaded `LiveTranscriber-Setup-<version>.zip` and double-click **`Install-LiveTranscriber.cmd`**. (Extracting first with right-click, **Extract All...** also works. Run straight from the zip, it finds the download in Downloads, Desktop or Documents and unpacks it for you.)
   - Windows may say *"Windows protected your PC"* (SmartScreen) or *"unknown publisher"*, because the installer isn't code-signed. Click **More info**, then **Run anyway**.
2. The installer sets up everything the app needs, and asks before installing anything:
   - **Python** (if the PC doesn't have Python 3.12 or newer): it asks *"Python is needed, install it now? [Y/n]"*, then downloads the official **Python 3.13.17** from python.org (about 29 MB), checks its SHA-256 checksum and Python Software Foundation signature, and installs it for your Windows account only (no admin needed).
   - **Speech engine**: a private Python environment in `%LOCALAPPDATA%\LiveTranscriber\venv` with faster-whisper, CTranslate2, PyAV, NumPy, ONNX Runtime and PyAudioWPatch (WASAPI audio capture). About 85 MB to download, about 400 MB on disk. Nothing is added to your global Python.
   - **Whisper model** `small.en` (English, runs on any CPU): about 465 MB, downloaded from Hugging Face with progress and automatic retries.
   - **GPU speed-up (optional)**: if an NVIDIA graphics card is found, it offers the CUDA libraries (about 2 GB) and the more accurate `large-v3-turbo` model (1.55 GB). Say no and the CPU model works fine. You need a current NVIDIA driver for the GPU option.
   - No ffmpeg is needed (audio decoding is built into PyAV).
3. It checks the speech engine, adds **Desktop** and **Start menu** shortcuts (**Ctrl+Alt+L**, skipped if another shortcut uses it) and opens the app.

Disk space: about 1 GB (CPU), about 5 GB with the GPU option. Make sure at least 1.5 GB (or 6 GB) is free.
Running the installer again is safe: it repairs or upgrades and skips what's already there. If you're offline or a download fails, it says so and stops safely; run it again later.

Requirements: Windows 10 or 11 (64-bit), internet for the first install, a microphone and/or speakers. Microsoft Edge (built into Windows) hosts the window.

## Where things live

- `%LOCALAPPDATA%\LiveTranscriber\app` - the app
- `%LOCALAPPDATA%\LiveTranscriber\data` - your transcripts (`transcripts\`), meeting notes (`transcripts\notes\`), settings and log
- `%LOCALAPPDATA%\LiveTranscriber\venv` and `models` - the speech engine and Whisper models

## Meeting notes (optional)

The Meeting Notetaker can build notes from the transcript text with an OpenAI-compatible service (OpenRouter by default, free models available). Set your key in the Windows user environment variable `OPENROUTER_API_KEY`; it is never saved in the app's files. Without a key, notes fall back to a local outline. Audio is never uploaded.

## Uninstall

Double-click **`Uninstall-LiveTranscriber.cmd`**, or **Start menu > Uninstall Live Transcriber**. It removes the app, the speech engine and models. Your transcripts are kept unless you choose to delete them. Python stays installed.
