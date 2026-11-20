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
# ffmpeg failed on the non-ASCII home folder: render (and temp files) into an ASCII folder
$env:KLIPY_OUT = "C:\Users\Public\gta6-out"
$env:TEMP = "C:\Users\Public\gta6-out\tmp"; $env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
$py = Join-Path $dir ".venv\Scripts\python.exe"
$now = Get-Date
try {
    & {
        "=== youtube $(Get-Date -Format s)"
        # always the newest code and publikovano.json from GitHub (local changes to tracked files are dropped)
        git fetch -q origin main
        git reset -q --hard origin/main
        # Pillow was missing on the PC (19. 11.: long-form crash "No module named 'PIL'", league chip off)
        & $py -c "import PIL" 2>$null
        if ($LASTEXITCODE -ne 0) { & $py -m pip install -q "Pillow==13.0.0" }
        # before the launch (19. 11.) trailer reactions up to 7 days old, after it only fresh videos
        $days = if ($now -lt [datetime]"2026-11-19") { 7 } else { 1 }
        & $py klipuj.py --whisper small.en batch --jen-yt --yt 3 --max 3 --days $days
        & $py publikuj.py schranka --uklid
    } *>&1 | Out-File $log -Append -Encoding utf8   # PS 5.1 "*>>" wrote UTF-16 and the report read "????"
} finally {
    Remove-Item $ylock -Force -ErrorAction SilentlyContinue
}
