# VERSION: 2.1.0 (2026-11-13) - installs like a normal app (installer, data folder, speech model download)
# 2026-11-05 v2 - adds the Meeting Notetaker (live structured notes via OpenRouter, local fallback)
#          and Meeting mode (PC/call audio + room mic together, no screen recorder needed)
"""Live Transcriber: real-time speech-to-text for this PC's audio and microphone.
Runs a private server on 127.0.0.1 and opens its window in Microsoft Edge app mode.
Speech is processed locally with faster-whisper (GPU when available, CPU fallback);
transcripts save to transcripts\\.
Meeting Notetaker (optional, ON by default): sends transcript text only to the configured OpenAI-compatible
endpoint (OpenRouter by default) to build structured notes; falls back to a local outline. Notes save to
transcripts\\notes\\. The API key is read from an environment variable at call time and never stored."""
import os, sys, json, time, glob, queue, socket, threading, subprocess, webbrowser, traceback, datetime as dt
from urllib.parse import urlparse, parse_qs
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
import numpy as np

VERSION = "2.1.0"
APP_DIR = os.path.dirname(os.path.abspath(__file__))


def _installed():
    """app\\installed.json is written by the installer (Install-LiveTranscriber.cmd); None in a plain source folder."""
    try:
        with open(os.path.join(APP_DIR, "installed.json"), "r", encoding="utf-8-sig") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return None


INSTALLED = _installed()
# Installed: %LOCALAPPDATA%\\LiveTranscriber\\{app, data, models, venv}. Source folder: everything next to the code.
if os.environ.get("TX_DATA_DIR"):
    DATA_DIR = os.environ["TX_DATA_DIR"]
elif INSTALLED is not None:
    DATA_DIR = os.path.normpath(os.path.join(APP_DIR, INSTALLED.get("data_dir") or os.path.join("..", "data")))
else:
    DATA_DIR = APP_DIR
if os.environ.get("TX_MODELS_DIR"):
    MODELS = os.environ["TX_MODELS_DIR"]
elif INSTALLED is not None:
    MODELS = os.path.normpath(os.path.join(APP_DIR, INSTALLED.get("models_dir") or os.path.join("..", "models")))
else:
    MODELS = os.path.join(APP_DIR, "models")
OUT_DIR = os.path.join(DATA_DIR, "transcripts")
EXPORT_DIR = os.path.join(OUT_DIR, "exports")
SETTINGS = os.path.join(DATA_DIR, "settings.json")
LOG = os.path.join(DATA_DIR, "transcriber.log")
PORT = int(os.environ.get("TX_PORT", "48241"))
NO_WINDOW = os.environ.get("TX_NO_WINDOW") == "1"
GPU_MODEL = os.environ.get("TX_MODEL", "large-v3-turbo")   # accurate model, used on the GPU
CPU_MODEL = os.environ.get("TX_CPU_MODEL", "small.en")      # fallback when CUDA is unavailable
FORCE_CPU = os.environ.get("TX_DEVICE", "").lower() == "cpu"
URL = f"http://127.0.0.1:{PORT}/"
SR = 16000
os.makedirs(OUT_DIR, exist_ok=True)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")  # models are already downloaded


def log(msg):
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S} {msg}\n")
    except OSError:
        pass


def add_cuda_dll_dirs():
    """Make the pip-installed NVIDIA DLLs (cuBLAS, cuDNN) findable by CTranslate2."""
    found = []
    roots = {os.path.join(sys.prefix, "Lib", "site-packages", "nvidia"),
             os.path.join(APP_DIR, ".venv", "Lib", "site-packages", "nvidia")}
    for root in roots:
        for d in glob.glob(os.path.join(root, "*", "bin")):
            if d in found:
                continue
            found.append(d)
            try:
                os.add_dll_directory(d)
            except (OSError, AttributeError):
                pass
            os.environ["PATH"] = d + os.pathsep + os.environ.get("PATH", "")
    return found


CUDA_DIRS = add_cuda_dll_dirs()

# ---------------- events to the window ----------------
_clients, _clients_lock = [], threading.Lock()
_last_client = [time.time()]


def broadcast(kind, **payload):
    msg = json.dumps({"type": kind, **payload})
    with _clients_lock:
        for q in list(_clients):
            q.put(msg)


# ---------------- settings ----------------
DEFAULT_SETTINGS = {"vocabulary": ""}


_NT_DEFAULT_SETTINGS = {"enabled": True, "interval_sec": 180, "base_url": "https://openrouter.ai/api/v1",
                        "model": "openrouter/free", "key_env": "OPENROUTER_API_KEY", "timeout": 60}


def load_settings():
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            s = {**DEFAULT_SETTINGS, **json.load(f)}
    except (OSError, ValueError):
        s = dict(DEFAULT_SETTINGS)
    nt = s.get("notetaker") if isinstance(s.get("notetaker"), dict) else {}
    s["notetaker"] = {**_NT_DEFAULT_SETTINGS, **{k: v for k, v in nt.items() if k in _NT_DEFAULT_SETTINGS}}
    if nt != s["notetaker"]:
        try:
            save_settings(s)  # write the notetaker block once so it can be edited by hand (never holds the key)
        except OSError:
            pass
    return s


def save_settings(s):
    tmp = SETTINGS + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2)
    os.replace(tmp, SETTINGS)


settings = load_settings()

# ---------------- model ----------------
state = {"model": "loading", "model_error": "", "recording": False, "file": "", "started": 0,
         "sources": {"system": False, "mic": False}, "devices": {},
         "device": "", "model_name": "", "compute": "", "fallback": "", "gpu": ""}
_model = [None]
_cfg = {"partial_every": 1.4, "final_beam": 3}
_switch_lock = threading.Lock()


def gpu_name():
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"], capture_output=True,
                           text=True, timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return r.stdout.strip().splitlines()[0].replace("NVIDIA GeForce ", "") if r.returncode == 0 else ""
    except Exception:
        return ""


def _load(name, device, compute):
    from faster_whisper import WhisperModel
    kw = dict(device=device, compute_type=compute, download_root=MODELS)
    if device == "cpu":
        kw.update(cpu_threads=max(4, (os.cpu_count() or 8) // 2), num_workers=2)
    m = WhisperModel(name, **kw)
    segs, _ = m.transcribe(np.zeros(SR, np.float32), language="en", beam_size=1, without_timestamps=True)
    list(segs)  # warm-up so the first live partial is fast
    return m


def load_cpu(reason):
    state.update(model="loading", fallback=reason)
    broadcast("status", **public_state())
    m = _load(CPU_MODEL, "cpu", "int8")
    _model[0] = m
    _cfg.update(partial_every=1.4, final_beam=3)
    state.update(model="ready", device="cpu", model_name=CPU_MODEL, compute="int8", model_error="")


def model_present(name):
    """True when a faster-whisper model is already in MODELS (Hugging Face cache layout)."""
    return bool(glob.glob(os.path.join(MODELS, f"models--*--faster-whisper-{name}", "snapshots", "*", "model.bin")))


def load_model():
    t = time.time()
    try:
        offline = os.environ.get("HF_HUB_OFFLINE") == "1"
        if offline and not model_present(CPU_MODEL) and not model_present(GPU_MODEL):
            raise RuntimeError(f"The speech model ({CPU_MODEL}) isn't downloaded yet. Run Install-LiveTranscriber.cmd again "
                               f"to download it (about 465 MB), or see the README.")
        if FORCE_CPU:
            load_cpu("TX_DEVICE=cpu is set")
        elif offline and not model_present(GPU_MODEL):
            load_cpu(f"GPU model {GPU_MODEL} isn't installed (the installer's GPU option adds it)")
        else:
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() < 1:
                    raise RuntimeError("no CUDA GPU found")
                _model[0] = _load(GPU_MODEL, "cuda", "float16")
                _cfg.update(partial_every=0.8, final_beam=5)
                state.update(model="ready", device="cuda", model_name=GPU_MODEL, compute="float16",
                             gpu=gpu_name(), fallback="", model_error="")
            except Exception as e:
                log(f"CUDA load failed, using CPU: {e}")
                load_cpu(f"GPU unavailable ({str(e)[:160]})")
        log(f"model ready: {state['model_name']} on {state['device']} ({state['compute']}) in {time.time() - t:.1f}s")
    except Exception as e:
        state["model"], state["model_error"] = "error", str(e)
        log("model load failed: " + traceback.format_exc())
    broadcast("status", **public_state())


def _gpu_failed(e):
    """A CUDA error mid-session: switch to the CPU model once, keep recording."""
    if state["device"] != "cuda":
        return
    with _switch_lock:
        if state["device"] != "cuda":
            return
        log(f"CUDA error during transcription, switching to CPU: {e}")
        try:
            load_cpu(f"GPU error during transcription ({str(e)[:160]})")
        except Exception as e2:
            state["model"], state["model_error"] = "error", str(e2)
    broadcast("status", **public_state())


HALLUCINATIONS = ("", "you", "thank you", "thanks for watching", "thank you for watching", "bye",
                  "please subscribe", "subscribe")


def transcribe(audio, beam):
    m = _model[0]
    if m is None:
        return ""
    vocab = (settings.get("vocabulary") or "").strip()
    try:
        segs, _ = m.transcribe(audio, language="en", beam_size=beam, vad_filter=False,
                               condition_on_previous_text=False, without_timestamps=True,
                               no_speech_threshold=0.6, hotwords=vocab or None)
        text = " ".join(s.text.strip() for s in segs if s.no_speech_prob < 0.8).strip()
    except Exception as e:
        msg = str(e)
        log("transcribe error: " + msg)
        if any(k in msg.lower() for k in ("cuda", "cublas", "cudnn", "gpu")):
            _gpu_failed(msg)
        return ""
    if text.lower().strip(" .!?") in HALLUCINATIONS:
        return ""  # common Whisper hallucinations on near-silence
    return text


# ---------------- transcript file ----------------
_file_lock = threading.Lock()


def write_line(src, text):
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    label = "PC audio" if src == "system" else "Mic"
    with _file_lock:
        if state["file"]:
            with open(state["file"], "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {label}: {text}\n")
    broadcast("final", src=src, text=text, time=stamp)




def parse_transcript(text):
    import re
    out = []
    for raw in text.splitlines():
        m = re.match(r"^\[(\d\d:\d\d:\d\d)\] (PC audio|Mic): (.*)$", raw)
        if m:
            out.append((m.group(1), m.group(2), m.group(3)))
    return out


def export_session(name, fmt):
    p = os.path.join(OUT_DIR, os.path.basename(name))
    if not (name.endswith(".txt") and os.path.exists(p)):
        raise FileNotFoundError(name)
    with open(p, "r", encoding="utf-8") as f:
        raw = f.read()
    lines = parse_transcript(raw)
    title = raw.splitlines()[0] if raw else "Live Transcriber"
    words = sum(len(t.split()) for _, _, t in lines)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    base = os.path.splitext(os.path.basename(name))[0]
    if fmt == "md":
        body = [f"# {title}", "", f"*{len(lines)} lines · {words} words*", ""]
        body += [f"- **{t}** · _{who}_: {txt}" for t, who, txt in lines]
        out = os.path.join(EXPORT_DIR, base + ".md")
    else:
        body = [title, "", *[f"[{t}] {who}: {txt}" for t, who, txt in lines]]
        out = os.path.join(EXPORT_DIR, base + ".txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(body) + "\n")
    return out


# ---------------- meeting notetaker ----------------
# Builds structured meeting notes from the transcript with an OpenAI-compatible model (OpenRouter by default).
# Only transcript TEXT is sent, and only while the notetaker is ON. The API key is read from the named
# environment variable at call time; it is never stored, logged, or sent to the window.
import re
NOTES_DIR = os.path.join(OUT_DIR, "notes")
NT_DEFAULT = {"enabled": True, "interval_sec": 180, "base_url": "https://openrouter.ai/api/v1",
              "model": "openrouter/free", "key_env": "OPENROUTER_API_KEY", "timeout": 60}
NT_MAX_CHARS = 12000      # transcript text sent per request (the newest part when longer)
NT_FIRST_WORDS = 40       # first live notes need this many words
NT_MORE_WORDS = 80        # later live refreshes need this many new words
_nt = {}                  # session name -> notes state
_nt_lock = threading.Lock()
_nt_work = {"busy": False, "queue": {}}   # one AI call in flight; others wait here (name -> reason)
_nt_done = threading.Condition(_nt_lock)


def nt_cfg():
    return {**NT_DEFAULT, **(settings.get("notetaker") or {})}


def nt_key_present():
    c = nt_cfg()
    return bool(c["key_env"] and os.environ.get(c["key_env"]))


def nt_clean_cfg(b):
    """Validate notetaker settings sent from the window. The key itself is never accepted here."""
    c = nt_cfg()
    if "enabled" in b:
        c["enabled"] = bool(b["enabled"])
    if "interval_sec" in b:
        try:
            c["interval_sec"] = max(60, min(1800, int(b["interval_sec"])))
        except (TypeError, ValueError):
            pass
    if "model" in b:
        m = " ".join(str(b["model"]).split())[:120]
        if m:
            c["model"] = m
    if "base_url" in b:
        u = str(b["base_url"]).strip()[:200]
        if re.match(r"^https?://[^\s]+$", u):
            c["base_url"] = u.rstrip("/")
    if "key_env" in b:
        k = str(b["key_env"]).strip()
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$", k):
            c["key_env"] = k
    if "timeout" in b:
        try:
            c["timeout"] = max(15, min(180, int(b["timeout"])))
        except (TypeError, ValueError):
            pass
    return c


def _session_path(name):
    name = os.path.basename(name or "")
    p = os.path.join(OUT_DIR, name)
    return (name, p) if name.endswith(".txt") and os.path.exists(p) else (name, "")


def _read_session(name):
    name, p = _session_path(name)
    if not p:
        return "", "", []
    with _file_lock:
        with open(p, "r", encoding="utf-8") as f:
            raw = f.read()
    return name, (raw.splitlines() or [""])[0], parse_transcript(raw)


def _words(lines):
    return sum(len(t.split()) for _, _, t in lines)


def _nt_json_path(name):
    return os.path.join(NOTES_DIR, os.path.splitext(os.path.basename(name))[0] + "-notes.json")


def nt_state(name):
    """The notes state for one session (loads saved notes from disk the first time)."""
    name = os.path.basename(name or "")
    with _nt_lock:
        s = _nt.get(name)
        if s is None:
            s = {"name": name, "notes": None, "status": "idle", "message": "", "source": "", "model": "",
                 "updated_ts": 0, "words": 0, "note": "", "last_try": 0.0}
            try:
                with open(_nt_json_path(name), "r", encoding="utf-8") as f:
                    saved = json.load(f)
                if isinstance(saved.get("notes"), dict):
                    s.update({k: saved.get(k, s[k]) for k in ("notes", "source", "model", "updated_ts", "words", "note")})
                    s["status"] = "updated"
            except (OSError, ValueError):
                pass
            _nt[name] = s
        return s


def nt_public(name):
    s = nt_state(name)
    c = nt_cfg()
    out = {k: s[k] for k in ("name", "notes", "status", "message", "source", "model", "updated_ts", "words", "note")}
    out.update(enabled=c["enabled"], interval_sec=c["interval_sec"], key_present=nt_key_present(),
               md=notes_markdown(s) if s["notes"] else "")
    return out


def _nt_status(name, status, message=""):
    s = nt_state(name)
    s["status"], s["message"] = status, message
    broadcast("notes_status", name=s["name"], status=status, message=message)


# ---- the model call ----
NT_SYS = (
    "You are the meeting notetaker inside a private transcription app on the user's PC. You receive a speech-to-text "
    "transcript of a meeting. Lines labeled 'Mic' come from the room microphone: the user and anyone else in the room. Lines "
    "labeled 'PC audio' are people on a call (or media) playing through the PC. A meeting can be in-room only, call only, or "
    "both. If the same sentence appears from both sources, it is the call echoing into the room mic: count it once. "
    "Speech recognition makes mistakes: silently fix obvious mis-hearings, "
    "but never invent facts, names, dates, numbers or commitments that are not in the transcript.\n"
    "Return ONLY one JSON object, no markdown, no commentary, with exactly these keys:\n"
    "{\"title\": string (max 80 chars, specific to what was discussed),\n"
    " \"summary\": string (2 to 4 plain sentences),\n"
    " \"key_points\": [string] (3 to 8 short bullets, most important first),\n"
    " \"decisions\": [string] (only decisions actually made or agreed; [] if none),\n"
    " \"action_items\": [{\"who\": string, \"what\": string, \"due\": string}] (who = the person's name if said or clear from "
    "context, otherwise \"Unassigned\"; due only if a time was said, else \"\"; [] if none),\n"
    " \"open_questions\": [string] (questions or issues left unresolved; [] if none)}\n"
    "Keep every bullet under 160 characters. Write in English.")


def _http_post(url, body, headers, timeout=60):
    import urllib.request, socket
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json", **headers}, method="POST")
    end = time.time() + timeout  # hard deadline: OpenRouter keeps slow requests alive with whitespace
    with urllib.request.urlopen(req, timeout=min(timeout, 25)) as r:
        buf = bytearray()
        while True:
            if time.time() > end:
                raise socket.timeout("timed out")
            chunk = r.read1(65536) if hasattr(r, "read1") else r.read(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.decode("utf-8"))


def _redact(text, key=""):
    t = str(text or "")
    if key:
        t = t.replace(key, "[key]")
    return re.sub(r"(sk-[A-Za-z0-9_\-]{6,}|Bearer\s+\S+)", "[key]", t)


def _err_text(e, key="", timeout=60):
    import urllib.error, socket
    if isinstance(e, urllib.error.HTTPError):
        msg = ""
        try:
            body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            err = body.get("error")
            msg = err.get("message", "") if isinstance(err, dict) else str(err or "")
        except Exception:
            pass
        hint = {401: "the key was rejected", 402: "the account needs credits for this model",
                404: "model or endpoint not found",
                429: "rate-limited; free models allow a limited number of requests per minute"}.get(e.code, "")
        return _redact(f"HTTP {e.code}" + (f" ({hint})" if hint else "") + (f": {msg[:140]}" if msg else ""), key)
    if isinstance(e, (socket.timeout, TimeoutError)) or "timed out" in str(e).lower():
        return f"no answer within {timeout} s"
    if isinstance(e, urllib.error.URLError):
        return _redact(f"can't reach the AI service: {e.reason}", key)
    return _redact(f"{type(e).__name__}: {str(e)[:140]}", key)


def _parse_obj(txt):
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", (txt or "").strip(), flags=re.S)
    cands = [t]
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        cands.append(m.group(1))
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        cands.append(m.group(0))
    for c in cands:
        try:
            v = json.loads(c)
        except ValueError:
            continue
        if isinstance(v, dict) and (v.get("summary") or v.get("key_points") or v.get("title")):
            return v
    return None


def _strs(v, n=12, cap=300):
    if isinstance(v, str):
        v = [v] if v.strip() else []
    out = []
    for x in v if isinstance(v, list) else []:
        if isinstance(x, dict):
            x = x.get("text") or x.get("what") or x.get("point") or ""
        x = " ".join(str(x).split()).lstrip("-• ").strip()
        if x and x not in out:
            out.append(x[:cap])
    return out[:n]


def normalize_notes(v):
    acts = []
    for a in (v.get("action_items") or []) if isinstance(v.get("action_items"), list) else []:
        if isinstance(a, dict):
            what = " ".join(str(a.get("what") or a.get("task") or a.get("text") or "").split())
            who = " ".join(str(a.get("who") or a.get("owner") or "").split()) or "Unassigned"
            due = " ".join(str(a.get("due") or a.get("when") or "").split())
        else:
            what, who, due = " ".join(str(a).split()), "Unassigned", ""
        if what:
            acts.append({"who": who[:60], "what": what[:300], "due": due[:60]})
    return {"title": " ".join(str(v.get("title") or "Meeting notes").split())[:120],
            "summary": " ".join(str(v.get("summary") or "").split())[:1500],
            "key_points": _strs(v.get("key_points")), "decisions": _strs(v.get("decisions")),
            "action_items": acts[:15], "open_questions": _strs(v.get("open_questions"))}


def _transcript_for_ai(title, lines, prev):
    full = "\n".join(f"[{t}] {who}: {txt}" for t, who, txt in lines)
    head = f"Session: {title}\nTranscript ({_words(lines)} words):\n"
    if len(full) <= NT_MAX_CHARS:
        return head + full
    cut = full[-NT_MAX_CHARS:]
    cut = cut[cut.find("\n") + 1:]
    ctx = ""
    if prev:
        ctx = ("Notes so far from the earlier part (keep what is still true and merge the new part in):\n"
               + json.dumps(prev, ensure_ascii=False)[:3000] + "\n\n")
    return head + ctx + "[... earlier part of the transcript omitted ...]\n" + cut


def ai_notes(title, lines, prev):
    """Returns (notes, model). Raises on any failure so the caller can fall back to the local outline."""
    import urllib.error
    c = nt_cfg()
    key = os.environ.get(c["key_env"] or "", "")
    if not key and "openrouter.ai" in c["base_url"]:
        raise RuntimeError(f"no API key found in the {c['key_env']} environment variable")
    url = c["base_url"].rstrip("/") + "/chat/completions"
    body = {"model": c["model"], "temperature": 0.2, "max_tokens": 4000,
            "messages": [{"role": "system", "content": NT_SYS},
                         {"role": "user", "content": _transcript_for_ai(title, lines, prev)}],
            "response_format": {"type": "json_object"}}
    hdr = {"Authorization": f"Bearer {key}"} if key else {}
    if "openrouter.ai" in url:
        hdr.update({"HTTP-Referer": "http://127.0.0.1/live-transcriber", "X-Title": "PC Live Transcriber"})
        body["reasoning"] = {"effort": "low", "exclude": True}  # free router may pick a "thinking" model
    tmo, t0 = c["timeout"], time.time()
    left = lambda: max(10, tmo - (time.time() - t0))
    try:
        try:
            r = _http_post(url, body, hdr, timeout=tmo)
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404, 415, 422):
                raise
            body.pop("response_format", None); body.pop("reasoning", None)  # model can't do JSON mode: ask plainly
            r = _http_post(url, body, hdr, timeout=left())
        ch = (r.get("choices") or [{}])[0]; msg = ch.get("message") or {}
        v = _parse_obj(msg.get("content") or "") or (_parse_obj(str(msg.get("reasoning") or "")[-8000:]) if msg.get("reasoning") else None)
        if v is None and tmo - (time.time() - t0) > 15:   # empty or not JSON: one short retry
            body["messages"][0]["content"] = NT_SYS + "\nAnswer with the JSON object immediately."
            if "reasoning" in body:
                body["reasoning"] = {"effort": "minimal", "exclude": True}
            r = _http_post(url, body, hdr, timeout=left())
            ch = (r.get("choices") or [{}])[0]; msg = ch.get("message") or {}
            v = _parse_obj(msg.get("content") or "")
        if v is None:
            raise RuntimeError("the AI answer wasn't usable notes")
        return normalize_notes(v), str(r.get("model") or c["model"])[:80]
    except Exception as e:
        raise RuntimeError(_err_text(e, key, tmo)) from None


# ---- local fallback (no network) ----
_ACT = re.compile(r"\b(i'll|i will|we'll|we will|you'll|need to|needs to|have to|going to|gonna|let's|follow up|"
                  r"send|schedule|set up|make sure|action item|to-?do|by (monday|tuesday|wednesday|thursday|friday|tomorrow|next week|end of))\b", re.I)
_DEC = re.compile(r"\b(decided|decision|agreed|we're going with|we'll go with|let's go with|final(ly)? (call|answer)|settled on|approved)\b", re.I)
_FILL = re.compile(r"^(um+|uh+|yeah|okay|ok|so|right|like|you know|i mean)[, .]*", re.I)


def _sentences(lines):
    out = []
    for _, who, txt in lines:
        for s in re.split(r"(?<=[.!?])\s+", txt):
            s = s.strip()
            for _ in range(3):
                s = _FILL.sub("", s).strip()
            if len(s.split()) >= 4:
                out.append((who, s[0].upper() + s[1:]))
    return out


def local_notes(title, lines):
    sents = _sentences(lines)
    seen, uniq = set(), []
    for who, s in sents:
        k = s.lower()[:80]
        if k not in seen:
            seen.add(k); uniq.append((who, s))
    meaty = [s for _, s in uniq if len(s.split()) >= 8] or [s for _, s in uniq]
    step = max(1, len(meaty) // 8)
    key_points = [s[:220] for s in meaty[::step]][:8]
    decisions = [s[:220] for _, s in uniq if _DEC.search(s)][:6]
    acts = [{"who": "Unassigned", "what": s[:220], "due": ""}
            for who, s in uniq if _ACT.search(s) and not s.endswith("?")][:8]
    questions = [s[:220] for _, s in uniq if s.endswith("?") and len(s.split()) >= 5][:6]
    first = (meaty[0] if meaty else "")
    t = " ".join(first.split()[:9]).rstrip(",.;:") if first else ""
    return {"title": (t + "…") if t else (title or "Meeting notes"),
            "summary": " ".join(meaty[:2])[:500] if meaty else "Not enough speech to summarize yet.",
            "key_points": key_points, "decisions": decisions, "action_items": acts, "open_questions": questions}


# ---- markdown ----
def notes_markdown(s):
    n = s.get("notes") or {}
    when = dt.datetime.fromtimestamp(s.get("updated_ts") or time.time()).strftime("%a, %b %d, %Y %I:%M %p")
    src = f"AI ({s.get('model')})" if s.get("source") == "ai" else "local outline (AI unavailable)"
    md = [f"# {n.get('title') or 'Meeting notes'}", "",
          f"*Meeting notes · {when} · from {s.get('name')} · {src}*", "", "## Summary", "", n.get("summary") or "_None._", ""]

    def sec(h, items):
        md.extend([f"## {h}", ""] + ([f"- {x}" for x in items] or ["_None noted._"]) + [""])
    sec("Key points", n.get("key_points") or [])
    sec("Decisions", n.get("decisions") or [])
    md.extend(["## Action items", ""])
    acts = n.get("action_items") or []
    md.extend([f"- [ ] **{a.get('who') or 'Unassigned'}:** {a.get('what')}" + (f" (due {a['due']})" if a.get("due") else "")
               for a in acts] or ["_None noted._"])
    md.append("")
    sec("Open questions", n.get("open_questions") or [])
    return "\n".join(md).rstrip() + "\n"


def _nt_save(s):
    os.makedirs(NOTES_DIR, exist_ok=True)
    tmp = _nt_json_path(s["name"]) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: s[k] for k in ("name", "notes", "source", "model", "updated_ts", "words", "note")}, f, indent=2)
    os.replace(tmp, _nt_json_path(s["name"]))


def export_notes(name):
    s = nt_state(name)
    if not s["notes"]:
        raise FileNotFoundError(name)
    os.makedirs(NOTES_DIR, exist_ok=True)
    out = os.path.join(NOTES_DIR, os.path.splitext(s["name"])[0] + "-notes.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(notes_markdown(s))
    return out


# ---- scheduling: one call in flight, never on the audio threads ----
def _nt_run(name, reason):
    s = nt_state(name)
    name, title, lines = _read_session(name)
    words = _words(lines)
    if not words:
        _nt_status(name, "updated" if s["notes"] else "idle", "No transcript text yet.")
        return
    if reason == "live" and s["notes"] and words - s["words"] < NT_MORE_WORDS:
        _nt_status(name, "updated", "")  # nothing new enough since the last update
        return
    s["last_try"] = time.time()
    _nt_status(name, "updating", "")
    c = nt_cfg()
    t0 = time.time()
    note, model, source = "", "", "ai"
    try:
        if not c["enabled"]:
            raise RuntimeError("the notetaker is off")
        notes, model = ai_notes(title, lines, s["notes"] if s["source"] == "ai" else None)
        log(f"notes: AI ok ({model}, {words} words, {time.time() - t0:.1f}s, {reason})")
    except Exception as e:
        msg = _redact(str(e), os.environ.get(c["key_env"] or "", ""))
        log(f"notes: AI unavailable, local outline used ({msg}; {reason})")
        if s["notes"] and s["source"] == "ai" and reason == "live":
            # keep the last good AI notes during a live refresh; just report the hiccup
            _nt_status(name, "error", f"Couldn't refresh: {msg}. Showing the last notes.")
            return
        notes, source = local_notes(title, lines), "local"
        note = ("The notetaker is off, so nothing was sent. This is a quick local outline." if not c["enabled"]
                else f"AI unavailable ({msg}). Showing a quick local outline instead.")
    s.update(notes=notes, source=source, model=model, note=note, words=words, updated_ts=time.time(),
             status="updated", message="")
    try:
        _nt_save(s)
    except OSError as e:
        log(f"notes: couldn't save notes json: {e}")
    broadcast("notes", name=name, state=nt_public(name))


def _nt_worker():
    while True:
        with _nt_lock:
            if not _nt_work["queue"]:
                _nt_work["busy"] = False
                _nt_done.notify_all()
                return
            name, reason = next(iter(_nt_work["queue"].items()))
            del _nt_work["queue"][name]
        try:
            _nt_run(name, reason)
        except Exception:
            log("notes error: " + traceback.format_exc())
            _nt_status(name, "error", "Something went wrong building the notes.")


def request_notes(name, reason="manual"):
    """Queue a notes build. Returns at once; results arrive as 'notes' events."""
    name = os.path.basename(name or "")
    with _nt_lock:
        _nt_work["queue"][name] = reason if reason != "live" or name not in _nt_work["queue"] else _nt_work["queue"][name]
        start = not _nt_work["busy"]
        _nt_work["busy"] = True
    if start:
        threading.Thread(target=_nt_worker, daemon=True).start()
    else:
        _nt_status(name, "updating", "Queued behind the current update.")


def wait_notes(timeout):
    end = time.time() + timeout
    with _nt_lock:
        while _nt_work["busy"] and time.time() < end:
            _nt_done.wait(max(0.1, end - time.time()))


def _nt_enough(name, final=False):
    s = nt_state(name)
    _, _, lines = _read_session(name)
    w = _words(lines)
    if final:
        return w >= NT_FIRST_WORDS and (w != s["words"] or s["source"] != "ai")
    return (w >= NT_FIRST_WORDS) if not s["words"] or not s["notes"] else (w - s["words"] >= NT_MORE_WORDS)


def nt_scheduler():
    """While recording with the notetaker on: refresh about every interval_sec when there is enough new speech."""
    while True:
        time.sleep(15)
        try:
            c = nt_cfg()
            if not (state["recording"] and c["enabled"] and state["file"]):
                continue
            name = os.path.basename(state["file"])
            s = nt_state(name)
            since = time.time() - max(s["last_try"], state["started"])
            if since >= c["interval_sec"] and not _nt_work["busy"] and _nt_enough(name):
                request_notes(name, "live")
        except Exception:
            log("notes scheduler error: " + traceback.format_exc())


def nt_on_stop(name):
    if name and nt_cfg()["enabled"]:
        try:
            if _nt_enough(name, final=True):
                request_notes(name, "final")
        except Exception:
            log("notes on stop error: " + traceback.format_exc())


# ---------------- audio capture + endpointing ----------------
def to_mono16k(chunk, channels, rate):
    x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    if rate != SR and len(x):
        k = rate / SR
        if abs(k - round(k)) < 1e-6 and k >= 2:  # 48k/32k: box low-pass then decimate
            k = int(round(k)); n = len(x) // k * k
            x = x[:n].reshape(-1, k).mean(axis=1)
        else:
            n_out = int(len(x) * SR / rate)
            x = np.interp(np.linspace(0, len(x) - 1, n_out), np.arange(len(x)), x).astype(np.float32)
    return x


class Source:
    FRAME = 480  # 30 ms at 16 kHz

    def __init__(self, name):
        self.name = name
        self.q = queue.Queue()
        self.stream = None
        self.stop_flag = threading.Event()
        self.noise = 0.003

    def start(self, pa, info):
        ch = int(info["maxInputChannels"]); rate = int(info["defaultSampleRate"])
        import pyaudiowpatch as pyaudio

        def cb(data, frames, t, status):
            self.q.put(to_mono16k(data, ch, rate))
            return (None, pyaudio.paContinue)
        self.stream = pa.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                              input_device_index=info["index"], frames_per_buffer=int(rate * 0.1),
                              stream_callback=cb)
        self.stream.start_stream()
        threading.Thread(target=self.loop, daemon=True).start()

    def stop(self):
        self.stop_flag.set()
        try:
            if self.stream:
                self.stream.stop_stream(); self.stream.close()
        except Exception:
            pass

    def loop(self):
        pending = np.zeros(0, np.float32)
        speech = []           # frames of the current utterance
        pre = []              # short lead-in before speech starts
        silent_frames = 0
        last_partial = 0.0
        last_level = 0.0
        while not self.stop_flag.is_set():
            try:
                pending = np.concatenate([pending, self.q.get(timeout=0.3)])
            except queue.Empty:
                if speech and not self.stop_flag.is_set():
                    silent_frames += 10
                else:
                    continue
            while len(pending) >= self.FRAME:
                fr, pending = pending[:self.FRAME], pending[self.FRAME:]
                rms = float(np.sqrt(np.mean(fr * fr)) + 1e-9)
                if not speech:
                    self.noise = 0.97 * self.noise + 0.03 * min(rms, 0.05)
                thresh = max(0.006, self.noise * 2.5)
                is_speech = rms > thresh
                if speech:
                    speech.append(fr)
                    silent_frames = 0 if is_speech else silent_frames + 1
                else:
                    pre.append(fr); pre = pre[-10:]
                    if is_speech:
                        speech, silent_frames = pre + [], 0
                        pre = []
                if time.time() - last_level > 0.12:
                    last_level = time.time()
                    broadcast("level", src=self.name, v=min(1.0, rms * 12))
            dur = len(speech) * self.FRAME / SR
            end = speech and (silent_frames >= 22 or dur >= 14)  # ~0.66 s pause or 14 s max
            if end:
                audio = np.concatenate(speech); speech = []; silent_frames = 0
                if len(audio) / SR >= 0.5:
                    text = transcribe(audio, beam=_cfg["final_beam"])
                    if text:
                        write_line(self.name, text)
                broadcast("partial", src=self.name, text="")
            elif speech and dur >= 1.0 and time.time() - last_partial > _cfg["partial_every"]:
                last_partial = time.time()
                text = transcribe(np.concatenate(speech), beam=1)
                broadcast("partial", src=self.name, text=text)


_rec = {"pa": None, "sources": []}
_rec_lock = threading.Lock()


def device_names():
    try:
        import pyaudiowpatch as pyaudio
        pa = pyaudio.PyAudio()
        out = {}
        try:
            out["system"] = pa.get_default_wasapi_loopback()["name"]
        except Exception:
            out["system"] = ""
        try:
            out["mic"] = pa.get_default_input_device_info()["name"]
        except Exception:
            out["mic"] = ""
        pa.terminate()
        return out
    except Exception:
        return {}


def start_recording(system, mic):
    import pyaudiowpatch as pyaudio
    with _rec_lock:
        if state["recording"]:
            return "already recording"
        if state["model"] != "ready":
            return "The speech model is still loading."
        if not (system or mic):
            return "Turn on PC / call audio or Room mic first (Ctrl+M turns on both)."
        pa = pyaudio.PyAudio()
        srcs, errs = [], []
        if system:
            try:
                s = Source("system"); s.start(pa, pa.get_default_wasapi_loopback()); srcs.append(s)
            except Exception as e:
                errs.append(f"PC / call audio: {e}")
        if mic:
            try:
                s = Source("mic"); s.start(pa, pa.get_default_input_device_info()); srcs.append(s)
            except Exception as e:
                errs.append(f"Room mic (no default microphone found or it is in use/blocked in Windows privacy settings): {e}")
        if not srcs:
            pa.terminate()
            return "Couldn't open audio. " + "; ".join(errs)
        name = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S") + ".txt"
        state["file"] = os.path.join(OUT_DIR, name)
        where = "GPU" if state["device"] == "cuda" else "CPU"
        with open(state["file"], "w", encoding="utf-8") as f:
            f.write(f"Live Transcriber - {dt.datetime.now():%A, %B %d, %Y %I:%M %p}\n")
            f.write(f"Model: {state['model_name']} on {where}\n\n")
        _rec.update(pa=pa, sources=srcs)
        state.update(recording=True, started=time.time(),
                     sources={"system": any(s.name == "system" for s in srcs), "mic": any(s.name == "mic" for s in srcs)})
    broadcast("status", **public_state())
    return "; ".join(errs)


def stop_recording():
    with _rec_lock:
        was = state["recording"]
        for s in _rec["sources"]:
            s.stop()
        if _rec["pa"]:
            try:
                _rec["pa"].terminate()
            except Exception:
                pass
        _rec.update(pa=None, sources=[])
        state.update(recording=False)
    broadcast("status", **public_state())
    if was:
        nt_on_stop(os.path.basename(state["file"]))  # final notes pass (runs on its own thread)


def public_state():
    return {"model": state["model"], "model_error": state["model_error"], "recording": state["recording"],
            "file": os.path.basename(state["file"]), "elapsed": int(time.time() - state["started"]) if state["recording"] else 0,
            "sources": state["sources"], "devices": state["devices"], "device": state["device"],
            "model_name": state["model_name"], "compute": state["compute"], "fallback": state["fallback"],
            "gpu": state["gpu"]}


def list_sessions():
    files = sorted((f for f in os.listdir(OUT_DIR) if f.endswith(".txt")), reverse=True)[:40]
    return [{"name": f, "size": os.path.getsize(os.path.join(OUT_DIR, f))} for f in files]


# ---------------- http ----------------
class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok_host(self):
        return self.headers.get("Host", "") in (f"127.0.0.1:{PORT}", f"localhost:{PORT}")

    def _send(self, code, body=b"", ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj).encode("utf-8"))

    def do_GET(self):
        if not self._ok_host():
            return self._send(403)
        u = urlparse(self.path); qs = parse_qs(u.query)
        if u.path in ("/", "/index.html"):
            return self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
        if u.path == "/favicon.ico":
            try:
                with open(os.path.join(APP_DIR, "transcriber.ico"), "rb") as f:
                    return self._send(200, f.read(), "image/x-icon")
            except OSError:
                return self._send(404)
        if u.path == "/api/status":
            return self._json(public_state())
        if u.path == "/api/sessions":
            return self._json(list_sessions())
        if u.path == "/api/settings":
            return self._json({**settings, "key_present": nt_key_present()})
        if u.path == "/api/notes":
            name = os.path.basename((qs.get("name") or [""])[0]) or os.path.basename(state["file"])
            if not name:
                return self._json({"name": "", "notes": None, "status": "idle", "enabled": nt_cfg()["enabled"],
                                   "key_present": nt_key_present(), "interval_sec": nt_cfg()["interval_sec"], "md": ""})
            return self._json(nt_public(name))
        if u.path == "/api/session":
            name = os.path.basename((qs.get("name") or [""])[0])
            p = os.path.join(OUT_DIR, name)
            if not (name.endswith(".txt") and os.path.exists(p)):
                return self._send(404)
            with open(p, "r", encoding="utf-8") as f:
                return self._json({"name": name, "text": f.read()})
        if u.path == "/events":
            return self.events()
        self._send(404)

    def do_POST(self):
        if not self._ok_host():
            return self._send(403)
        n = int(self.headers.get("Content-Length", "0") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}") if n else {}
        except ValueError:
            body = {}
        if self.path == "/api/start":
            err = start_recording(bool(body.get("system")), bool(body.get("mic")))
            return self._json({"ok": state["recording"], "error": err, **public_state()})
        if self.path == "/api/stop":
            stop_recording(); return self._json(public_state())
        if self.path == "/api/open-folder":
            os.startfile(OUT_DIR); return self._json({"ok": True})
        if self.path == "/api/export":
            name = body.get("name") or os.path.basename(state["file"])
            fmt = "md" if body.get("fmt") == "md" else "txt"
            try:
                out = export_session(name, fmt)
            except FileNotFoundError:
                return self._json({"ok": False, "error": "No saved session to export yet."})
            subprocess.Popen(["explorer", "/select,", out])
            return self._json({"ok": True, "path": out, "file": os.path.basename(out)})
        if self.path == "/api/settings":
            if "vocabulary" in body:
                vocab = str(body.get("vocabulary", ""))[:600]
                settings["vocabulary"] = " ".join(vocab.split())
            if isinstance(body.get("notetaker"), dict):
                settings["notetaker"] = nt_clean_cfg(body["notetaker"])
            save_settings(settings)
            return self._json({**settings, "key_present": nt_key_present()})
        if self.path == "/api/notes/generate":
            name, p = _session_path(body.get("name") or os.path.basename(state["file"]))
            if not p:
                return self._json({"ok": False, "error": "Record or open a session first."})
            if not _words(_read_session(name)[2]):
                return self._json({"ok": False, "error": "This session has no transcribed speech yet."})
            request_notes(name, "manual")
            if body.get("wait"):
                wait_notes(nt_cfg()["timeout"] * 3 + 30)
                return self._json({"ok": True, **nt_public(name)})
            return self._json({"ok": True, "queued": True, "name": name})
        if self.path == "/api/notes/export":
            name = os.path.basename(body.get("name") or os.path.basename(state["file"]))
            try:
                out = export_notes(name)
            except FileNotFoundError:
                return self._json({"ok": False, "error": "No notes for this session yet. Press Generate first."})
            if not body.get("quiet"):
                subprocess.Popen(["explorer", "/select,", out])
            return self._json({"ok": True, "path": out, "file": os.path.basename(out)})
        self._send(404)

    def events(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream"); self.send_header("Cache-Control", "no-store")
        self.end_headers()
        q = queue.Queue()
        with _clients_lock:
            _clients.append(q)
        q.put(json.dumps({"type": "status", **public_state()}))
        try:
            while True:
                try:
                    msg = q.get(timeout=10)
                    self.wfile.write(f"data: {msg}\n\n".encode("utf-8"))
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with _clients_lock:
                if q in _clients:
                    _clients.remove(q)
            _last_client[0] = time.time()


def find_edge():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
        if base:
            p = os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe")
            if os.path.exists(p):
                return p
    try:  # Edge installed somewhere unusual: ask Windows (App Paths)
        import winreg
        for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(hive, r"Software\Microsoft\Windows\CurrentVersion\App Paths\msedge.exe") as k:
                    p = winreg.QueryValue(k, None)
                if p and os.path.exists(p.strip('"')):
                    return p.strip('"')
            except OSError:
                pass
    except ImportError:
        pass
    return None


def open_window():
    if NO_WINDOW:
        return
    edge = find_edge()
    if edge:
        subprocess.Popen([edge, f"--app={URL}", f"--user-data-dir={os.path.join(DATA_DIR, 'window-profile')}",
                          "--window-size=1360,860", "--no-first-run", "--no-default-browser-check"])
    else:
        webbrowser.open(URL)


def watchdog(server):
    while True:
        time.sleep(5)
        with _clients_lock:
            n = len(_clients)
        if n:
            _last_client[0] = time.time()
        elif time.time() - _last_client[0] > 120:
            stop_recording(); server.shutdown(); return
        if state["recording"]:
            broadcast("tick", elapsed=int(time.time() - state["started"]))


class AppServer(ThreadingHTTPServer):
    """One copy only: on Windows SO_REUSEADDR would let a second launch bind the same port."""
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def self_test():
    """--self-test: check the packages, the audio devices and the speech model without opening a window. Prints one JSON line."""
    out = {"version": VERSION, "python": sys.version.split()[0], "data_dir": DATA_DIR, "models": MODELS, "ok": False}
    try:
        import faster_whisper, ctranslate2, av  # noqa: F401
        import pyaudiowpatch  # noqa: F401
        out["packages"] = "ok"
        out["devices"] = device_names()
        out["cuda_devices"] = ctranslate2.get_cuda_device_count()
        global FORCE_CPU
        FORCE_CPU = FORCE_CPU or "--cpu" in sys.argv
        load_model()
        out.update(model=state["model"], model_name=state["model_name"], device=state["device"],
                   model_error=state["model_error"][:300], fallback=state["fallback"][:200])
        if state["model"] == "ready":
            segs, _ = _model[0].transcribe(np.zeros(SR, np.float32), language="en", beam_size=1)
            list(segs)
            out["ok"] = True
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    print(json.dumps(out))
    return 0 if out["ok"] else 1


def main():
    if "--self-test" in sys.argv:
        sys.exit(self_test())
    try:
        server = AppServer(("127.0.0.1", PORT), Handler)
    except OSError:
        open_window(); return
    server.daemon_threads = True
    state["devices"] = device_names()
    threading.Thread(target=load_model, daemon=True).start()
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    threading.Thread(target=nt_scheduler, daemon=True).start()
    open_window()
    server.serve_forever()


HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>Live Transcriber</title>
<link rel="icon" href="/favicon.ico">
<style>
:root{--bg:#0d1017;--panel:#141822;--panel2:#1a1f2c;--line:#262c3b;--text:#e8ebf2;--muted:#8a93a8;
 --accent:#7c6cff;--accent2:#4fc3ff;--rec:#ff5d73;--sys:#4fc3ff;--mic:#b69cff;--ok:#3ddc97;--warn:#ffa94d}
*{box-sizing:border-box}html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
 font-family:"Segoe UI Variable Text","Segoe UI",system-ui,sans-serif;font-size:14px}
button{font:inherit;color:inherit}
kbd{font-family:"Cascadia Mono",Consolas,monospace;font-size:11px;background:#0d1017;border:1px solid #333b50;border-bottom-width:2px;border-radius:6px;padding:1px 6px;color:#c9cfdc;white-space:nowrap}
.wrap{display:flex;flex-direction:column;height:100vh;padding:20px 26px 16px;gap:14px}
.top{display:flex;align-items:center;gap:14px}
.logo{width:40px;height:40px;border-radius:12px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;box-shadow:0 6px 18px #7c6cff55;flex:none}
.logo svg{width:22px;height:22px}
.top h1{margin:0;font-size:22px;letter-spacing:-.2px}
.top .sub{color:var(--muted);font-size:12.5px;margin-top:2px}
.spacer{flex:1}
.pill{display:flex;align-items:center;gap:8px;background:var(--panel);border:1px solid var(--line);padding:6px 12px 6px 10px;border-radius:20px;color:var(--muted);font-size:12.5px;max-width:460px}
.pill .d{width:8px;height:8px;border-radius:50%;background:var(--muted);flex:none}
.pill.ready .d{background:var(--ok);box-shadow:0 0 8px #3ddc9799} .pill.err .d{background:var(--rec)}
.pill.load .d{background:var(--warn);animation:blink 1s infinite} .pill.cpu .d{background:var(--warn)}
.pill .dev{font-weight:700;font-size:10.5px;letter-spacing:.6px;padding:2px 7px;border-radius:10px;background:#3ddc9722;color:var(--ok)}
.pill.cpu .dev{background:#ffa94d22;color:var(--warn)}
.pill .mn{color:#dfe3ee;font-weight:600;white-space:nowrap}
.pill .gx{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.iconbtn{width:34px;height:34px;border-radius:10px;background:var(--panel);border:1px solid var(--line);display:grid;place-items:center;cursor:pointer;color:#c9cfdc}
.iconbtn:hover{border-color:#3a4560;color:#fff}.iconbtn svg{width:17px;height:17px}
@keyframes blink{50%{opacity:.3}}
.ctrl{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px 18px;display:flex;align-items:center;gap:18px;flex-wrap:wrap;box-shadow:0 10px 30px #00000033}
.rec{width:62px;height:62px;border-radius:50%;border:0;cursor:pointer;background:linear-gradient(135deg,#ff5d73,#ff8a5b);display:grid;place-items:center;box-shadow:0 8px 24px #ff5d7355;transition:.15s;flex:none}
.rec:hover{transform:scale(1.04)} .rec:disabled{filter:grayscale(1);opacity:.5;cursor:default;transform:none}
.rec .ic{width:22px;height:22px;border-radius:50%;background:#fff;transition:.2s}
.rec.on .ic{border-radius:5px;width:20px;height:20px}
.rec.on{animation:pulse 1.6s infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 #ff5d7366}70%{box-shadow:0 0 0 16px #ff5d7300}100%{box-shadow:0 0 0 0 #ff5d7300}}
.recinfo{min-width:160px}
.recinfo b{font-size:16px;display:block;font-variant-numeric:tabular-nums}
.recinfo span{color:var(--muted);font-size:12.5px}
.recinfo kbd{margin-left:2px}
.srcs{display:flex;gap:12px;flex-wrap:wrap}
.src{display:flex;align-items:center;gap:12px;background:#0d1017;border:1px solid var(--line);border-radius:12px;padding:10px 12px;cursor:pointer;min-width:230px;user-select:none;transition:.15s;position:relative}
.src.on{border-color:var(--c);background:color-mix(in srgb,var(--c) 9%,#0d1017)}
.src .sw{width:34px;height:19px;border-radius:20px;background:#2a3142;position:relative;flex:none;transition:.2s}
.src .sw::after{content:"";position:absolute;width:15px;height:15px;border-radius:50%;background:#fff;top:2px;left:2px;transition:.2s}
.src.on .sw{background:var(--c)} .src.on .sw::after{left:17px}
.src .nm{font-weight:600;display:flex;align-items:center;gap:8px}
.src .nm kbd{font-weight:400;font-size:10px;padding:0 5px;opacity:.75}
.src .dv{color:var(--muted);font-size:11.5px;max-width:170px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.meter{width:60px;height:6px;border-radius:6px;background:#ffffff10;overflow:hidden;margin-top:5px}
.meter i{display:block;height:100%;width:0;background:var(--c);border-radius:6px;transition:width .1s}
.acts{display:flex;gap:8px;margin-left:auto;flex-wrap:wrap}
.mmode{display:flex;align-items:center;gap:9px;background:#0d1017;border:1px solid var(--line);border-radius:12px;padding:8px 12px;cursor:pointer;text-align:left;color:var(--muted)}
.mmode b{display:block;font-size:12.5px;color:#c9cfdc}.mmode small{font-size:11px}
.mmode .mdot{width:10px;height:10px;border-radius:50%;background:#2a3142;flex:none}
.mmode.on{border-color:#3ddc9788;background:#3ddc970d}.mmode.on .mdot{background:var(--ok);box-shadow:0 0 8px #3ddc9799}.mmode.on b{color:#fff}
.mmode:hover{border-color:#3a4560}
.btn{background:#1d2333;border:1px solid var(--line);border-radius:10px;padding:8px 12px;cursor:pointer;color:#c9cfdc;display:flex;align-items:center;gap:7px;font-size:13px;position:relative}
.btn:hover{border-color:#3a4560;color:#fff}
.btn svg{width:15px;height:15px}
.menu{position:absolute;top:calc(100% + 6px);right:0;background:#1a1f2c;border:1px solid #333b50;border-radius:12px;padding:6px;display:none;z-index:20;min-width:210px;box-shadow:0 14px 34px #0009}
.menu.show{display:block}
.menu div{padding:8px 10px;border-radius:8px;display:flex;justify-content:space-between;gap:12px;cursor:pointer;color:#c9cfdc}
.menu div:hover{background:#262c3b;color:#fff}
.main{flex:1;display:grid;grid-template-columns:minmax(0,1fr) 350px 230px;gap:14px;min-height:0}
@media (max-width:1180px){.main{grid-template-columns:minmax(0,1fr) 320px;grid-template-rows:minmax(0,1.25fr) minmax(0,1fr)}
 .main .txwrap{grid-row:1/3}.main .side{grid-column:2;overflow:auto;gap:10px}.side .card.grow{min-height:110px}
 .side .hint{display:none}.side .vrow .hint{display:inline}textarea.vocab{min-height:40px}#nToggleTxt{display:none}.side .card{padding:10px 12px}}
.notes{background:var(--panel);border:1px solid var(--line);border-radius:16px;display:flex;flex-direction:column;min-height:0;overflow:hidden}
.nhead{display:flex;align-items:center;gap:8px;padding:9px 10px 9px 16px;border-bottom:1px solid var(--line)}
.nhead .ttl{font-weight:600;font-size:13px;color:#dfe3ee;white-space:nowrap}
.nchip{font-size:11px;padding:2px 8px;border-radius:10px;background:#ffffff0d;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:150px}
.nchip.busy{background:#7c6cff22;color:#cfc8ff}.nchip.busy::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);margin-right:5px;vertical-align:1px;animation:blink 1s infinite}
.nchip.ok{background:#3ddc971a;color:var(--ok)}.nchip.local{background:#ffa94d1a;color:var(--warn)}.nchip.err{background:#ff5d731a;color:#ff9aa8}
.tsw{display:flex;align-items:center;gap:6px;cursor:pointer;user-select:none;color:var(--muted);font-size:11.5px}
.tsw .sw{width:30px;height:17px;border-radius:20px;background:#2a3142;position:relative;flex:none;transition:.2s}
.tsw .sw::after{content:"";position:absolute;width:13px;height:13px;border-radius:50%;background:#fff;top:2px;left:2px;transition:.2s}
.tsw.on .sw{background:var(--accent)}.tsw.on .sw::after{left:15px}.tsw.on{color:#cfc8ff}
.iconbtn.sm{width:28px;height:28px;border-radius:8px}.iconbtn.sm svg{width:15px;height:15px}.iconbtn.sm.on{border-color:var(--accent);color:#fff}
.nset{display:none;padding:10px 16px 12px;border-bottom:1px solid var(--line);background:#10141d}.nset.show{display:block}
.nset label{display:block;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px;margin:6px 0 4px}
.fld{width:100%;background:#0d1017;border:1px solid var(--line);border-radius:8px;color:var(--text);padding:6px 8px;font:inherit;font-size:12.5px;outline:none}
.fld:focus{border-color:var(--accent)}
.nbody{flex:1;overflow:auto;padding:12px 16px 14px;user-select:text}
.nbody h2{font-size:16px;margin:0 0 3px;line-height:1.3;letter-spacing:-.1px}
.nmeta{color:var(--muted);font-size:11.5px;margin-bottom:10px}
.nsum{line-height:1.55;font-size:13.5px;color:#dfe3ee}
.nsec h4{margin:14px 0 6px;font-size:10.5px;color:var(--muted);text-transform:uppercase;letter-spacing:1.2px;display:flex;align-items:center;gap:6px}
.nsec h4 .cnt{background:#ffffff0d;border-radius:8px;padding:0 6px;letter-spacing:0}
.nsec ul{margin:0;padding-left:18px}.nsec li{margin:4px 0;line-height:1.45;font-size:13.5px}
.nsec li::marker{color:var(--accent)}
.nsec.acts ul{list-style:none;padding-left:0}.nsec.acts li{display:flex;gap:8px;align-items:flex-start}
.owner{font-size:10.5px;font-weight:700;letter-spacing:.3px;padding:2px 7px;border-radius:10px;background:#4fc3ff1c;color:var(--sys);white-space:nowrap;margin-top:1px;flex:none}
.owner.me{background:#b69cff1f;color:var(--mic)}.owner.un{background:#ffffff0d;color:var(--muted)}
.due{color:var(--warn);font-size:11.5px;white-space:nowrap}
.nnone{color:var(--muted);font-size:12.5px;font-style:italic}
.nnote{background:#ffa94d14;border:1px solid #ffa94d44;color:#ffd8a8;border-radius:8px;padding:6px 9px;font-size:12px;margin-bottom:10px;line-height:1.4}
.nnote.err{background:#ff5d7314;border-color:#ff5d7344;color:#ffc2cb}
.nempty{height:100%;display:grid;place-items:center;text-align:center;color:var(--muted);font-size:13px;line-height:1.5;padding:0 10px}
.nempty .big{font-size:30px;margin-bottom:6px}
.nfoot{display:flex;gap:6px;padding:9px 10px;border-top:1px solid var(--line);flex-wrap:wrap}
.btn.primary{background:linear-gradient(135deg,#7c6cff,#5b8cff);border-color:transparent;color:#fff}
.btn.primary:hover{filter:brightness(1.1)}.btn:disabled{opacity:.5;cursor:default}
.txwrap{background:var(--panel);border:1px solid var(--line);border-radius:16px;display:flex;flex-direction:column;min-height:0;position:relative;overflow:hidden}
.txhead{display:flex;align-items:center;gap:10px;padding:11px 18px;border-bottom:1px solid var(--line);font-size:12.5px;color:var(--muted)}
.txhead .vw{color:#dfe3ee;font-weight:600;font-size:13px}
.txhead .tag{font-size:10.5px;font-weight:700;letter-spacing:.6px;padding:2px 8px;border-radius:10px;background:#ff5d7322;color:var(--rec);display:none}
.txhead .tag.show{display:inline-block}
.txhead .back{display:none;color:var(--accent2);cursor:pointer}
.txhead .back.show{display:inline}
.txhead .stats{margin-left:auto;font-variant-numeric:tabular-nums}
.tx{flex:1;overflow:auto;padding:12px 20px 18px;scroll-behavior:smooth}
.line{display:flex;gap:12px;padding:9px 10px;margin:0 -10px;border-radius:10px;animation:in .2s ease;border-left:3px solid transparent}
.line:hover{background:#ffffff05}
@keyframes in{from{opacity:0;transform:translateY(3px)}to{opacity:1}}
.t{color:var(--muted);font-size:11.5px;font-variant-numeric:tabular-nums;padding-top:3px;width:58px;flex:none}
.who{font-size:11px;font-weight:700;letter-spacing:.4px;text-transform:uppercase;padding:3px 8px;border-radius:20px;height:fit-content;flex:none;margin-top:1px;min-width:72px;text-align:center}
.who.system{background:#4fc3ff1c;color:var(--sys)} .who.mic{background:#b69cff1f;color:var(--mic)}
.txt{line-height:1.6;font-size:15px;user-select:text}
.line.partial{border-left:3px dashed color-mix(in srgb,var(--c) 70%,transparent);background:color-mix(in srgb,var(--c) 5%,transparent)}
.line.partial .txt{color:#aab2c5}
.line.partial .t{color:var(--c);font-weight:700;font-size:10.5px;letter-spacing:.6px}
.line.partial .t::before{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--c);margin-right:5px;vertical-align:1px;animation:blink 1s infinite}
.line.partial .txt::after{content:"▍";color:var(--c);animation:blink 1s infinite;margin-left:2px}
.empty{height:100%;display:grid;place-items:center;text-align:center;color:var(--muted)}
.empty .big{font-size:42px;margin-bottom:10px}
.jump{position:absolute;bottom:14px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;border:0;border-radius:20px;padding:7px 14px;font-size:12.5px;cursor:pointer;box-shadow:0 8px 20px #7c6cff55;display:none}
.jump.show{display:block}
.side{display:flex;flex-direction:column;gap:14px;min-height:0}
.card{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px}
.card.grow{flex:1;overflow:auto;min-height:0}
.card h3{margin:2px 4px 10px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1.2px;display:flex;justify-content:space-between;align-items:center}
.sess{padding:9px 10px;border-radius:10px;cursor:pointer;color:#c9cfdc;font-size:13px}
.sess:hover{background:#1b2030} .sess.on{background:#7c6cff22;color:#fff}
.sess small{display:block;color:var(--muted);font-size:11.5px}
.sess .live{color:var(--rec);font-weight:600}
textarea.vocab{width:100%;min-height:62px;resize:vertical;background:#0d1017;border:1px solid var(--line);border-radius:10px;color:var(--text);padding:8px 10px;font:inherit;font-size:12.5px;line-height:1.45;outline:none}
textarea.vocab:focus{border-color:var(--accent)}
.hint{color:var(--muted);font-size:11.5px;margin:6px 2px 8px;line-height:1.4}
.vrow{display:flex;justify-content:space-between;align-items:center}
.small{padding:6px 10px;font-size:12px}
.banner{background:#7c6cff1a;border:1px solid #7c6cff55;color:#d9d3ff;padding:8px 12px;border-radius:10px;font-size:12.5px;display:none}
.banner.warn{background:#ffa94d14;border-color:#ffa94d55;color:#ffd8a8}
.toast{position:fixed;bottom:18px;right:22px;background:#1d2333;border:1px solid var(--line);padding:9px 14px;border-radius:10px;color:#c9cfdc;font-size:12.5px;opacity:0;transition:.3s;pointer-events:none;z-index:50}
.toast.show{opacity:1}
.ov{position:fixed;inset:0;background:#05070bcc;backdrop-filter:blur(3px);display:none;place-items:center;z-index:40}
.ov.show{display:grid}
.sheet{background:var(--panel);border:1px solid #333b50;border-radius:18px;padding:22px 24px;width:min(620px,92vw);max-height:88vh;overflow:auto;box-shadow:0 24px 60px #000a}
.sheet h2{margin:0 0 4px;font-size:18px}
.sheet .sub{color:var(--muted);font-size:12.5px;margin-bottom:14px}
.kgrid{display:grid;grid-template-columns:1fr 1fr;gap:4px 22px}
.kgrid h4{grid-column:1/-1;margin:12px 0 4px;font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1.2px}
.krow{display:flex;justify-content:space-between;align-items:center;gap:10px;padding:6px 0;border-bottom:1px solid #ffffff08;font-size:13px}
.krow span:last-child{display:flex;gap:4px}
::-webkit-scrollbar{width:10px}::-webkit-scrollbar-thumb{background:#262c3b;border-radius:10px;border:2px solid var(--panel)}
</style></head><body>
<div class="wrap">
 <div class="top">
  <div class="logo"><svg viewBox="0 0 24 24" fill="none" stroke="#fff" stroke-width="2.6" stroke-linecap="round"><path d="M4 10v4M8 7v10M12 4v16M16 8v8M20 11v2"/></svg></div>
  <div><h1>Live Transcriber</h1><div class="sub" id="privacy">Runs on this PC. Nothing leaves your computer.</div></div>
  <div class="spacer"></div>
  <div class="pill load" id="model" title=""><span class="d"></span><span class="dev" id="devTag" style="display:none"></span><span class="mn" id="modelTxt">Loading speech model…</span><span class="gx" id="gpuTxt"></span></div>
  <button class="iconbtn" id="keysBtn" title="Keyboard shortcuts (?)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/></svg></button>
 </div>
 <div class="ctrl">
  <button class="rec" id="rec" disabled title="Start or stop (Space or Ctrl+R)"><span class="ic"></span></button>
  <div class="recinfo"><b id="recTitle">Ready</b><span id="recSub">Press <kbd>Space</kbd> or the red button to record</span></div>
  <div class="srcs">
   <div class="src on" id="srcSystem" style="--c:var(--sys)" title="Toggle PC / call audio (Ctrl+1): what plays through your speakers or headset"><div class="sw"></div><div><div class="nm">PC / call audio <kbd>Ctrl 1</kbd></div><div class="dv" id="dvSystem">Teams, Zoom, anything playing</div><div class="meter"><i id="lvSystem"></i></div></div></div>
   <div class="src" id="srcMic" style="--c:var(--mic)" title="Toggle room mic (Ctrl+2): your default microphone hears you and people in the room"><div class="sw"></div><div><div class="nm">Room mic <kbd>Ctrl 2</kbd></div><div class="dv" id="dvMic">You and people in the room</div><div class="meter"><i id="lvMic"></i></div></div></div>
  </div>
  <button class="mmode" id="meetBtn" title="Meeting mode: record call audio and the room mic together (Ctrl+M)"><span class="mdot"></span><span><b>Meeting mode</b><small id="meetSub">Call + room</small></span></button>
  <div class="acts">
   <button class="btn" id="copy" title="Copy transcript (Ctrl+Shift+C)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V5a1 1 0 0 0-1-1H5a1 1 0 0 0-1 1v10a1 1 0 0 0 1 1h3"/></svg>Copy</button>
   <button class="btn" id="exportBtn" title="Export (Ctrl+E)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>Export
    <div class="menu" id="exportMenu"><div data-fmt="md">Markdown (.md) <kbd>Ctrl E</kbd></div><div data-fmt="txt">Plain text (.txt) <kbd>Ctrl Shift E</kbd></div></div></button>
   <button class="btn" id="folder" title="Open transcripts folder (Ctrl+O)"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>Transcripts</button>
   <button class="btn" id="clear" title="Clear the live view (Ctrl+L). The saved file is kept."><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M4 7h16M10 11v6M14 11v6M6 7l1 13h10l1-13M9 7V4h6v3"/></svg>Clear</button>
  </div>
 </div>
 <div class="banner" id="banner"></div>
 <div class="main">
  <div class="txwrap">
   <div class="txhead"><span class="vw" id="viewName">Live view</span><span class="tag" id="liveTag">● REC</span><span class="back" id="backLive">← Back to live <kbd>Esc</kbd></span><span class="stats" id="stats">0 lines · 0 words</span></div>
   <div class="tx" id="tx"></div>
   <button class="jump" id="jump">↓ Jump to latest</button>
  </div>
  <div class="notes" id="notesPanel">
   <div class="nhead"><span class="ttl">Meeting notes</span><span class="nchip" id="nStatus">Idle</span><span class="spacer"></span>
    <div class="tsw on" id="nToggle" title="Notetaker on or off (Ctrl+3)"><div class="sw"></div><span id="nToggleTxt">On</span></div>
    <button class="iconbtn sm" id="nGear" title="Notes settings"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg></button></div>
   <div class="nset" id="nSet">
    <label for="nModel">AI model (OpenRouter)</label><input class="fld" id="nModel" spellcheck="false" placeholder="openrouter/free">
    <label for="nInt">Auto refresh while recording</label>
    <select class="fld" id="nInt"><option value="120">Every 2 minutes</option><option value="180">Every 3 minutes</option><option value="300">Every 5 minutes</option><option value="600">Every 10 minutes</option></select>
    <div class="hint" id="nKey"></div>
    <div class="vrow"><span class="hint" style="margin:0">Also refreshes when you stop.</span><button class="btn small" id="nSave">Save</button></div>
   </div>
   <div class="nbody" id="nBody"></div>
   <div class="nfoot"><button class="btn small primary" id="nGen" title="Build notes now (Ctrl+G)">Generate <kbd>Ctrl G</kbd></button><button class="btn small" id="nCopy" title="Copy notes as Markdown (Ctrl+Shift+G)">Copy</button><button class="btn small" id="nExport" title="Save notes to transcripts\notes (Ctrl+Shift+S)">Export .md</button></div>
  </div>
  <div class="side">
   <div class="card grow"><h3>Past sessions <span><kbd>Alt ↑↓</kbd></span></h3><div id="sessions"></div></div>
   <div class="card"><h3>Vocabulary <span><kbd>Ctrl K</kbd></span></h3>
    <textarea class="vocab" id="vocab" spellcheck="false" placeholder="Names and jargon, comma separated"></textarea>
    <div class="hint">Names and terms to listen for (people, products, acronyms). Used on the next sentence.</div>
    <div class="vrow"><span class="hint" id="vocabState" style="margin:0"></span><button class="btn small" id="vocabSave">Save <kbd>Ctrl ↵</kbd></button></div>
   </div>
  </div>
 </div>
</div>
<div class="ov" id="ov"><div class="sheet" role="dialog" aria-label="Keyboard shortcuts">
 <h2>Keyboard shortcuts</h2><div class="sub">Press <kbd>?</kbd> any time to show this, <kbd>Esc</kbd> to close. Open the app from anywhere with <kbd>Ctrl</kbd><kbd>Alt</kbd><kbd>L</kbd>.</div>
 <div class="kgrid" id="kgrid"></div>
</div></div>
<div class="toast" id="toast"></div>
<script>
const $=s=>document.querySelector(s);
let st={}, sel={system:true,mic:true}, viewing=null, partials={system:null,mic:null}, live=[], sessionList=[], lastFile="";
const el=(t,c)=>{const e=document.createElement(t); if(c)e.className=c; return e;};
function toast(m){const t=$("#toast");t.textContent=m;t.classList.add("show");clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("show"),2000);}
const fmt=s=>{const h=Math.floor(s/3600),m=Math.floor(s%3600/60),x=s%60;return (h?h+":":"")+String(m).padStart(h?2:1,"0")+":"+String(x).padStart(2,"0");};
const atBottom=()=>{const t=$("#tx");return t.scrollHeight-t.scrollTop-t.clientHeight<80;};
function scrollDown(){const t=$("#tx"); t.scrollTop=t.scrollHeight; $("#jump").classList.remove("show");}
const SHORTCUTS=[
 ["Recording"],
 ["Start or stop recording",["Space"],["Ctrl","R"]],
 ["Toggle PC / call audio",["Ctrl","1"]],
 ["Toggle room mic",["Ctrl","2"]],
 ["Meeting mode (call + room mic)",["Ctrl","M"]],
 ["Transcript"],
 ["Copy whole transcript",["Ctrl","Shift","C"]],
 ["Copy (when nothing selected)",["Ctrl","C"]],
 ["Export as Markdown",["Ctrl","E"]],
 ["Export as plain text",["Ctrl","Shift","E"]],
 ["Clear the live view",["Ctrl","L"]],
 ["Jump to latest line",["End"]],
 ["Meeting notes"],
 ["Build or refresh notes now",["Ctrl","G"]],
 ["Copy notes (Markdown)",["Ctrl","Shift","G"]],
 ["Export notes to transcripts\\notes",["Ctrl","Shift","S"]],
 ["Turn the notetaker on or off",["Ctrl","3"]],
 ["Navigate"],
 ["Open transcripts folder",["Ctrl","O"]],
 ["Previous / next session",["Alt","↑"],["Alt","↓"]],
 ["Back to live view",["Esc"]],
 ["Edit vocabulary",["Ctrl","K"]],
 ["Save vocabulary",["Ctrl","Enter"]],
 ["Show shortcuts",["?"]],
 ["Close this panel",["Esc"]],
 ["Open the app (Windows)",["Ctrl","Alt","L"]],
];
function buildKeys(){const g=$("#kgrid");for(const r of SHORTCUTS){
  if(r.length===1){const h=el("h4");h.textContent=r[0];g.append(h);continue;}
  const row=el("div","krow"),a=el("span"),b=el("span");a.textContent=r[0];
  r.slice(1).forEach((combo,i)=>{if(i){const o=el("span");o.textContent="or";o.style.color="var(--muted)";o.style.fontSize="11px";b.append(o);} combo.forEach(k=>{const kb=el("kbd");kb.textContent=k;b.append(kb);});});
  row.append(a,b);g.append(row);}}
function updateStats(){const lines=viewing?[...$("#tx").querySelectorAll(".line:not(.partial)")].length:live.length;
  const words=[...$("#tx").querySelectorAll(".line:not(.partial) .txt")].reduce((n,e)=>n+(e.textContent.trim()?e.textContent.trim().split(/\s+/).length:0),0);
  $("#stats").textContent=`${lines} line${lines===1?"":"s"} · ${words.toLocaleString()} word${words===1?"":"s"}`;}
function showEmpty(msg){const tx=$("#tx"); if(tx.querySelector(".line"))return; tx.innerHTML="";
  const e=el("div","empty"); e.id="empty";
  e.innerHTML=msg||'<div><div class="big">🎙️</div><b>Nothing transcribed yet</b><div style="margin-top:6px">Press <kbd>Space</kbd> or the red button to record. <b>Meeting mode</b> (<kbd>Ctrl M</kbd>) listens to both:<br><b>Room mic</b> for people in the room, and <b>PC / call audio</b> for Teams or Zoom playing on this PC.<br>No screen recorder needed: this app captures audio on its own. Press <kbd>?</kbd> for shortcuts.</div></div>';
  tx.append(e);}
function clearEmpty(){const e=$("#empty"); if(e)e.remove();}
function makeLine(src,text,time,partial){
  const l=el("div","line"+(partial?" partial":"")); l.style.setProperty("--c",src==="system"?"var(--sys)":"var(--mic)");
  const t=el("div","t"); t.textContent=partial?"LIVE":(time||"");
  const w=el("div","who "+src); w.textContent=src==="system"?"Call":"Room";
  const x=el("div","txt"); x.textContent=text; l.append(t,w,x); return l;}
function addLine(src,text,time,partial){
  clearEmpty(); const stick=atBottom(); const l=makeLine(src,text,time,partial);
  const tx=$("#tx"); const firstPartial=tx.querySelector(".line.partial");
  if(!partial && firstPartial) tx.insertBefore(l,firstPartial); else tx.append(l);
  if(stick)scrollDown(); else if(!partial) $("#jump").classList.add("show");
  return l;}
function setPartial(src,text){
  if(viewing)return;
  if(!text){if(partials[src]){partials[src].remove();partials[src]=null;} if(!live.length)showEmpty(); return;}
  if(!partials[src]) partials[src]=addLine(src,text,"",true);
  else {partials[src].querySelector(".txt").textContent=text; if(atBottom())scrollDown();}
}
function renderLive(){
  const tx=$("#tx"); tx.innerHTML=""; partials={system:null,mic:null};
  for(const l of live) tx.append(makeLine(l.src,l.text,l.time,false));
  if(!live.length) showEmpty(); updateStats(); scrollDown(); renderHead();}
function renderHead(){
  $("#viewName").textContent=viewing?sessLabel(viewing):"Live view";
  $("#liveTag").classList.toggle("show",!viewing&&!!st.recording);
  $("#backLive").classList.toggle("show",!!viewing);
  $("#clear").style.display=viewing?"none":"";}
function renderStatus(){
  const m=$("#model"), tag=$("#devTag");
  const cpu=st.device==="cpu";
  m.className="pill "+(st.model==="ready"?("ready"+(cpu?" cpu":"")):st.model==="error"?"err":"load");
  if(st.model==="ready"){
    tag.style.display=""; tag.textContent=cpu?"CPU":"GPU";
    $("#modelTxt").textContent=st.model_name+(st.compute?" · "+st.compute:"");
    $("#gpuTxt").textContent=cpu?"":(st.gpu?"· "+st.gpu:"");
    m.title=(cpu?"Running on the CPU. ":"Running on the GPU (CUDA). ")+(st.fallback||"");
  } else {tag.style.display="none"; $("#gpuTxt").textContent="";
    $("#modelTxt").textContent=st.model==="error"?"Model error":(st.fallback?"Switching to CPU model…":"Loading speech model…");}
  const b=$("#banner");
  if(st.model==="error"){b.className="banner";b.style.display="block";b.textContent="The speech model couldn't load: "+st.model_error;}
  else if(st.fallback&&st.model==="ready"){b.className="banner warn";b.style.display="block";b.textContent="Running on the CPU with "+st.model_name+": "+st.fallback+". Accuracy is lower; restart the app to retry the GPU.";}
  else if(b.classList.contains("warn")) b.style.display="none";
  const r=$("#rec"); r.disabled=st.model!=="ready"; r.classList.toggle("on",!!st.recording);
  $("#recTitle").textContent=st.recording?"Recording "+fmt(st.elapsed||0):(st.model==="ready"?"Ready":"Getting ready…");
  $("#recSub").innerHTML=st.recording?"":"Press <kbd>Space</kbd> or the red button to record";
  if(st.recording)$("#recSub").textContent="Saving to "+st.file+" · Space to stop";
  if(st.devices){ if(st.devices.system)$("#dvSystem").textContent=st.devices.system; if(st.devices.mic)$("#dvMic").textContent=st.devices.mic; }
  const s=st.recording?st.sources:sel;
  $("#srcSystem").classList.toggle("on",!!s.system); $("#srcMic").classList.toggle("on",!!s.mic);
  const both=!!(s.system&&s.mic); $("#meetBtn").classList.toggle("on",both);
  $("#meetSub").textContent=both?"Call + room mic on":(s.mic?"Room mic only":s.system?"Call audio only":"Pick a source");
  if(st.devices&&st.devices.mic) $("#srcMic").title="Room mic: "+st.devices.mic+" (your Windows default microphone). Ctrl+2";
  if(st.file)lastFile=st.file; renderHead();
  if(notesTarget()!==notesFor) loadNotes();
}
function sessLabel(name){const m=name.match(/(\d{4})-(\d\d)-(\d\d)_(\d\d)(\d\d)/); if(!m)return name;
  const d=new Date(+m[1],m[2]-1,+m[3],+m[4],+m[5]); return d.toLocaleDateString(undefined,{weekday:"short",month:"short",day:"numeric"})+", "+d.toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit"});}
async function loadSessions(){
  sessionList=await (await fetch("/api/sessions")).json(); const box=$("#sessions"); box.innerHTML="";
  const lv=el("div","sess"+(viewing?"":" on")); lv.innerHTML="<b>Live view</b><small>"+(st.recording?'<span class="live">● Recording now</span>':"Current session")+"</small>"; lv.onclick=()=>backToLive(); box.append(lv);
  for(const f of sessionList){const d=el("div","sess"+(viewing===f.name?" on":""));
    const b=el("b"); b.textContent=sessLabel(f.name); const sm=el("small");
    sm.textContent=(st.recording&&st.file===f.name?"● Recording · ":"")+Math.max(1,Math.round(f.size/1024))+" KB";
    d.append(b,sm); d.onclick=()=>openSession(f.name); box.append(d);}
}
function parseLines(text){const out=[];for(const raw of text.split("\n")){const m=raw.match(/^\[(\d\d:\d\d:\d\d)\] (PC audio|Mic): (.*)$/); if(m) out.push({src:m[2]==="Mic"?"mic":"system",text:m[3],time:m[1]});} return out;}
async function openSession(name){
  if(st.recording&&name===st.file){return backToLive();}
  viewing=name; const r=await (await fetch("/api/session?name="+encodeURIComponent(name))).json();
  const tx=$("#tx"); tx.innerHTML=""; partials={system:null,mic:null};
  for(const l of parseLines(r.text)) tx.append(makeLine(l.src,l.text,l.time,false));
  if(!tx.children.length) showEmpty('<div>This session has no transcribed lines.</div>');
  tx.scrollTop=0; $("#jump").classList.remove("show"); updateStats(); renderHead(); loadSessions(); loadNotes();
}
function backToLive(){ if(!viewing)return; viewing=null; renderLive(); loadSessions(); loadNotes(); }
function stepSession(dir){const names=[null,...sessionList.map(s=>s.name)]; let i=names.indexOf(viewing); if(i<0)i=0;
  i=Math.max(0,Math.min(names.length-1,i+dir)); if(names[i]===null)backToLive(); else openSession(names[i]);}
function toggleSrc(k){if(st.recording)return toast("Stop recording to change sources"); sel[k]=!sel[k]; renderStatus(); toast((k==="system"?"PC / call audio ":"Room mic ")+(sel[k]?"on":"off"));}
function meetingMode(){if(st.recording)return toast("Stop recording to change sources"); sel={system:true,mic:true}; renderStatus(); toast("Meeting mode: call audio + room mic");}
$("#meetBtn").onclick=meetingMode;
$("#srcSystem").onclick=()=>toggleSrc("system");
$("#srcMic").onclick=()=>toggleSrc("mic");
async function toggleRec(){
  if($("#rec").disabled)return toast("The speech model is still loading");
  if(st.recording){st=await (await fetch("/api/stop",{method:"POST"})).json(); renderStatus(); toast("Saved to "+(st.file||"transcripts")); loadSessions(); return;}
  if(viewing){viewing=null;}
  live=[]; renderLive();
  const r=await (await fetch("/api/start",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(sel)})).json();
  const b=$("#banner"); if(r.error){b.className="banner";b.style.display="block";b.textContent=r.error;} else if(!b.classList.contains("warn")) b.style.display="none";
  st=r; renderStatus(); loadSessions();
}
$("#rec").onclick=()=>{toggleRec(); $("#rec").blur();};
$("#folder").onclick=()=>fetch("/api/open-folder",{method:"POST"});
function transcriptText(){return [...document.querySelectorAll("#tx .line:not(.partial)")].map(l=>{const q=l.children;return `[${q[0].textContent}] ${q[1].textContent}: ${q[2].textContent}`;}).join("\n");}
async function copyAll(){const text=transcriptText(); if(!text)return toast("Nothing to copy yet");
  await navigator.clipboard.writeText(text); const n=text.split("\n").length; toast(`Copied ${n} line${n===1?"":"s"}`);}
$("#copy").onclick=copyAll;
async function doExport(fmt){$("#exportMenu").classList.remove("show");
  const name=viewing||st.file||lastFile; if(!name)return toast("Record or open a session first");
  const r=await (await fetch("/api/export",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name,fmt})})).json();
  toast(r.ok?"Exported "+r.file+" to transcripts\\exports":r.error);}
$("#exportBtn").onclick=e=>{const it=e.target.closest("[data-fmt]"); if(it){e.stopPropagation(); return doExport(it.dataset.fmt);} $("#exportMenu").classList.toggle("show");};
document.addEventListener("click",e=>{if(!e.target.closest("#exportBtn"))$("#exportMenu").classList.remove("show");});
function clearView(){ if(viewing)return; live=[]; renderLive(); toast("Cleared the view. The saved file is unchanged."); }
$("#clear").onclick=clearView;
$("#jump").onclick=scrollDown;
$("#backLive").onclick=backToLive;
$("#tx").addEventListener("scroll",()=>{if(atBottom())$("#jump").classList.remove("show");});
const ov=$("#ov"); const showKeys=v=>ov.classList.toggle("show",v);
$("#keysBtn").onclick=()=>showKeys(true); ov.onclick=e=>{if(e.target===ov)showKeys(false);};
async function loadVocab(){const s=await (await fetch("/api/settings")).json(); $("#vocab").value=s.vocabulary||""; $("#vocabState").textContent="";
  ntCfg=s.notetaker||ntCfg; keyPresent=!!s.key_present; fillNtSettings(); renderNotes();
  if(!st.recording){ sel=ntCfg.enabled?{system:true,mic:true}:{system:true,mic:!!sel.mic}; renderStatus(); }}
async function saveVocab(){const r=await (await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({vocabulary:$("#vocab").value})})).json();
  $("#vocab").value=r.vocabulary; $("#vocabState").textContent="Saved"; toast("Vocabulary saved");}
$("#vocabSave").onclick=saveVocab;
$("#vocab").addEventListener("input",()=>{$("#vocabState").textContent="Unsaved";});

// ---------------- meeting notes ----------------
let notesFor=null, nState=null, ntCfg={enabled:true,interval_sec:180,model:"openrouter/free"}, keyPresent=false;
const notesTarget=()=>viewing||st.file||lastFile||"";
const hhmm=ts=>new Date(ts*1000).toLocaleTimeString(undefined,{hour:"numeric",minute:"2-digit"});
async function loadNotes(){const name=notesTarget(); notesFor=name;
  if(!name){nState=null; renderNotes(); return;}
  try{const r=await (await fetch("/api/notes?name="+encodeURIComponent(name))).json(); if(notesFor!==name)return; nState=r; renderNotes();}catch(e){}}
function sec(title,items,cls){const s=el("div","nsec"+(cls?" "+cls:"")); const h=el("h4"); h.textContent=title;
  const c=el("span","cnt"); c.textContent=items.length; h.append(c); s.append(h);
  if(!items.length){const n=el("div","nnone"); n.textContent="None noted."; s.append(n); return s;}
  const ul=el("ul"); for(const it of items){const li=el("li");
    if(typeof it==="string") li.textContent=it;
    else {const who=(it.who||"Unassigned"); const o=el("span","owner"+(/^me$/i.test(who)?" me":/^unassigned$/i.test(who)?" un":"")); o.textContent=who;
      const w=el("span"); w.textContent=it.what||""; li.append(o,w); if(it.due){const d=el("span","due"); d.textContent="· "+it.due; li.append(d);}}
    ul.append(li);} s.append(ul); return s;}
function renderNotes(){
  const on=!!ntCfg.enabled, b=$("#nBody"), chip=$("#nStatus"), s=nState||{};
  $("#nToggle").classList.toggle("on",on); $("#nToggleTxt").textContent=on?"On":"Off";
  $("#privacy").textContent=on?"Speech is transcribed on this PC. Notetaker sends transcript text to OpenRouter for notes.":"Runs on this PC. Nothing leaves your computer.";
  let cls="", txt="Idle", tip="";
  if(s.status==="updating"){cls="busy"; txt="Updating…"; tip=s.message||"";}
  else if(s.status==="error"){cls="err"; txt="Error"; tip=s.message||"";}
  else if(s.notes&&s.updated_ts){cls=s.source==="local"?"local":"ok"; txt="Updated "+hhmm(s.updated_ts)+(s.source==="local"?" · local":"");}
  else if(!on){txt="Off";}
  chip.className="nchip "+cls; chip.textContent=txt; chip.title=tip||(s.source==="ai"&&s.model?"AI model: "+s.model:"");
  const has=!!(s.notes); $("#nCopy").disabled=!has; $("#nExport").disabled=!has; $("#nGen").disabled=!notesFor;
  b.innerHTML="";
  if(s.status==="error"&&s.message){const n=el("div","nnote err"); n.textContent=s.message; b.append(n);}
  if(!has){const e=el("div","nempty");
    if(!notesFor) e.innerHTML='<div><div class="big">📝</div><b>No meeting notes yet</b><div style="margin-top:6px">Start a recording or open a past session.</div></div>';
    else if(!on) e.innerHTML='<div><div class="big">📝</div><b>Notetaker is off</b><div style="margin-top:6px">Nothing is sent anywhere while it is off.<br>Turn it on (<kbd>Ctrl 3</kbd>) for live notes, or press <kbd>Ctrl G</kbd> for a quick local outline.</div></div>';
    else if(s.status==="updating") e.innerHTML='<div><div class="big">✍️</div><b>Writing notes…</b><div style="margin-top:6px">This usually takes 10 to 40 seconds.</div></div>';
    else e.innerHTML='<div><div class="big">📝</div><b>Notes build as you talk</b><div style="margin-top:6px">While recording they refresh about every '+Math.round((ntCfg.interval_sec||180)/60)+' minutes and once more when you stop.<br>Press <kbd>Ctrl G</kbd> to build them now.</div></div>';
    b.append(e); return;}
  const n=s.notes;
  if(s.note){const x=el("div","nnote"); x.textContent=s.note; b.append(x);}
  const h=el("h2"); h.textContent=n.title||"Meeting notes"; b.append(h);
  const m=el("div","nmeta"); m.textContent=(s.source==="ai"?"AI notes":"Local outline")+" · "+(s.words||0).toLocaleString()+" words · "+hhmm(s.updated_ts); b.append(m);
  const sh=el("div","nsec"); const h4=el("h4"); h4.textContent="Summary"; const p=el("div","nsum"); p.textContent=n.summary||"—"; sh.append(h4,p); b.append(sh);
  b.append(sec("Key points",n.key_points||[]), sec("Decisions",n.decisions||[]), sec("Action items",n.action_items||[],"acts"), sec("Open questions",n.open_questions||[]));
}
async function genNotes(){const name=notesTarget(); if(!name)return toast("Record or open a session first");
  const r=await (await fetch("/api/notes/generate",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})})).json();
  if(!r.ok) return toast(r.error||"Couldn't build notes");
  if(nState&&notesFor===name){nState.status="updating"; renderNotes();}
  toast(ntCfg.enabled?"Building meeting notes…":"Notetaker is off: building a local outline");}
async function copyNotes(){if(!nState||!nState.md)return toast("No notes to copy yet");
  await navigator.clipboard.writeText(nState.md); toast("Copied meeting notes");}
async function exportNotes(){const name=notesTarget(); if(!name)return toast("Record or open a session first");
  const r=await (await fetch("/api/notes/export",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({name})})).json();
  toast(r.ok?"Saved "+r.file+" to transcripts\\notes":r.error);}
async function saveNt(patch,msg){const r=await (await fetch("/api/settings",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({notetaker:patch})})).json();
  ntCfg=r.notetaker||ntCfg; keyPresent=!!r.key_present; fillNtSettings(); renderNotes(); if(msg)toast(msg);}
function toggleNt(){const on=!ntCfg.enabled; saveNt({enabled:on}, on?"Notetaker on: live notes while recording":"Notetaker off: nothing is sent");}
function fillNtSettings(){$("#nModel").value=ntCfg.model||""; const sel=$("#nInt"), v=String(ntCfg.interval_sec||180);
  if(![...sel.options].some(o=>o.value===v)){const o=el("option"); o.value=v; o.textContent="Every "+Math.round(v/60)+" minutes"; sel.append(o);} sel.value=v;
  $("#nKey").textContent=keyPresent?"API key found in the "+(ntCfg.key_env||"OPENROUTER_API_KEY")+" environment variable (never shown or saved).":"No API key found in "+(ntCfg.key_env||"OPENROUTER_API_KEY")+": notes will use the local outline.";}
$("#nToggle").onclick=toggleNt;
$("#nGear").onclick=()=>{const v=$("#nSet").classList.toggle("show"); $("#nGear").classList.toggle("on",v);};
$("#nSave").onclick=()=>saveNt({model:$("#nModel").value,interval_sec:+$("#nInt").value},"Notes settings saved");
$("#nGen").onclick=genNotes; $("#nCopy").onclick=copyNotes; $("#nExport").onclick=exportNotes;
document.addEventListener("keydown",e=>{
  const k=e.key, lk=k.toLowerCase(), c=e.ctrlKey||e.metaKey;
  if(ov.classList.contains("show")){ if(k==="Escape"||k==="?"){e.preventDefault();showKeys(false);} return; }
  const typing=e.target.closest&&e.target.closest("input,textarea,[contenteditable]");
  if(typing){ if(k==="Escape")e.target.blur(); else if(c&&k==="Enter"&&e.target.id==="vocab"){e.preventDefault();saveVocab();} return; }
  const hit=f=>{e.preventDefault(); f();};
  if(k===" "&&!c&&!e.altKey) return hit(toggleRec);
  if(c&&lk==="g") return hit(e.shiftKey?copyNotes:genNotes);
  if(c&&e.shiftKey&&lk==="s") return hit(exportNotes);
  if(c&&!e.shiftKey&&(k==="3"||e.code==="Digit3")) return hit(toggleNt);
  if(c&&!e.shiftKey&&lk==="m") return hit(meetingMode);
  if(c&&!e.shiftKey&&lk==="r") return hit(toggleRec);
  if(c&&e.shiftKey&&lk==="c") return hit(copyAll);
  if(c&&!e.shiftKey&&lk==="c"){ if(!String(window.getSelection()))hit(copyAll); return; }
  if(c&&lk==="o") return hit(()=>fetch("/api/open-folder",{method:"POST"}));
  if(c&&(k==="1"||e.code==="Digit1")) return hit(()=>toggleSrc("system"));
  if(c&&(k==="2"||e.code==="Digit2")) return hit(()=>toggleSrc("mic"));
  if(c&&lk==="e") return hit(()=>doExport(e.shiftKey?"txt":"md"));
  if(c&&lk==="l") return hit(clearView);
  if(c&&lk==="k") return hit(()=>{$("#vocab").focus();$("#vocab").select();});
  if(e.altKey&&k==="ArrowUp") return hit(()=>stepSession(-1));
  if(e.altKey&&k==="ArrowDown") return hit(()=>stepSession(1));
  if(k==="End"&&!c) return hit(scrollDown);
  if(k==="?"||(e.shiftKey&&e.code==="Slash")) return hit(()=>showKeys(true));
  if(k==="Escape"){ $("#exportMenu").classList.remove("show"); if(viewing) hit(backToLive); }
});
const es=new EventSource("/events");
let first=true;
es.onmessage=async e=>{const m=JSON.parse(e.data);
  if(m.type==="status"){st=m;renderStatus();
    if(first){first=false; if(st.recording&&st.file){ // window reopened mid-session: restore lines
      const r=await (await fetch("/api/session?name="+encodeURIComponent(st.file))).json(); live=parseLines(r.text); if(!viewing)renderLive();} loadSessions();}}
  else if(m.type==="tick"){st.elapsed=m.elapsed;renderStatus();}
  else if(m.type==="level"){const b=m.src==="system"?$("#lvSystem"):$("#lvMic"); b.style.width=Math.round(m.v*100)+"%";}
  else if(m.type==="partial"){setPartial(m.src,m.text);}
  else if(m.type==="notes_status"){ if(m.name===notesFor){ nState=Object.assign(nState||{name:m.name},{status:m.status,message:m.message||""}); renderNotes(); } }
  else if(m.type==="notes"){ if(m.name===notesFor){ nState=m.state; renderNotes(); } else if(m.name===st.file&&!viewing){ loadNotes(); } }
  else if(m.type==="final"){ live.push({src:m.src,text:m.text,time:m.time}); if(viewing)return;
    if(partials[m.src]){partials[m.src].remove();partials[m.src]=null;} addLine(m.src,m.text,m.time); updateStats();}
};
es.onerror=()=>{$("#modelTxt").textContent="Reconnecting…";};
buildKeys(); showEmpty(); updateStats(); loadSessions(); loadVocab(); renderNotes();
</script></body></html>
"""

if __name__ == "__main__":
    main()
