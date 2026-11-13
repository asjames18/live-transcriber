<#
  Live Transcriber installer (no admin needed). Run it with Install-LiveTranscriber.cmd (double-click).
  Installs to %LOCALAPPDATA%\LiveTranscriber:
    app\     the code
    venv\    a private Python environment with faster-whisper and the audio packages (nothing goes into the global Python)
    models\  the Whisper speech model(s), downloaded from Hugging Face
    data\    your transcripts, notes and settings (never touched by a reinstall)
  If Python 3.12+ isn't on the PC it offers to install the official python.org Python (per-user) first.
  Running it again is safe: it repairs or upgrades and skips anything already in place.

  Test / sandbox example (touches nothing real):
    Install-LiveTranscriber.cmd -InstallRoot "%TEMP%\LTTest" -DesktopDir "%TEMP%\LTTest\desk" -StartMenuDir "%TEMP%\LTTest\start"
      -ShortcutName "Live Transcriber TEST" -NoHotkey -NoLaunch -Quiet -NoGpu [-ForcePythonInstall -PythonDir "%TEMP%\LTTest\py"]
#>
param(
  [string]$InstallRoot = (Join-Path $env:LOCALAPPDATA "LiveTranscriber"),
  [string]$ShortcutName = "Live Transcriber",
  [string]$DesktopDir = [Environment]::GetFolderPath("Desktop"),
  [string]$StartMenuDir = (Join-Path $env:APPDATA "Microsoft\Windows\Start Menu\Programs"),
  [string]$Hotkey = "Ctrl+Alt+L",
  [switch]$NoHotkey,
  [switch]$NoLaunch,
  [switch]$Quiet,
  [switch]$Gpu,
  [switch]$NoGpu,
  [switch]$InstallPython,
  [switch]$ForcePythonInstall,
  [string]$PythonDir = ""
)
$ErrorActionPreference = "Stop"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Here "SetupCommon.ps1")
Initialize-Net
$PkgRoot = Split-Path -Parent $Here
$Src = Join-Path $PkgRoot "app"
$InstallRoot = [IO.Path]::GetFullPath($InstallRoot)
$AppDir = Join-Path $InstallRoot "app"
$DataDir = Join-Path $InstallRoot "data"
$VenvDir = Join-Path $InstallRoot "venv"
$ModelsDir = Join-Path $InstallRoot "models"
$T0 = Get-Date
function Finish($code) {
  $secs = [int]((Get-Date) - $T0).TotalSeconds
  Say ("  (took {0} min {1} s)" -f [math]::Floor($secs / 60), ($secs % 60))
  if (-not $Quiet) { Write-Host ""; Read-Host "Press Enter to close" | Out-Null }
  exit $code
}

Say ""
Say "  Live Transcriber setup" Cyan
Say "  ----------------------"
if (-not (Test-Path -LiteralPath (Join-Path $Src "transcriber.pyw"))) {
  Say "  Can't find the app files next to this installer." Red
  Say "  Right-click the downloaded zip, choose 'Extract All...', then run Install-LiveTranscriber.cmd from the extracted folder."
  Finish 1
}
$Version = "?"
$m = Select-String -LiteralPath (Join-Path $Src "transcriber.pyw") -Pattern '^VERSION = "([^"]+)"' | Select-Object -First 1
if ($m) { $Version = $m.Matches[0].Groups[1].Value }
Say "  Version $Version  ->  $InstallRoot"
Say "  Needs: Python 3.12+, about 90 MB of speech packages and the 465 MB Whisper model (about 1 GB on disk, 3-10 minutes)."
Test-PathLength $InstallRoot

# ---- 1. GPU option (asked first so all questions come before the downloads) ----
$hasNvidia = $false
try { $hasNvidia = [bool](Get-CimInstance Win32_VideoController -ErrorAction Stop | Where-Object { $_.Name -like "*NVIDIA*" }) } catch { }
$wantGpu = $false
if ($Gpu) { $wantGpu = $true }
elseif (-not $NoGpu -and $hasNvidia) {
  Say ""
  Say "  An NVIDIA graphics card was found. The optional GPU speed-up uses it for the more accurate large-v3-turbo model."
  Say "  It adds about 2 GB of NVIDIA libraries and a 1.55 GB model (about 4 GB more disk). Without it, the CPU model works fine."
  $wantGpu = Ask-YesNo "Add the GPU speed-up?" $false -Quiet:$Quiet
}
$needMB = 1500; if ($wantGpu) { $needMB = 6000 }
if (-not (Assert-FreeSpace $InstallRoot $needMB "Live Transcriber")) { Finish 1 }

# ---- 2. Python ----
$py = Resolve-Python -Min ([version]"3.12") -NeedVenv -Force:$ForcePythonInstall -SandboxDir $PythonDir -AutoYes:($InstallPython -or $ForcePythonInstall) -Quiet:$Quiet -AppName "Live Transcriber"
if (-not $py) { Say "  Live Transcriber needs Python. Nothing was installed." Yellow; Finish 2 }

# ---- 3. copy the app ----
Stop-AppProcesses $InstallRoot
New-Item -ItemType Directory -Force -Path $AppDir, $DataDir, $ModelsDir | Out-Null
Copy-Item -Path (Join-Path $Src "*") -Destination $AppDir -Recurse -Force
foreach ($f in @("Uninstall-LiveTranscriber.ps1", "SetupCommon.ps1")) { Copy-Item -LiteralPath (Join-Path $Here $f) -Destination (Join-Path $InstallRoot $f) -Force }
Get-ChildItem -LiteralPath $AppDir -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
Say "  App copied to $AppDir" Green

# ---- 4. packages + models ----
if (-not (Ensure-VCRuntime -AutoYes:$InstallPython -Quiet:$Quiet)) { Say "  Live Transcriber's speech engine needs the Visual C++ runtime. Install it, then run this installer again." Red; Finish 3 }
$extra = @(); if ($wantGpu) { $extra = @("-r", (Join-Path $AppDir "requirements-gpu.txt")) }
$vpy = Ensure-Venv $py.Path $VenvDir (Join-Path $AppDir "requirements.txt") "faster_whisper, ctranslate2, av, numpy, pyaudiowpatch" "speech" $extra
if (-not $vpy) { Say "  Live Transcriber can't run without its speech packages. Run the installer again when you're online." Red; Finish 3 }
if (-not (Ensure-WhisperModel $vpy $ModelsDir "small.en")) { Say "  Live Transcriber can't transcribe without the speech model. Run the installer again when you're online." Red; Finish 3 }
if ($wantGpu) {
  if (-not (Ensure-WhisperModel $vpy $ModelsDir "large-v3-turbo")) { Say "  The GPU model didn't download; the app will use the CPU model until you run the installer again." Yellow }
}
$info = [ordered]@{ data_dir = "..\data"; models_dir = "..\models"; venv = "..\venv"; version = $Version; installed = (Get-Date -Format s); base_python = $py.Path; gpu = $wantGpu }
Write-Utf8NoBom (Join-Path $AppDir "installed.json") ($info | ConvertTo-Json)
Say "  Your transcripts and settings live in $DataDir"

# ---- 5. check it starts ----
Say "  Checking the speech engine..."
$env:TX_DEVICE = $(if ($wantGpu) { "" } else { "cpu" })
$ErrorActionPreference = "Continue"
$st = & $vpy (Join-Path $AppDir "transcriber.pyw") --self-test 2>$null | Select-Object -Last 1
$ErrorActionPreference = "Stop"
Remove-Item Env:\TX_DEVICE -ErrorAction SilentlyContinue
if ("$st" -match '"ok": true') { Say "  Speech engine OK" Green } else { Say "  Self-test didn't pass: $st" Yellow }

# ---- 6. shortcuts ----
$PyW = Join-Path $VenvDir "Scripts\pythonw.exe"
$Ico = Join-Path $AppDir "transcriber.ico"
$appArgs = '"' + (Join-Path $AppDir "transcriber.pyw") + '"'
New-Item -ItemType Directory -Force -Path $DesktopDir, $StartMenuDir | Out-Null
function Get-LinkPath($dir, $name) {
  $p = Join-Path $dir "$name.lnk"
  if (Test-Path -LiteralPath $p) {
    try { $l = (New-Object -ComObject WScript.Shell).CreateShortcut($p); if (($l.Arguments + $l.TargetPath) -notlike "*$InstallRoot*") { $p = Join-Path $dir "$name (installed).lnk" } } catch { }
  }
  return $p
}
$desk = Get-LinkPath $DesktopDir $ShortcutName
$start = Get-LinkPath $StartMenuDir $ShortcutName
$hot = $null
if (-not $NoHotkey) {
  $taken = Find-HotkeyClash $Hotkey (Get-ShortcutDirs $DesktopDir $StartMenuDir) @($desk, $start)
  if ($taken) { Say "  Note: '$(Split-Path $taken -Leaf)' already uses $Hotkey, so the new shortcut has no hotkey." Yellow } else { $hot = $Hotkey }
}
New-AppShortcut $desk $PyW $appArgs $AppDir $Ico $hot "Live Transcriber: on-device speech to text"
New-AppShortcut $start $PyW $appArgs $AppDir $Ico $null "Live Transcriber: on-device speech to text"
$unArgs = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $InstallRoot "Uninstall-LiveTranscriber.ps1") + '" -InstallRoot "' + $InstallRoot + '" -ShortcutName "' + $ShortcutName + '" -DesktopDir "' + $DesktopDir + '" -StartMenuDir "' + $StartMenuDir + '"'
New-AppShortcut (Join-Path $StartMenuDir "Uninstall $ShortcutName.lnk") "powershell.exe" $unArgs $InstallRoot $Ico $null "Uninstall Live Transcriber"
$hotMsg = ""; if ($hot) { $hotMsg = " (hotkey $hot)" }
Say ("  Shortcuts: Desktop + Start menu" + $hotMsg) Green

if (-not $NoLaunch) { Start-Process -FilePath $PyW -ArgumentList $appArgs -WorkingDirectory $AppDir; Say "  Opening Live Transcriber." Cyan }
"installed $Version $(Get-Date -Format s) python=$($py.Path) gpu=$wantGpu" | Add-Content -LiteralPath (Join-Path $DataDir "install.log")
Say ""
Say "  Done!" Green
Finish 0
