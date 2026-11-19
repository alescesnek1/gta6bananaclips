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
$launch = ($now.Month -eq 11) -and ($now.Day -ge 18) -and ($now.Day -le 21)

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
        # the live chat watcher (from 18. 11. 00:00): queued in its group, so it runs back to back
        if ($now -ge [datetime]"2026-11-18T00:00:00") {
            Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/aleshrani/gta6bananaclips/actions/workflows/live.yml/dispatches" `
                -Headers @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json"; "User-Agent" = "gta6-pc" } `
                -Body '{"ref":"main"}' -ContentType "application/json" | Out-Null
        }
    } catch {
        "$(Get-Date -Format s) github run FAILED: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
    }
}

# --- 1b) long-form every 2 h, BUILT ON THIS PC (owner 19. 11.: GitHub took 2-4 h per episode; "at tam klidne
# stavi nonstop, ale at se to tam nikde neuklada"): worker\long.ps1 in its own process, one at a time (PID lock).
# It delivers through GitHub (tools/long_pc.py -> long-pc.yml: release link, e-mail, state) and deletes what it built.
if (Due "long" 115) {
    $llock = Join-Path $dir "out\long.lock"
    $busy = $false
    if (Test-Path $llock) {
        $old = (Get-Content $llock -Raw -ErrorAction SilentlyContinue)
        $age = ($now - (Get-Item $llock).LastWriteTime).TotalHours
        $busy = $old -and $age -lt 5 -and (Get-Process -Id ([int]$old.Trim()) -ErrorAction SilentlyContinue)
    }
    if ($busy) {
        "$(Get-Date -Format s) long-form still building (pid $($old.Trim())), skipped" | Out-File $log -Append -Encoding utf8
    } else {
        $lp = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -PassThru `
            -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "long.ps1"))
        $lp.Id | Set-Content $llock -Encoding ascii
        "$(Get-Date -Format s) long-form build started on the PC (pid $($lp.Id))" | Out-File $log -Append -Encoding utf8
    }
}

# --- 1a) hardware once (owner 19. 11.: GPU + VRAM, CPU, RAM of this PC, read from the report): one line per part
$hw = Join-Path $dir "out\hw_reported_v1.txt"
if (-not (Test-Path $hw)) {
    try {
        $cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
        $ram = [math]::Round((Get-CimInstance Win32_ComputerSystem).TotalPhysicalMemory / 1GB, 1)
        $lines = @("$(Get-Date -Format s) HW CPU: $($cpu.Name.Trim()) ($($cpu.NumberOfCores) cores / $($cpu.NumberOfLogicalProcessors) threads)",
                   "$(Get-Date -Format s) HW RAM: $ram GB")
        $smi = Get-Command nvidia-smi -ErrorAction SilentlyContinue
        if ($smi) {
            & nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader | ForEach-Object { $lines += "$(Get-Date -Format s) HW GPU (nvidia-smi): $_" }
        }
        Get-ItemProperty "HKLM:\SYSTEM\ControlSet001\Control\Class\{4d36e968-e325-11ce-bfc1-08002be10318}\0*" -ErrorAction SilentlyContinue |
            Where-Object { $_.DriverDesc } | ForEach-Object {
                $vram = if ($_."HardwareInformation.qwMemorySize") { [math]::Round([double]$_."HardwareInformation.qwMemorySize" / 1GB, 1) } else { "?" }
                $lines += "$(Get-Date -Format s) HW GPU: $($_.DriverDesc) VRAM $vram GB"
            }
        $lines | Out-File $log -Append -Encoding utf8
        "done" | Set-Content $hw -Encoding ascii
    } catch {
        "$(Get-Date -Format s) HW read FAILED: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
    }
}

# --- 1c) report in every 30 min (18. 11.: nobody could see what the PC does without going to it): the tail
# of worker.log as a dispatch input of pc-hlaseni.yml (the token can start workflows, not push). Public repo:
# long token-like strings are masked, at most 15000 characters.
if ($token -and (Test-Path $log) -and (Due "hlaseni" 28)) {
    try {
        $tail = (Get-Content $log -Tail 80 -ErrorAction Stop) -join "`n"
        # NUL bytes out first: lines written as UTF-16 (PS 5.1 "*>>") read back as text again (19. 11.: "????")
        $tail = $tail -replace "`0", '' -replace '[A-Za-z0-9_\-]{32,}', '***' -replace '[^\x09\x0A\x20-\x7E]', '?'
        if ($tail.Length -gt 15000) { $tail = $tail.Substring($tail.Length - 15000) }
        $body = @{ ref = "main"; inputs = @{ log = "$(Get-Date -Format s) $env:COMPUTERNAME`n$tail" } } | ConvertTo-Json -Compress
        Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/aleshrani/gta6bananaclips/actions/workflows/pc-hlaseni.yml/dispatches" `
            -Headers @{ Authorization = "Bearer $token"; Accept = "application/vnd.github+json"; "User-Agent" = "gta6-pc" } `
            -Body $body -ContentType "application/json" | Out-Null
    } catch {
        "$(Get-Date -Format s) report FAILED: $($_.Exception.Message)" | Out-File $log -Append -Encoding utf8
    }
}

# --- 2) YouTube, every 30 min, in its OWN process (worker\youtube.ps1): a YouTube render can take up to
# 2 h and the task runs with MultipleInstances IgnoreNew - in this process it stopped the clock above.
# Its own lock file (PID) makes sure only one YouTube batch runs at a time.
if (-not (Due "youtube" 25)) { exit 0 }
$ylock = Join-Path $dir "out\youtube.lock"
if (Test-Path $ylock) {
    $old = (Get-Content $ylock -Raw -ErrorAction SilentlyContinue)
    $age = ($now - (Get-Item $ylock).LastWriteTime).TotalHours
    if ($old -and $age -lt 3 -and (Get-Process -Id ([int]$old.Trim()) -ErrorAction SilentlyContinue)) {
        "$(Get-Date -Format s) youtube batch still running (pid $($old.Trim())), skipped" | Out-File $log -Append -Encoding utf8
        exit 0
    }
}
$p = Start-Process -FilePath "powershell.exe" -WindowStyle Hidden -PassThru `
    -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", (Join-Path $PSScriptRoot "youtube.ps1"))
$p.Id | Set-Content $ylock -Encoding ascii
"$(Get-Date -Format s) youtube batch started (pid $($p.Id))" | Out-File $log -Append -Encoding utf8
