import os, sys, time, glob, wave
import numpy as np
APP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sp = os.path.join(APP, ".venv", "Lib", "site-packages", "nvidia")
for d in glob.glob(os.path.join(sp, "*", "bin")):
    os.add_dll_directory(d); os.environ["PATH"] = d + os.pathsep + os.environ["PATH"]
os.environ["HF_HUB_OFFLINE"] = "1"
from faster_whisper import WhisperModel
w = wave.open(sys.argv[1]); sr = w.getframerate(); ch = w.getnchannels()
x = np.frombuffer(w.readframes(w.getnframes()), np.int16).astype(np.float32) / 32768
if ch > 1: x = x.reshape(-1, ch).mean(1)
if sr != 16000: x = np.interp(np.linspace(0, len(x)-1, int(len(x)*16000/sr)), np.arange(len(x)), x).astype(np.float32)
dur = len(x) / 16000; print(f"audio {dur:.2f}s")
def run(m, beam):
    segs, _ = m.transcribe(x, language="en", beam_size=beam, vad_filter=False, condition_on_previous_text=False, without_timestamps=True)
    return " ".join(s.text.strip() for s in segs)
for name, dev, ct in [("large-v3-turbo", "cuda", "float16"), ("small.en", "cuda", "float16"), ("distil-large-v3", "cuda", "float16")]:
    if name.startswith("distil") and not glob.glob(os.path.join(APP, "models", "*distil*")): continue
    t = time.time(); m = WhisperModel(name, device=dev, compute_type=ct, download_root=os.path.join(APP, "models")); lt = time.time() - t
    run(m, 1)  # warm-up
    for beam in (1, 5 if name != "small.en" else 3):
        ts = []
        for _ in range(3):
            t = time.time(); txt = run(m, beam); ts.append(time.time() - t)
        print(f"{name} {dev}/{ct} beam{beam}: load {lt:.1f}s, best {min(ts):.3f}s, RTF {min(ts)/dur:.3f} ({dur/min(ts):.0f}x realtime) :: {txt}")
    seg = x[:3*16000]; t = time.time(); m.transcribe(seg, language="en", beam_size=1, without_timestamps=True)[0] and None
    segs,_ = m.transcribe(seg, language="en", beam_size=1, without_timestamps=True); list(segs); print(f"  3s partial slice: {time.time()-t:.3f}s")
    del m
import subprocess; print(subprocess.run(["nvidia-smi","--query-gpu=memory.used,memory.total","--format=csv,noheader"],capture_output=True,text=True).stdout)
