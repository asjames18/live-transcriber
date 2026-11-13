@echo off
setlocal
rem Live Transcriber installer: double-click this file. No admin rights needed.
rem If it was opened straight from inside the zip (Windows only pulls out this one file),
rem it finds the downloaded zip, extracts it for you and carries on.
set "APPSETUP_HERE=%~dp0"
set "APPSETUP_SELF=%~f0"
set "APPSETUP_UNPACK=%TEMP%\LiveTranscriber-setup-unpacked"
set "APPSETUP_PS=%~dp0installer\Install-LiveTranscriber.ps1"
set "APPSETUP_QUIET="
echo %* | findstr /i /c:"-Quiet" >nul && set "APPSETUP_QUIET=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$s=[IO.File]::ReadAllText($env:APPSETUP_SELF); $i=$s.LastIndexOf('#'+'PS-BOOT'); Invoke-Expression $s.Substring($i)"
set "APPSETUP_RC=%errorlevel%"
if "%APPSETUP_RC%"=="10" set "APPSETUP_PS=%APPSETUP_UNPACK%\pkg\installer\Install-LiveTranscriber.ps1"
if not "%APPSETUP_RC%"=="0" if not "%APPSETUP_RC%"=="10" goto failed
if not exist "%APPSETUP_PS%" goto failed
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%APPSETUP_PS%" %*
set "APPSETUP_RC=%errorlevel%"
exit /b %APPSETUP_RC%
:failed
if not defined APPSETUP_QUIET pause
exit /b 1

#PS-BOOT
# Runs before the real installer: checks that PowerShell scripts may run, unblocks the files, and unpacks the zip when needed.
# Exit 0 = run the installer next to this file, 10 = run the unpacked copy in %TEMP%, anything else = stop.
$ErrorActionPreference = 'Stop'
$quiet = [bool]$env:APPSETUP_QUIET
function Show-Msg($text) {
  Write-Host ''; Write-Host $text
  if ($quiet) { return }
  try { Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show($text, 'Live Transcriber setup', 'OK', 'Information') } catch { }
}
try {
  foreach ($scope in 'MachinePolicy', 'UserPolicy') {
    $p = Get-ExecutionPolicy -Scope $scope
    if ($p -eq 'AllSigned' -or $p -eq 'Restricted') {
      Show-Msg ("Your organization's Group Policy ($scope = $p) blocks PowerShell scripts that aren't code-signed, so this installer can't run on this PC.`r`n`r`nAsk your IT team to allow it, or install on a personal PC.")
      exit 3
    }
  }
  $here = $env:APPSETUP_HERE.TrimEnd('\')
  if (Test-Path -LiteralPath (Join-Path $here 'installer\Install-LiveTranscriber.ps1')) {
    # Normal case (extracted zip): clear the downloaded-from-the-internet mark so Windows doesn't block the scripts.
    Get-ChildItem -LiteralPath $here -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue
    exit 0
  }
  Write-Host ''; Write-Host '  Live Transcriber setup: unpacking the download first...'
  $name = Split-Path $here -Leaf
  if ($name -notlike 'LiveTranscriber-Setup-*') { $name = 'LiveTranscriber-Setup-*' }
  $dirs = @()
  if ($env:APPSETUP_SEARCH) { $dirs += $env:APPSETUP_SEARCH }
  try { $dirs += (New-Object -ComObject Shell.Application).NameSpace('shell:Downloads').Self.Path } catch { }
  $dirs += (Join-Path $env:USERPROFILE 'Downloads'), [Environment]::GetFolderPath('Desktop'), [Environment]::GetFolderPath('MyDocuments')
  $dirs = $dirs | Where-Object { $_ -and (Test-Path -LiteralPath $_) } | Select-Object -Unique
  $zip = Get-ChildItem -Path $dirs -Filter ($name + '.zip') -File -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
  if (-not $zip) {
    Show-Msg ("Windows opened this installer from inside the zip, so the rest of the files aren't here.`r`n`r`nRight-click the downloaded LiveTranscriber-Setup zip, choose 'Extract All...', then double-click Install-LiveTranscriber.cmd in the folder that opens.")
    exit 1
  }
  $dest = $env:APPSETUP_UNPACK
  if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
  $x = Join-Path $dest '_x'
  Expand-Archive -LiteralPath $zip.FullName -DestinationPath $x -Force
  $ps = Get-ChildItem -LiteralPath $x -Recurse -Filter 'Install-LiveTranscriber.ps1' -File | Where-Object { $_.Directory.Name -eq 'installer' } | Select-Object -First 1
  if (-not $ps) { Show-Msg ("The download looks incomplete. Please download LiveTranscriber-Setup again and run Install-LiveTranscriber.cmd."); exit 1 }
  Move-Item -LiteralPath $ps.Directory.Parent.FullName -Destination (Join-Path $dest 'pkg')
  Remove-Item -LiteralPath $x -Recurse -Force -ErrorAction SilentlyContinue
  Get-ChildItem -LiteralPath $dest -Recurse -File | Unblock-File -ErrorAction SilentlyContinue
  Write-Host ('  Found ' + $zip.Name + ' and unpacked it.')
  exit 10
} catch {
  Show-Msg ("Couldn't get the installer ready:`r`n" + $_.Exception.Message + "`r`n`r`nRight-click the zip, choose 'Extract All...', then run Install-LiveTranscriber.cmd from the extracted folder.")
  exit 1
}
