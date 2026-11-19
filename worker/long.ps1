# gta6bananaclips 24/7 PC - the long-form part, started by beh.ps1 as its own process every ~2 h (owner 19. 11.:
# "at tam klidne stavi nonstop, ale at se to tam nikde neuklada"). Only one runs at a time (out\long.lock = its PID).
# kompilace.py decides per brand (cap long_min_gap_h, enough strong material, else a silent skip); a finished
# episode goes to GitHub via tools/long_pc.py pack (Netlify draft -> long-pc.yml: release link, e-mail, state),
# then EVERYTHING this run built is deleted - videos, sources, thumbnails, state copies.
# ASCII only: Windows PowerShell 5.1 reads scripts without BOM as ANSI.

$dir = Split-Path $PSScriptRoot -Parent
$env:Path = (Join-Path $dir ".venv\Scripts") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")
Set-Location $dir
$log = Join-Path $dir "out\worker.log"
$llock = Join-Path $dir "out\long.lock"
$PID | Set-Content $llock -Encoding ascii
$env:PYTHONUTF8 = "1"
# ffmpeg / OpenCV fail on the non-ASCII home folder: everything this run writes goes to an ASCII folder
$work = "C:\Users\Public\gta6-long"
$env:KLIPY_OUT = $work
$env:TEMP = Join-Path $work "tmp"; $env:TMP = $env:TEMP
New-Item -ItemType Directory -Force $env:TEMP | Out-Null
# own copies of the long-form state: youtube.ps1 resets the checkout every 30 min (git reset --hard), which
# would roll these files back in the middle of a 1-2 h build
$st = Join-Path $work "state"
New-Item -ItemType Directory -Force $st | Out-Null
$env:LONG_USED = Join-Path $st "long_used.json"
$env:LONG_EPISODES = Join-Path $st "long_episodes.json"
$env:LONG_RESERVE = Join-Path $st "long_reserve.json"
$env:LONG_VISION = Join-Path $st "long_vision.json"
$env:KLIPY_POOL = Join-Path $st "long_pool.json"
$py = Join-Path $dir ".venv\Scripts\python.exe"
try {
    & {
        "=== long-form $(Get-Date -Format s)"
        git fetch -q origin main
        git reset -q --hard origin/main
        foreach ($f in "long_used.json", "long_episodes.json", "long_reserve.json", "long_vision.json", "long_pool.json") {
            if (Test-Path (Join-Path $dir $f)) { Copy-Item (Join-Path $dir $f) (Join-Path $st $f) -Force }
        }
        $since = [DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - 5
        foreach ($z in "bananas", "fanpage") {
            $env:LONG_BRANDS_AFTER = $(if ($z -eq "bananas") { 1 } else { 0 })
            & $py kompilace.py --znacka $z
        }
        & $py tools\long_pc.py pack --since $since
    } *>> $log
} finally {
    # nothing stays on the PC (the video is on Netlify / GitHub by now, or it failed and the next run builds again)
    foreach ($p in (Join-Path $work "_long"), (Join-Path $work "_pool"), $env:TEMP, $st) {
        Remove-Item $p -Recurse -Force -ErrorAction SilentlyContinue
    }
    Remove-Item $llock -Force -ErrorAction SilentlyContinue
}
