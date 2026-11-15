# gta6bananaclips YouTube worker - one-time install from the USB stick.
# Run SPUST.cmd on a Windows PC with a home internet connection. Installs git, Python 3.12,
# ffmpeg and deno (winget), clones the public repo to %USERPROFILE%\gta6-yt, sets up the
# Python environment and a scheduled task every 30 minutes (only while you are logged in).
# Copies .env (NETLIFY_AUTH_TOKEN, NETLIFY_SITE_ID) and youtube_cookies.txt from the stick.
# Safe to run again (updates everything). ASCII only (Windows PowerShell 5.1).

$ErrorActionPreference = "Stop"
$usb = $PSScriptRoot
$dir = Join-Path $env:USERPROFILE "gta6-yt"
$repo = "https://github.com/aleshrani/gta6bananaclips.git"

function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}
function Need($cmd, $id) {
    if (Get-Command $cmd -ErrorAction SilentlyContinue) { return }
    Write-Host "instaluji $id ..."
    winget install -e --id $id --accept-source-agreements --accept-package-agreements --silent
    Refresh-Path
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "$cmd se nenainstaloval ($id)" }
}

if (-not (Test-Path (Join-Path $usb ".env"))) { throw "na flashce chybi .env (NETLIFY_AUTH_TOKEN, NETLIFY_SITE_ID)" }
if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { throw "chybi winget (App Installer z Microsoft Store)" }

Need git Git.Git
$has312 = $false
if (Get-Command py -ErrorAction SilentlyContinue) { cmd /c "py -3.12 -c pass >nul 2>&1"; $has312 = ($LASTEXITCODE -eq 0) }
if (-not $has312) {
    Write-Host "instaluji Python 3.12 ..."
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements --silent
    Refresh-Path
}
Need ffmpeg Gyan.FFmpeg
Need deno DenoLand.Deno

Write-Host "kod -> $dir"
if (Test-Path (Join-Path $dir ".git")) {
    git -C $dir fetch -q origin main
    git -C $dir reset -q --hard origin/main
} else {
    git clone -q $repo $dir
}
Copy-Item (Join-Path $usb ".env") (Join-Path $dir ".env") -Force
$cookies = Join-Path $usb "youtube_cookies.txt"
if (Test-Path $cookies) { Copy-Item $cookies (Join-Path $dir "youtube_cookies.txt") -Force }

Write-Host "python prostredi (torch ~2-3 GB, chvili to trva) ..."
$py = Join-Path $dir ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { py -3.12 -m venv (Join-Path $dir ".venv") }
& $py -m pip install -q --upgrade pip
$gpu = [bool](Get-Command nvidia-smi -ErrorAction SilentlyContinue)
$torch = if ($gpu) { "https://download.pytorch.org/whl/cu128" } else { "https://download.pytorch.org/whl/cpu" }
& $py -m pip install -q torch --index-url $torch
& $py -m pip install -q -r (Join-Path $dir "requirements-ci.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install selhal" }

Write-Host "naplanovana uloha gta6-yt (kazdych 30 min) ..."
$beh = Join-Path $dir "worker\beh.ps1"
$act = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$beh`""
$trig = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) `
    -RepetitionInterval (New-TimeSpan -Minutes 30) -RepetitionDuration (New-TimeSpan -Days 3650)
$boot = New-ScheduledTaskTrigger -AtStartup                  # and right after every restart
$boot.Delay = "PT2M"
$set = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -WakeToRun `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
# 24/7: runs whether anyone is logged in or not (S4U = no password stored); needs admin (SPUST.cmd asks)
$who = "$env:USERDOMAIN\$env:USERNAME"
try {
    $pr = New-ScheduledTaskPrincipal -UserId $who -LogonType S4U -RunLevel Limited
    Register-ScheduledTask -TaskName "gta6-yt" -Action $act -Trigger @($trig, $boot) -Settings $set -Principal $pr -Force -ErrorAction Stop | Out-Null
    $mode = "i bez prihlaseni a po restartu"
} catch {
    Register-ScheduledTask -TaskName "gta6-yt" -Action $act -Trigger @($trig, $boot) -Settings $set -Force | Out-Null
    $mode = "JEN kdyz je uzivatel prihlaseny (spust SPUST.cmd jako spravce pro rezim 24/7)"
}

Write-Host ""
Write-Host "HOTOVO. Vse je zkopirovane v $dir - flashku muzes vytahnout."
Write-Host "Prvni beh za minutu, pak kazdych 30 min, $mode."
Write-Host "Pocitac nesmi usinat: Nastaveni > Napajeni > Rezim spanku = Nikdy."
Write-Host "Log: $dir\out\worker.log"
Write-Host "Vypnout: Unregister-ScheduledTask gta6-yt"
