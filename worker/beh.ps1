# gta6bananaclips YouTube worker - one run (Task Scheduler starts it every 30 min).
# YouTube blocks GitHub's servers, so this PC (home connection) finds the YouTube moments,
# renders them and puts them on a free Netlify draft deploy ("yt-inbox"). The GitHub run
# picks them up from there and publishes them together with Twitch + Kick.
# ASCII only: Windows PowerShell 5.1 reads scripts without BOM as ANSI.

# runs without anyone logged in (S4U task): take PATH from the registry so git/ffmpeg/deno are found
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
$dir = Split-Path $PSScriptRoot -Parent
Set-Location $dir
$log = Join-Path $dir "out\worker.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item $log "$log.old" -Force }

$env:KLIPY_STATE = Join-Path $dir "worker_state.json"   # own memory: the checkout is reset below
$env:PYTHONUTF8 = "1"
$py = Join-Path $dir ".venv\Scripts\python.exe"

& {
    "=== $(Get-Date -Format s)"
    # always the newest code and publikovano.json from GitHub (local changes to tracked files are dropped)
    git fetch -q origin main
    git reset -q --hard origin/main
    & $py klipuj.py --whisper small.en batch --jen-yt --yt 3 --max 3
    & $py publikuj.py schranka --uklid
} *>> $log
