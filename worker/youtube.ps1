# gta6bananaclips 24/7 PC - the YouTube part, started by beh.ps1 as its own process (so a long render
# never blocks the 5-min clock). Only one runs at a time (out\youtube.lock = its PID, beh.ps1 checks it).
# YouTube blocks GitHub's servers, so this PC (home connection) finds the YouTube moments, renders them
# and puts them on a free Netlify draft deploy ("yt-inbox"); the GitHub run picks them up from there.
# ASCII only: Windows PowerShell 5.1 reads scripts without BOM as ANSI.

$dir = Split-Path $PSScriptRoot -Parent
$env:Path = (Join-Path $dir ".venv\Scripts") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
Set-Location $dir
$log = Join-Path $dir "out\worker.log"
$ylock = Join-Path $dir "out\youtube.lock"
$PID | Set-Content $ylock -Encoding ascii
$env:KLIPY_STATE = Join-Path $dir "worker_state.json"   # own memory: the checkout is reset below
$env:PYTHONUTF8 = "1"
$py = Join-Path $dir ".venv\Scripts\python.exe"
$now = Get-Date
try {
    & {
        "=== youtube $(Get-Date -Format s)"
        # always the newest code and publikovano.json from GitHub (local changes to tracked files are dropped)
        git fetch -q origin main
        git reset -q --hard origin/main
        # before the launch (19. 11.) trailer reactions up to 7 days old, after it only fresh videos
        $days = if ($now -lt [datetime]"2026-11-19") { 7 } else { 1 }
        & $py klipuj.py --whisper small.en batch --jen-yt --yt 3 --max 3 --days $days
        & $py publikuj.py schranka --uklid
    } *>> $log
} finally {
    Remove-Item $ylock -Force -ErrorAction SilentlyContinue
}
