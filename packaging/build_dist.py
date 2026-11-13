"""Build <ZipPrefix>-<version>.zip (+ .sha256) from the clean source.
Usage: python packaging/build_dist.py <src_dir> <out_dir>   (e.g. python packaging/build_dist.py . dist)
Refuses to build if it finds keys, tokens or personal strings in any file it packs."""
import hashlib, json, os, re, sys, zipfile
SRC, OUT = sys.argv[1], sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(HERE, "dist.json"), encoding="utf-8"))
BAD = [r"sk-or-v1-[A-Za-z0-9]{10,}", r"\bsk-[A-Za-z0-9]{20,}", r"ghp_[A-Za-z0-9]{20,}", r"github_pat_[A-Za-z0-9_]{20,}", r"AKIA[0-9A-Z]{16}",
       r"hf_[A-Za-z0-9]{30,}", r"xox[abp]-[A-Za-z0-9-]{10,}", r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
       r"(?i)jamesa", r"Antonio", r"Element\s?451", r"Tony, A", r"asjames18@", r"(?i)sfsc", r"(?i)southflorida", r"(?i)south florida state",
       r"@[a-z0-9-]+\.edu\b", r"(?i)IT0" + "3230", r"(?i)grok" + "bot", r"@gmail\.com"]   # split so this file doesn't contain the strings it blocks

ver = re.search(CFG["version_regex"], open(os.path.join(SRC, CFG["version_file"]), encoding="utf-8").read(), re.M).group(1)
root = f"{CFG['zip_prefix']}-{ver}"
os.makedirs(OUT, exist_ok=True)
zpath = os.path.join(OUT, root + ".zip")


def crlf(b):
    return b.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")


entries = [(f"{root}/app/{f}", open(os.path.join(SRC, f), "rb").read()) for f in CFG["app_files"]]
entries += [(f"{root}/{f}", open(os.path.join(HERE, f), "rb").read()) for f in CFG["top_files"]]
entries += [(f"{root}/installer/{f}", open(os.path.join(HERE, "installer", f), "rb").read()) for f in CFG["installer_files"]]
problems = []
for name, b in entries:
    if name.endswith((".png", ".ico")):
        continue
    txt = b.decode("utf-8", "replace")
    if name.endswith((".ps1", ".cmd")) and re.search(r"[^\x00-\x7f]", txt):
        problems.append(f"{name}: non-ASCII character (Windows PowerShell 5.1 would misread it)")
    for pat in BAD:
        for m in re.finditer(pat, txt):
            problems.append(f"{name}: {pat} -> {txt[max(0, m.start()-30):m.end()+30]!r}")
if problems:
    print("REFUSING TO BUILD, personal data, secrets or bad characters found:\n" + "\n".join(problems))
    sys.exit(1)
with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
    for name, b in entries:
        if name.endswith((".cmd", ".ps1")):
            b = crlf(b)
        zi = zipfile.ZipInfo(name, date_time=(2026, 11, 13, 12, 0, 0))
        zi.compress_type = zipfile.ZIP_DEFLATED
        zi.external_attr = 0o644 << 16
        z.writestr(zi, b)
sha = hashlib.sha256(open(zpath, "rb").read()).hexdigest()
open(zpath + ".sha256", "w").write(f"{sha}  {os.path.basename(zpath)}\n")
print(zpath, os.path.getsize(zpath), "bytes", "sha256", sha)
