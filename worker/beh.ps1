# gta6bananaclips 24/7 PC - one run (Task Scheduler starts it every 5 min).
#  1. CLOCK: asks GitHub to start the 'denne-klipy' run (GitHub's own schedule is unreliable for
#     public repos - it ran every 4-7 h instead of 30 min). Every 5 min on launch days
#     (19.-21. 11.), otherwise every 30 min. Needs GITHUB_TOKEN in .env (fine-grained,
#     this repo only, Actions: read and write). The PC renders nothing for Twitch/Kick.
#  2. YOUTUBE (every 30 min): YouTube blocks GitHub's servers, so this PC (home connection)
#     finds the YouTube moments, renders them and puts them on a free Netlify draft deploy
#     ("yt-inbox"); the GitHub run picks them up from there.
# ASCII only: Windows PowerShell 5.1 reads scripts without BOM as ANSI.

$dir = Split-Path $PSScriptRoot -Parent
# runs without anyone logged in (S4U task): PATH from the registry + the venv's tools (yt-dlp)
$env:Path = (Join-Path $dir ".venv\Scripts") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
Set-Location $dir
$log = Join-Path $dir "out\worker.log"
New-Item -ItemType Directory -Force (Split-Path $log) | Out-Null
if ((Test-Path $log) -and (Get-Item $log).Length -gt 5MB) { Move-Item $log "$log.old" -Force }

$env:KLIPY_STATE = Join-Path $dir "worker_state.json"   # own memory: the checkout is reset below
$env:PYTHONUTF8 = "1"
$py = Join-Path $dir ".venv\Scripts\python.exe"
$now = Get-Date
$launch = ($now.Month -eq 11) -and ($now.Day -ge 19) -and ($now.Day -le 21)

# --- 1) clock for the GitHub run
# "is it time?" by the time of the last run (not by the clock minute: the task may run at :15/:45)
function Due($name, $minutes) {
    $f = Join-Path $dir "out\last_$name.txt"
    if (Test-Path $f) {
        $last = [datetime]::Parse((Get-Content $f -Raw).Trim(), [Globalization.CultureInfo]::InvariantCulture)
        if (($now - $last).TotalMinutes -lt $minutes) { return $false }
    }
    $now.ToString("s") | Set-Content $f -Encoding ascii
    return $true
}
$token = (Get-Content (Join-Path $dir ".env") -ErrorAction SilentlyContinue |
          Where-Object { $_ -match '^GITHUB_TOKEN=(.+)$' } | ForEach-Object { $Matches[1].Trim() }) | Select-Object -First 1
if (-not $token) { "$(Get-Date -Format s) no GITHUB_TOKEN in .env - clock off" | Out-File $log -Append -Encoding utf8 }
if ($token -and (Due "github" $(if ($launch) { 4 } else { 25 }))) {
    try {
        Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/aleshrani/gta6bananaclips/actions/workflows/denne.yml/dispatches" `
            -Headers @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json"; "User-Agent" = "gta6-pc" } `
            -Body '{"ref":"main"}' -ContentType "application/json" | Out-Null
        "$(Get-Date -Format s) github run started" | Out-File $log -Append -Encoding utf8
    } catch {
        "$(Get-Date -Format s) github run FAILED: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
    }
}

# --- 2) YouTube, every 30 min
if (-not (Due "youtube" 25)) { exit 0 }
& {
    "=== $(Get-Date -Format s)"
    # always the newest code and publikovano.json from GitHub (local changes to tracked files are dropped)
    git fetch -q origin main
    git reset -q --hard origin/main
    & $py klipuj.py --whisper small.en batch --jen-yt --yt 3 --max 3
    & $py publikuj.py schranka --uklid
} *>> $log
