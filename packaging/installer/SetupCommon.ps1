<#
  Shared setup helpers used by the Work To-Do, Live Transcriber and PC Cleanup & Organizer installers
  (the same file ships in each installer\ folder). PowerShell 5.1 compatible, no admin needed.

  What it can install, and only after asking (or when a -Yes style switch was passed):
    * Python 3.13 (64-bit) from python.org, per-user. The download is checked against a pinned SHA-256
      and its Authenticode signature (Python Software Foundation) before it runs.
    * An app-private virtual environment (pip packages go there, never into the global Python).
    * Whisper speech models from Hugging Face (with progress and retries).
#>

$script:PyPin = @{
  Version = "3.13.17"
  Url     = "https://www.python.org/ftp/python/3.13.17/python-3.13.17-amd64.exe"
  Sha256  = "5DA012101BA2E17113A9A1CD901E9CE1441086F2B7ECB5DE06EC94163899A190"
  Signer  = "Python Software Foundation"
  SizeMB  = 29
  # Test-only sandbox mode unpacks these component MSIs from the same python.org release (each pinned + signature-checked).
  Msi     = @{
    core  = "592AF28B25B8142111B3600C82792F778FFB522B12768170A205FB90E40C72E8"
    exe   = "985E3CE8B3F0A432FDADFA5AACAC506635816C5D24A36FD6222B9E390DA279D5"
    lib   = "7D047AE8B57857E33C3827FE77B052297E61BDAB6B3FD4FECD343C3C30E3A8FF"
    tcltk = "AE3E5F9DD998B36D14655CD3444D0AF906C1849C60AEE40994EEB1E460A3CABF"
  }
}
$script:WhisperRepos = @{
  "small.en"       = @{ Repo = "Systran/faster-whisper-small.en";             SizeMB = 465 }
  "large-v3-turbo" = @{ Repo = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"; SizeMB = 1550 }
}

function Say($msg, $color) { if ($color) { Write-Host $msg -ForegroundColor $color } else { Write-Host $msg } }

function Initialize-Net {
  # Windows 10 PowerShell 5.1 may default to TLS 1.0; python.org, PyPI and Hugging Face need TLS 1.2+.
  try { [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12 } catch { }
}

function Write-Utf8NoBom($path, $text) {
  # Python reads these files as UTF-8; PowerShell 5.1's -Encoding UTF8 would add a BOM, ASCII would mangle non-English names.
  [IO.File]::WriteAllText($path, $text, (New-Object Text.UTF8Encoding $false))
}

function Ask-YesNo($question, [bool]$default, [switch]$Quiet) {
  if ($Quiet) { return $default }
  $hint = "[y/N]"; if ($default) { $hint = "[Y/n]" }
  $a = Read-Host "  $question $hint"
  if ([string]::IsNullOrWhiteSpace($a)) { return $default }
  return ($a -match '^\s*(y|yes)\s*$')
}

function Get-FreeMB($path) {
  try {
    $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($path))
    return [math]::Floor((New-Object IO.DriveInfo $root).AvailableFreeSpace / 1MB)
  } catch { return -1 }
}

function Assert-FreeSpace($path, [int]$needMB, $what) {
  $free = Get-FreeMB $path
  if ($free -ge 0 -and $free -lt $needMB) {
    Say ("  Not enough disk space for {0}: about {1:N0} MB is needed and {2:N0} MB is free on {3}." -f $what, $needMB, $free, [IO.Path]::GetPathRoot($path)) Red
    Say "  Free up some space (PC Cleanup & Organizer can help), then run the installer again. Nothing was changed."
    return $false
  }
  return $true
}

function Test-Online([string[]]$urls) {
  Initialize-Net
  foreach ($u in $urls) {
    try {
      $r = [Net.HttpWebRequest]::Create($u); $r.Method = "HEAD"; $r.Timeout = 12000; $r.AllowAutoRedirect = $true
      $r.UserAgent = "WindowsAppSetup/1.0"
      $resp = $r.GetResponse(); $resp.Close()
    } catch [Net.WebException] {
      # An HTTP error status still proves we're online; only a connection failure means offline.
      if ($_.Exception.Response) { continue }
      return $false
    } catch { return $false }
  }
  return $true
}

function Save-Download([string]$url, [string]$dest, [string]$label, [int]$tries = 3) {
  Initialize-Net
  for ($i = 1; $i -le $tries; $i++) {
    $in = $null; $out = $null; $resp = $null
    $tmp = "$dest.part"
    try {
      $req = [Net.HttpWebRequest]::Create($url)
      $req.Timeout = 30000; $req.ReadWriteTimeout = 60000; $req.UserAgent = "WindowsAppSetup/1.0"
      $resp = $req.GetResponse(); $total = $resp.ContentLength
      $in = $resp.GetResponseStream(); $out = [IO.File]::Create($tmp)
      $buf = New-Object byte[] 262144; $done = 0; $last = -1; $n = 0
      while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
        $out.Write($buf, 0, $n); $done += $n
        if ($total -gt 0) {
          $pct = [int][math]::Floor(100 * $done / $total)
          if ($pct -ne $last -and ($pct % 5 -eq 0)) { Write-Host -NoNewline ("`r    {0}: {1,3}%  ({2:N1} of {3:N1} MB)" -f $label, $pct, ($done / 1MB), ($total / 1MB)); $last = $pct }
        }
      }
      $out.Close(); $out = $null; $in.Close(); $in = $null; $resp.Close(); $resp = $null
      Write-Host ""
      if ($total -gt 0 -and $done -ne $total) { throw "the download was cut short ($done of $total bytes)" }
      Move-Item -LiteralPath $tmp -Destination $dest -Force
      return $true
    } catch {
      Write-Host ""
      Say ("    Download problem (try {0} of {1}): {2}" -f $i, $tries, $_.Exception.Message) Yellow
      if ($i -lt $tries) { Start-Sleep -Seconds (4 * $i) }
    } finally {
      if ($out) { $out.Close() }; if ($in) { $in.Close() }; if ($resp) { $resp.Close() }
      if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
  }
  return $false
}

# ---------------- Python ----------------
$script:ProbePy = @'
import sys, os
tk = 0
try:
    import tkinter
    tkinter.Tcl()
    tk = 1
except Exception:
    pass
venv = 0
try:
    import venv as _v, ensurepip as _e
    venv = 1
except Exception:
    pass
print(sys.executable)
print("%d.%d.%d" % sys.version_info[:3])
print(int(sys.maxsize > 2**32))
print(tk)
print(venv)
print(int(sys.prefix != sys.base_prefix))
'@

function Test-PyExe([string]$exe, [string[]]$pre) {
  $ErrorActionPreference = "Continue"
  try {
    $probe = Join-Path $env:TEMP "appsetup-pyprobe.py"
    Write-Utf8NoBom $probe $script:ProbePy
    $a = @(); if ($pre) { $a += $pre }; $a += $probe
    $out = & $exe @a 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $out) { return $null }
    $l = @($out | ForEach-Object { "$_".Trim() })
    if ($l.Count -lt 6) { return $null }
    if ($l[0] -like "*\WindowsApps\*") { return $null }   # Microsoft Store Python: sandboxed paths, skip
    if ($l[5] -eq "1") { return $null }                    # a virtual environment, not a base install
    if ($l[2] -ne "1") { return $null }                    # 32-bit
    return [pscustomobject]@{ Path = $l[0]; Version = [version]$l[1]; Tk = ($l[3] -eq "1"); Venv = ($l[4] -eq "1") }
  } catch { return $null }
}

function Find-Pythons {
  $seen = @{}; $found = @()
  $cands = @()
  foreach ($v in @("3.13", "3.14", "3.12", "3.15", "3.11", "3.10")) { $cands += ,@("py", @("-$v")) }
  $cands += ,@("py", @("-3")); $cands += ,@("python", @()); $cands += ,@("python3", @())
  $hasPy = [bool](Get-Command py -ErrorAction SilentlyContinue)
  foreach ($c in $cands) {
    if ($c[0] -eq "py" -and -not $hasPy) { continue }
    if ($c[0] -ne "py" -and -not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
    $r = Test-PyExe $c[0] $c[1]
    if ($r -and -not $seen.ContainsKey($r.Path.ToLower())) { $seen[$r.Path.ToLower()] = 1; $found += $r }
  }
  $globs = @("$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe", "$env:SystemDrive\Python3*\python.exe")
  foreach ($g in $globs) {
    foreach ($f in (Get-ChildItem $g -ErrorAction SilentlyContinue)) {
      if ($seen.ContainsKey($f.FullName.ToLower())) { continue }
      $r = Test-PyExe $f.FullName @()
      if ($r -and -not $seen.ContainsKey($r.Path.ToLower())) { $seen[$r.Path.ToLower()] = 1; $found += $r }
    }
  }
  return $found
}

function Select-Python($found, [version]$Min, [switch]$NeedTk, [switch]$NeedVenv) {
  # Preference: 3.13, then 3.14, then 3.12, then anything newer, then anything else that meets the minimum.
  $rank = @{ "3.13" = 0; "3.14" = 1; "3.12" = 2 }
  $ok = @($found | Where-Object { $_.Version -ge $Min -and (-not $NeedTk -or $_.Tk) -and (-not $NeedVenv -or $_.Venv) })
  if (-not $ok) { return $null }
  return ($ok | Sort-Object @{ Expression = { $k = "{0}.{1}" -f $_.Version.Major, $_.Version.Minor; if ($rank.ContainsKey($k)) { $rank[$k] } else { 10 } } }, @{ Expression = { $_.Version }; Descending = $true } | Select-Object -First 1)
}

function Test-PythonInstaller([string]$exe) {
  $h = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
  if ($h -ne $script:PyPin.Sha256) { Say "  The Python download didn't match the expected checksum, so it was not run." Red; return $false }
  $sig = Get-AuthenticodeSignature -LiteralPath $exe
  if ($sig.Status -ne "Valid" -or -not $sig.SignerCertificate -or $sig.SignerCertificate.Subject -notlike "*$($script:PyPin.Signer)*") {
    Say "  The Python download isn't signed by the $($script:PyPin.Signer) ($($sig.Status)), so it was not run." Red; return $false
  }
  return $true
}

function Get-PythonInstaller {
  $exe = Join-Path $env:TEMP ("python-{0}-amd64.exe" -f $script:PyPin.Version)
  if ((Test-Path -LiteralPath $exe) -and ((Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash -eq $script:PyPin.Sha256)) { Say "    Using the Python installer already downloaded to $exe" }
  else {
    if (-not (Save-Download $script:PyPin.Url $exe ("Python " + $script:PyPin.Version))) { return $null }
  }
  if (-not (Test-PythonInstaller $exe)) { Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue; return $null }
  Say "    Checksum and Python Software Foundation signature OK" Green
  return $exe
}

function Install-PinnedPython([string]$SandboxDir) {
  $ErrorActionPreference = "Continue"
  <# Normal mode: per-user install from python.org (no admin): %LOCALAPPDATA%\Programs\Python\Python313.
     -SandboxDir (testing only): still downloads and verifies the full installer, then unpacks the same release's signed
     component MSIs (core, exe, lib, tcltk) into that folder with an administrative extract (msiexec /a), so nothing is
     registered in Windows and the folder can simply be deleted afterwards. #>
  if (-not (Test-Online @("https://www.python.org/"))) {
    Say "  Can't reach python.org. Connect to the internet and run the installer again. Nothing was changed." Red
    return $null
  }
  $exe = Get-PythonInstaller
  if (-not $exe) { return $null }
  if ($SandboxDir) {
    $dl = Join-Path $SandboxDir "_pymsi"; $dest = Join-Path $SandboxDir "python"
    New-Item -ItemType Directory -Force -Path $dl, $dest | Out-Null
    Say "    Test mode: unpacking Python into $dest (administrative extract, nothing is registered in Windows)..."
    $base = $script:PyPin.Url.Substring(0, $script:PyPin.Url.LastIndexOf("/")) + "/amd64/"
    foreach ($m in @("core", "exe", "lib", "tcltk")) {
      $msi = Join-Path $dl "$m.msi"
      if (-not (Save-Download ($base + "$m.msi") $msi "$m.msi")) { return $null }
      $h = (Get-FileHash -LiteralPath $msi -Algorithm SHA256).Hash
      $sig = Get-AuthenticodeSignature -LiteralPath $msi
      if ($h -ne $script:PyPin.Msi[$m] -or $sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "*$($script:PyPin.Signer)*") { Say "    $m.msi failed its checksum or signature check ($($sig.Status))." Red; return $null }
      $q = Start-Process -FilePath "msiexec.exe" -ArgumentList @("/a", "`"$msi`"", "/qn", "TARGETDIR=`"$dest`"") -Wait -PassThru
      if ($q.ExitCode -ne 0) { Say "    Unpacking $m.msi failed (msiexec exit $($q.ExitCode))" Red; return $null }
    }
    Get-ChildItem -LiteralPath $dest -Filter *.msi -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $dl -Recurse -Force -ErrorAction SilentlyContinue
    $py = Join-Path $dest "python.exe"
  } else {
    Say "    Installing Python $($script:PyPin.Version) for your Windows account (about a minute, no admin needed)..."
    $args2 = @("/quiet", "InstallAllUsers=0", "PrependPath=1", "Include_tcltk=1", "Include_launcher=1", "InstallLauncherAllUsers=0",
               "Include_test=0", "Include_doc=0", "Include_dev=0", "Include_debug=0", "Include_symbols=0", "SimpleInstall=1")
    $p = Start-Process -FilePath $exe -ArgumentList $args2 -Wait -PassThru
    if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) { Say "    The Python installer stopped with code $($p.ExitCode)." Red; return $null }
    $mm = $script:PyPin.Version.Split(".")
    $py = Join-Path $env:LOCALAPPDATA ("Programs\Python\Python{0}{1}\python.exe" -f $mm[0], $mm[1])
  }
  if (-not (Test-Path -LiteralPath $py)) { Say "    Python didn't end up where expected ($py)." Red; return $null }
  $r = Test-PyExe $py @()
  if (-not $r) { Say "    The new Python doesn't run ($py)." Red; return $null }
  Say ("    Python {0} ready: {1} (tkinter: {2})" -f $r.Version, $r.Path, $(if ($r.Tk) { "yes" } else { "no" })) Green
  return $r
}

function Resolve-Python([version]$Min, [switch]$NeedTk, [switch]$NeedVenv, [switch]$Force, [string]$SandboxDir,
                        [switch]$AutoYes, [switch]$Quiet, [string]$AppName) {
  <# Returns a python object (Path, Version, Tk, Venv) or $null. Asks before installing anything. #>
  if (-not $Force) {
    $best = Select-Python (Find-Pythons) $Min -NeedTk:$NeedTk -NeedVenv:$NeedVenv
    if ($best) { Say ("  Python {0} found: {1}" -f $best.Version, $best.Path) Green; return $best }
    $why = "Python $Min or newer"; if ($NeedTk) { $why += " (with tkinter)" }
    Say ""; Say "  $AppName needs $why, and it isn't on this PC yet." Yellow
  } else { Say ""; Say "  -ForcePythonInstall: installing Python even if one is already present." Yellow }
  $yes = $AutoYes
  if (-not $yes) {
    if ($Quiet) { Say "  Run the installer without -Quiet (or add -InstallPython) to let it install Python. Nothing was changed." Red; return $null }
    $yes = Ask-YesNo ("Python is needed, install it now? (official python.org Python {0}, about {1} MB download, no admin)" -f $script:PyPin.Version, $script:PyPin.SizeMB) $true
  }
  if (-not $yes) { Say "  OK, nothing was installed. You can install Python 3.12+ from python.org yourself and run this installer again."; return $null }
  return (Install-PinnedPython -SandboxDir $SandboxDir)
}

# Runs a program in this console (so pip and download progress bars show) and returns its exit code.
# Nothing it prints leaks into a function's return value, and paths with spaces are quoted.
function Invoke-Native([string]$exe, [string[]]$argv) {
  $q = @($argv | ForEach-Object { if ($_ -match '[\s"]') { '"' + ($_ -replace '"', '\"') + '"' } else { $_ } })
  $p = Start-Process -FilePath $exe -ArgumentList $q -NoNewWindow -Wait -PassThru
  return $p.ExitCode
}

# ---------------- Microsoft Visual C++ runtime ----------------
function Test-VCRuntime {
  # faster-whisper's CTranslate2 and ONNX Runtime need the Visual C++ 2015-2022 x64 runtime (msvcp140.dll etc.).
  $sys = [Environment]::GetFolderPath("System")
  foreach ($d in @("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll")) { if (-not (Test-Path -LiteralPath (Join-Path $sys $d))) { return $false } }
  return $true
}

function Ensure-VCRuntime([switch]$AutoYes, [switch]$Quiet) {
  if (Test-VCRuntime) { return $true }
  Say ""
  Say "  The Microsoft Visual C++ runtime (needed by the speech engine) isn't on this PC." Yellow
  $yes = $AutoYes
  if (-not $yes) {
    if ($Quiet) { Say "  Run without -Quiet (or add -InstallPython) to let the installer add it." Red; return $false }
    $yes = Ask-YesNo "Install it now? (official Microsoft download, about 25 MB; Windows asks for admin approval once)" $true
  }
  if (-not $yes) { Say "  Skipped. Install 'Microsoft Visual C++ Redistributable 2015-2022 (x64)' from microsoft.com, then run this installer again."; return $false }
  $exe = Join-Path $env:TEMP "vc_redist.x64.exe"
  if (-not (Save-Download "https://aka.ms/vs/17/release/vc_redist.x64.exe" $exe "Visual C++ runtime")) { return $false }
  $sig = Get-AuthenticodeSignature -LiteralPath $exe
  if ($sig.Status -ne "Valid" -or $sig.SignerCertificate.Subject -notlike "*Microsoft Corporation*") { Say "  The download isn't signed by Microsoft ($($sig.Status)), so it was not run." Red; return $false }
  $p = Start-Process -FilePath $exe -ArgumentList @("/install", "/quiet", "/norestart") -Verb RunAs -Wait -PassThru
  Remove-Item -LiteralPath $exe -Force -ErrorAction SilentlyContinue
  if (@(0, 1638, 3010) -notcontains $p.ExitCode -or -not (Test-VCRuntime)) { Say "  The Visual C++ runtime didn't install (code $($p.ExitCode))." Red; return $false }
  Say "  Visual C++ runtime installed" Green
  return $true
}

# ---------------- virtual environment + pip ----------------
function Test-VenvImports([string]$venvPy, [string]$imports) {
  $ErrorActionPreference = "Continue"
  if (-not (Test-Path -LiteralPath $venvPy)) { return $false }
  & $venvPy -c "import $imports" 2>$null | Out-Null
  return ($LASTEXITCODE -eq 0)
}

function Ensure-Venv([string]$basePy, [string]$venvDir, [string]$reqFile, [string]$imports, [string]$label, [string[]]$extraReq) {
  <# Idempotent: an existing venv that already has the same packages and imports cleanly is kept.
     Otherwise the old one is set aside, a new one is built, and the old one is put back if pip fails,
     so a failed or offline download never leaves the app without a working environment. #>
  $ErrorActionPreference = "Continue"
  $venvPy = Join-Path $venvDir "Scripts\python.exe"
  $stamp = Join-Path $venvDir "requirements.installed.txt"
  $want = ((Get-Content -LiteralPath $reqFile) -join "`n") + "`n" + (($extraReq | Where-Object { $_ }) -join "`n") + "`nbase=" + $basePy
  if ((Test-VenvImports $venvPy $imports) -and (Test-Path -LiteralPath $stamp) -and (((Get-Content -LiteralPath $stamp) -join "`n") -eq $want)) {
    Say "  $label packages already installed ($venvDir)" Green; return $venvPy
  }
  if (-not (Test-Online @("https://pypi.org/simple/pip/", "https://files.pythonhosted.org/"))) {
    Say "  Can't reach PyPI (pypi.org) to download the $label packages. Connect to the internet and run the installer again. Nothing was changed." Red
    return $null
  }
  $old = "$venvDir.old"
  if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }
  if (Test-Path -LiteralPath $venvDir) { Rename-Item -LiteralPath $venvDir -NewName (Split-Path $old -Leaf) -ErrorAction SilentlyContinue }
  Say "  Creating a private Python environment for $label ($venvDir)..."
  $rc = Invoke-Native $basePy @("-m", "venv", $venvDir)
  $ok = ($rc -eq 0 -and (Test-Path -LiteralPath $venvPy))
  if ($ok) {
    $env:PIP_DISABLE_PIP_VERSION_CHECK = "1"; $env:PIP_NO_INPUT = "1"; $env:PYTHONUTF8 = "1"
    $ok = $false
    for ($i = 1; $i -le 3 -and -not $ok; $i++) {
      Say "  Downloading and installing the $label packages (try $i of 3)..."
      $pa = @("-m", "pip", "install", "--only-binary=:all:", "--retries", "5", "--timeout", "60", "-r", $reqFile)
      foreach ($x in $extraReq) { if ($x) { $pa += $x } }
      $rc = Invoke-Native $venvPy $pa
      if ($rc -eq 0 -and (Test-VenvImports $venvPy $imports)) { $ok = $true } elseif ($i -lt 3) { Start-Sleep -Seconds (5 * $i) }
    }
  }
  if (-not $ok) {
    Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path -LiteralPath $old) { Rename-Item -LiteralPath $old -NewName (Split-Path $venvDir -Leaf) -ErrorAction SilentlyContinue }
    Say "  Installing the $label packages failed (see the messages above). Check the internet connection and run the installer again." Red
    return $null
  }
  if (Test-Path -LiteralPath $old) { Remove-Item -LiteralPath $old -Recurse -Force -ErrorAction SilentlyContinue }
  Write-Utf8NoBom $stamp $want
  Say "  $label packages installed" Green
  return $venvPy
}

# ---------------- Whisper models ----------------
$script:ModelPy = @'
import os, sys, time
os.environ.pop("HF_HUB_OFFLINE", None)
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
name, models, check_only = sys.argv[1], sys.argv[2], sys.argv[3] == "1"
from faster_whisper import WhisperModel
from faster_whisper.utils import download_model
if not check_only:
    download_model(name, cache_dir=models)
os.environ["HF_HUB_OFFLINE"] = "1"
import numpy as np
m = WhisperModel(name, device="cpu", compute_type="int8", download_root=models, local_files_only=True)
segs, _ = m.transcribe(np.zeros(16000, np.float32), language="en", beam_size=1)
list(segs)
print("MODEL-OK", name)
'@

function Ensure-WhisperModel([string]$venvPy, [string]$modelsDir, [string]$name) {
  $ErrorActionPreference = "Continue"
  $script = Join-Path $env:TEMP "appsetup-whisper-model.py"
  Write-Utf8NoBom $script $script:ModelPy
  New-Item -ItemType Directory -Force -Path $modelsDir | Out-Null
  $out = & $venvPy $script $name $modelsDir "1" 2>$null
  if ($LASTEXITCODE -eq 0 -and ("$out" -match "MODEL-OK")) { Say "  Speech model $name already downloaded" Green; return $true }
  $info = $script:WhisperRepos[$name]; $mb = 500; if ($info) { $mb = $info.SizeMB }
  if (-not (Test-Online @("https://huggingface.co/"))) {
    Say "  Can't reach huggingface.co to download the $name speech model (about $mb MB). Connect to the internet and run the installer again." Red
    return $false
  }
  for ($i = 1; $i -le 3; $i++) {
    Say ("  Downloading the {0} speech model (about {1} MB, try {2} of 3). This can take a few minutes..." -f $name, $mb, $i)
    $rc = Invoke-Native $venvPy @($script, $name, $modelsDir, "0")
    if ($rc -eq 0) { Say "  Speech model $name downloaded and tested" Green; return $true }
    if ($i -lt 3) { Start-Sleep -Seconds (5 * $i) }
  }
  Say "  The $name speech model couldn't be downloaded. Check the internet connection and run the installer again (finished parts are kept)." Red
  return $false
}

# ---------------- shortcuts ----------------
function Normalize-Hotkey([string]$hk) {
  # "Ctrl+Alt+T" and WScript's "Alt+Ctrl+T" are the same key; compare on a sorted form.
  $parts = @($hk -split '\+' | ForEach-Object { $_.Trim().ToLower() } | Where-Object { $_ })
  if (-not $parts) { return "" }
  $key = $parts[-1]; $mods = @($parts[0..($parts.Count - 2)] | Where-Object { $_ } | Sort-Object)
  return (($mods + $key) -join "+")
}

function Find-HotkeyClash([string]$hotkey, [string[]]$dirs, [string[]]$ownLinks) {
  $want = Normalize-Hotkey $hotkey
  $sh = New-Object -ComObject WScript.Shell
  foreach ($d in $dirs) {
    if (-not $d -or -not (Test-Path -LiteralPath $d)) { continue }
    foreach ($f in (Get-ChildItem -LiteralPath $d -Filter *.lnk -Recurse -ErrorAction SilentlyContinue)) {
      if ($ownLinks -contains $f.FullName) { continue }
      try { $h = $sh.CreateShortcut($f.FullName).Hotkey; if ($h -and (Normalize-Hotkey $h) -eq $want) { return $f.FullName } } catch { }
    }
  }
  return $null
}

function Get-ShortcutDirs([string]$DesktopDir, [string]$StartMenuDir) {
  $d = @($DesktopDir, $StartMenuDir, [Environment]::GetFolderPath("Desktop"), [Environment]::GetFolderPath("CommonDesktopDirectory"),
         [Environment]::GetFolderPath("Programs"), [Environment]::GetFolderPath("CommonPrograms"))
  return @($d | Where-Object { $_ } | Select-Object -Unique)
}

function New-AppShortcut([string]$path, [string]$target, [string]$arguments, [string]$workDir, [string]$icon, [string]$hotkey, [string]$desc) {
  $sh = New-Object -ComObject WScript.Shell
  $l = $sh.CreateShortcut($path)
  $l.TargetPath = $target; $l.Arguments = $arguments; $l.WorkingDirectory = $workDir
  if ($icon) { $l.IconLocation = "$icon,0" }
  if ($desc) { $l.Description = $desc }
  if ($hotkey) { $l.Hotkey = $hotkey }
  $l.Save()
}

function Stop-AppProcesses([string]$dir) {
  $procs = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.Name -like "python*" -and $_.CommandLine -and $_.CommandLine -like "*$dir*" }
  foreach ($p in $procs) { try { Stop-Process -Id $p.ProcessId -Force -ErrorAction Stop; Say "  Stopped the running copy (pid $($p.ProcessId))" } catch { } }
  if ($procs) { Start-Sleep -Milliseconds 800 }
}

function Test-PathLength([string]$root) {
  # Python packages add up to ~150 characters under the venv. Windows' classic limit is 260 unless long paths are on.
  $lp = 0; try { $lp = (Get-ItemProperty "HKLM:\SYSTEM\CurrentControlSet\Control\FileSystem" -Name LongPathsEnabled -ErrorAction Stop).LongPathsEnabled } catch { }
  if ($lp -ne 1 -and $root.Length -gt 90) {
    Say "  Note: the install folder path is long ($($root.Length) characters) and Windows long paths are off; package installs may fail." Yellow
    Say "  If that happens, install to a shorter folder with -InstallRoot (for example C:\Apps\MyApp)." Yellow
  }
}
