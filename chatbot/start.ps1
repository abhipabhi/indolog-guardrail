# Starts the guardrail test chatbot on Windows, with its settings read from one private file.
#
#   start.bat         double-click it, or type it in Command Prompt
#   .\start.ps1       in PowerShell
#
# What start.sh does on a Mac or in Linux, for Windows without WSL:
#
#   * The first run installs the app, into .venv in this folder, with uv (https://docs.astral.sh/uv):
#     `uv sync` installs the chatbot and the guardrail beside it (..\guardrail) at the versions in
#     uv.lock, with Python 3.11, which uv brings when this machine has none. After an update that
#     changes uv.lock, the next start brings .venv up to date.
#   * The settings file is %USERPROFILE%\.guardrail-chatbot.env: NAME=value lines, as .env.example
#     shows. It is outside the repository, so a key in it cannot be committed, and in your own
#     profile folder, which other users of this computer cannot open. The first run asks for your
#     Gemini key without showing what you type. It is the only place settings come from: what is
#     set in this window is not used. It is read as text, and each value is taken exactly as written.
#   * The guardrail: when one answers at GUARDRAIL_URL (http://localhost:8001 unless the settings
#     file says otherwise), for example the one Docker runs, that one is used. When none does, one
#     is started here from .venv, without Docker, and stopped again with the chatbot.
#
# The app listens on localhost only. No value from the settings file, and nothing you type at the
# prompt, is ever printed. This file is plain ASCII so that Windows PowerShell 5.1 reads it as written.
#Requires -Version 5.1
$ErrorActionPreference = 'Stop'

$Here = $PSScriptRoot
$Settings = Join-Path $env:USERPROFILE '.guardrail-chatbot.env'
# Every setting the app reads. A name in the file that is not one of these is a typing mistake.
$Known = @('CHATBOT_MODEL', 'GOOGLE_API_KEY', 'OPENAI_API_KEY', 'GROQ_API_KEY', 'OLLAMA_BASE_URL', 'CHATBOT_LOG_DIR',
           'GUARDRAIL_URL', 'GUARDRAIL_TIMEOUT_MS', 'CHATBOT_CALL_MODEL_ON_REVIEW', 'CHATBOT_MODEL_TIMEOUT_SECONDS',
           'CHATBOT_GEMINI_MODEL', 'CHATBOT_OPENAI_MODEL', 'CHATBOT_GROQ_MODEL', 'CHATBOT_OLLAMA_MODEL', 'CHATBOT_PASSWORD')

function Stop-Here([string]$Message) {
    [Console]::Error.WriteLine("start.ps1: $Message")
    exit 1
}

# --- the app itself: installed by uv into .venv here, the first time and after an update ------------
# uv's installer puts it in %USERPROFILE%\.local\bin, which a window opened before the install does
# not search yet, so it is looked for there as well.
$Uv = $null
$found = Get-Command uv -ErrorAction SilentlyContinue
if ($found) {
    $Uv = $found.Source
} else {
    foreach ($candidate in @((Join-Path $env:USERPROFILE '.local\bin\uv.exe'), (Join-Path $env:USERPROFILE '.cargo\bin\uv.exe'))) {
        if (Test-Path $candidate) { $Uv = $candidate; break }
    }
}
$Python = Join-Path $Here '.venv\Scripts\python.exe'
$Streamlit = Join-Path $Here '.venv\Scripts\streamlit.exe'
$Lock = Join-Path $Here 'uv.lock'
$Installed = Join-Path $Here '.venv\uv.lock.installed'      # a copy of what was installed, to tell when an update changes it

function Install-App {
    # --frozen: exactly what uv.lock says, never a newer version.
    & $Uv sync --frozen --project $Here
    if ($LASTEXITCODE -ne 0) {
        Stop-Here 'uv could not install the app; its own message is above. Check the internet connection and run start.bat again.'
    }
    Copy-Item -LiteralPath $Lock -Destination $Installed -Force
}

function Test-SameFile([string]$A, [string]$B) {
    if (-not (Test-Path -LiteralPath $B)) { return $false }
    return (Get-FileHash -LiteralPath $A).Hash -eq (Get-FileHash -LiteralPath $B).Hash
}

if (-not (Test-Path -LiteralPath $Streamlit)) {
    if (-not $Uv) {
        Stop-Here ('the app is not installed yet, and uv, which installs it, was not found. Install uv with:  ' +
                   'powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"   Then run start.bat again.')
    }
    Write-Host "Installing the app into $Here\.venv. This happens once and takes a minute or two, longer on a"
    Write-Host 'slow connection: it downloads about 80 packages (about 500 MB on disk), and Python 3.11 if'
    Write-Host 'this machine does not have it.'
    Install-App
    Write-Host 'Installed.'
    Write-Host ''
} elseif ($Uv -and -not (Test-SameFile $Lock $Installed)) {
    if (Test-Path -LiteralPath $Installed) {
        Write-Host 'uv.lock has changed since the app was installed. Bringing it up to date.'
    }
    Install-App
}

# --- the settings file: outside the repository, a plain file, yours ---------------------------------
if (-not (Test-Path -LiteralPath $Settings)) {
    Write-Host "There is no settings file yet ($Settings)."
    Write-Host 'Paste your own Gemini API key and press Enter. What you paste is not shown.'
    Write-Host 'To start without a model (guardrail only), just press Enter.'
    $secure = Read-Host -Prompt 'Gemini API key' -AsSecureString
    $pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $key = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
    }
    $key = $key.Trim()
    if ($key -match '\s' -or $key -match '^<.*>$' -or $key.StartsWith('"') -or $key.StartsWith("'")) {
        Stop-Here 'that does not look like a key: it has spaces, quotes or < > in it. Nothing was written. Start again and paste the key itself.'
    }
    $lines = @('# Settings for the guardrail test chatbot. Written by start.ps1. Keep this file private.',
               '# Every setting is listed in .env.example. One NAME=value a line; a value is taken as written.')
    if ($key) {
        $lines += 'CHATBOT_MODEL=gemini'
        $lines += "GOOGLE_API_KEY=$key"
    } else {
        $lines += 'CHATBOT_MODEL=none'
    }
    # UTF-8 without a byte-order mark, as start.sh writes it.
    [IO.File]::WriteAllLines($Settings, [string[]]$lines, (New-Object Text.UTF8Encoding($false)))
    if ($key) {
        Write-Host "Wrote $Settings, with your key and the model set to gemini."
    } else {
        Write-Host "Wrote $Settings, with no key: the app starts guardrail-only."
        Write-Host 'To connect Gemini later, add your key to that file (see .env.example) and start again.'
    }
    $key = $null
    $lines = $null
    Write-Host ''
}

$item = Get-Item -LiteralPath $Settings -Force
if ($item.PSIsContainer) { Stop-Here "$Settings is not a file." }
if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { Stop-Here "$Settings is a link. Keep the settings in a real file there." }

# --- settings come from that file and nowhere else ------------------------------------------------
foreach ($name in @(Get-ChildItem Env: | Where-Object { $_.Name -like 'CHATBOT_*' } | ForEach-Object { $_.Name }) + $Known) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}

# Read as text. Nothing in the file is run, and a mistake is said by its line and its NAME, never
# by its value. They are put in this window's environment only after the guardrail has started, so
# that the guardrail, which needs no key, is given none.
$values = [ordered]@{}
$named = @()
$number = 0
foreach ($line in [IO.File]::ReadAllLines($Settings)) {
    $number++
    $trimmed = $line.Trim()
    if ($trimmed -eq '' -or $trimmed.StartsWith('#')) { continue }
    if ($trimmed.StartsWith('export ')) { $trimmed = $trimmed.Substring(7).Trim() }
    $equals = $trimmed.IndexOf('=')
    if ($equals -lt 1) { Stop-Here "line $number of $Settings is not NAME=value." }
    $name = $trimmed.Substring(0, $equals).Trim()
    $value = $trimmed.Substring($equals + 1).Trim()
    if ($Known -notcontains $name) {
        Stop-Here "line $number of $Settings sets a name this app does not read. The names are in .env.example."
    }
    if ($value.Length -ge 2 -and (($value.StartsWith('"') -and $value.EndsWith('"')) -or ($value.StartsWith("'") -and $value.EndsWith("'")))) {
        $value = $value.Substring(1, $value.Length - 2)       # one pair of quotes around a value is taken off
    }
    if ($value -match '^<.*>$') {
        Stop-Here "$name on line $number of $Settings is written inside < >. Write the value itself, without the brackets."
    }
    $values[$name] = $value
    if ($value) { $named += $name }
}

# Every run is logged, so there must be somewhere to log it: the logs folder here, which git
# ignores, unless the settings file names another. The app checks that it cannot be committed.
$LogDir = $values['CHATBOT_LOG_DIR']
if (-not $LogDir) { $LogDir = Join-Path $Here 'logs' }
if ($LogDir.StartsWith('~/') -or $LogDir.StartsWith('~\')) { $LogDir = Join-Path $env:USERPROFILE $LogDir.Substring(2) }
$values['CHATBOT_LOG_DIR'] = $LogDir

# --- the guardrail: the one that answers at GUARDRAIL_URL, or one started here ---------------------
$Guardrail = $values['GUARDRAIL_URL']
if (-not $Guardrail) { $Guardrail = 'http://localhost:8001' }
$Guardrail = $Guardrail.TrimEnd('/')
# Windows tries "localhost" over IPv6 first and takes about two seconds to give up on it before it
# tries IPv4, where the guardrail listens (127.0.0.1, in Docker as here). A browser falls back at
# once; a short check from here never got there, and the chatbot would wait those two seconds on
# every call. So both are given the IPv4 address.
$Direct = $Guardrail -replace '^http://localhost(?=[:/]|$)', 'http://127.0.0.1'

function Test-Guardrail {
    try {
        $null = Invoke-WebRequest -Uri "$Direct/health" -UseBasicParsing -TimeoutSec 5
        return $true
    } catch {
        return $false
    }
}

$started = $null
# Whatever ends this script, Ctrl+C while it waits for the guardrail included, the guardrail it
# started goes with it.
try {
    if (Test-Guardrail) {
        Write-Host "Using the guardrail at $Guardrail."
    } else {
        $port = $null
        if ($Guardrail -match '^http://(localhost|127\.0\.0\.1):(\d+)$') { $port = $Matches[2] }
        $installed = $false
        if ($port -and (Test-Path -LiteralPath $Python)) {
            # In Windows PowerShell, a native program's redirected error output stops a script that
            # stops on errors; the failed import is an answer here, not an error.
            $ErrorActionPreference = 'Continue'
            & $Python -c 'import guardrail' 2>$null
            $installed = ($LASTEXITCODE -eq 0)
            $ErrorActionPreference = 'Stop'
        }
        if ($installed) {
            $rules = Join-Path (Resolve-Path -LiteralPath (Join-Path $Here '..\guardrail')).Path 'rules.rollout.json'
            New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
            $log = Join-Path $LogDir 'guardrail.log'
            $errors = Join-Path $LogDir 'guardrail.errors.log'
            # A program that already listens on the port, and does not answer as a guardrail, keeps the one
            # started here from opening it ([Errno 10048]). Say which program before trying. Windows says
            # who listens; a port it keeps for itself shows up below, in the guardrail's own error.
            $otherPort = if ($port -eq '18001') { '28001' } else { '18001' }
            $holder = $null
            try {
                $holder = Get-NetTCPConnection -LocalPort ([int]$port) -State Listen -ErrorAction Stop | Select-Object -First 1
            } catch {
                $holder = $null                         # nothing listens, or this Windows cannot say
            }
            if ($holder) {
                $program = (Get-Process -Id $holder.OwningProcess -ErrorAction SilentlyContinue).ProcessName
                Stop-Here ("port $port is already in use by another program ($program, process $($holder.OwningProcess)), which " +
                           "does not answer as a guardrail, so none can be started here. If it is a guardrail left from an " +
                           "earlier start (python), stop it with:  Stop-Process -Id $($holder.OwningProcess)   If it is the Docker " +
                           "guardrail, wait until it is ready. Otherwise give the guardrail another port: in $Settings, " +
                           "have the one line  GUARDRAIL_URL=http://127.0.0.1:$otherPort  and run start.bat again.")
            }
            Write-Host "No guardrail answers at $Guardrail, so one is started here, without Docker: the rollout"
            Write-Host 'policy, as the Docker image runs it, with no database and without the classifier, which'
            Write-Host "only observes in that policy. It stops when the chatbot stops. Its log: $log"
            $env:GUARDRAIL_RULES = $rules
            $started = Start-Process -FilePath $Python -WorkingDirectory $Here -NoNewWindow -PassThru `
                -ArgumentList @('-m', 'uvicorn', 'guardrail.app:app', '--host', '127.0.0.1', '--port', $port) `
                -RedirectStandardOutput $log -RedirectStandardError $errors
            Remove-Item -LiteralPath 'Env:GUARDRAIL_RULES' -ErrorAction SilentlyContinue
            $waited = 0
            while (-not (Test-Guardrail)) {
                if ($started.HasExited) {
                    [Console]::Error.WriteLine("The end of ${errors}:")
                    $tail = @(Get-Content -LiteralPath $errors -Tail 15 -ErrorAction SilentlyContinue)
                    $tail | ForEach-Object { [Console]::Error.WriteLine("    $_") }
                    if (($tail -join ' ') -match '10048|10013|address already in use|forbidden by its access permissions') {
                        Stop-Here ("port $port cannot be used: another program has it, or Windows keeps it for itself. " +
                                   "In $Settings, have the one line  GUARDRAIL_URL=http://127.0.0.1:$otherPort  and run start.bat again.")
                    }
                    Stop-Here 'the guardrail stopped while starting; the lines above say why.'
                }
                if ($waited -ge 120) {
                    Stop-Process -Id $started.Id -Force -ErrorAction SilentlyContinue
                    Stop-Here "the guardrail did not answer within a minute. See $errors."
                }
                Start-Sleep -Milliseconds 500
                $waited++
            }
            Write-Host "The guardrail is running at $Guardrail."
        } elseif ($port) {
            Write-Host "No guardrail answers at $Guardrail, and the guardrail is not installed in .venv here to start one."
            Write-Host 'Start it with Docker (README.md); until then the chatbot says it cannot reach it.'
        } else {
            Write-Host "No guardrail answers at $Guardrail. Until it does, the chatbot says it cannot reach it."
        }
    }

    # --- start ------------------------------------------------------------------------------------------
    foreach ($name in $values.Keys) {
        Set-Item -LiteralPath "Env:$name" -Value $values[$name]
    }
    $env:GUARDRAIL_URL = $Direct
    if ($named.Count -eq 0) { $named = @('none set') }
    Write-Host "Settings from ${Settings}: $($named -join ' ')"
    Write-Host 'The screen names every provider a test email can reach, before anything is sent.'
    Write-Host "Results are logged to $(Join-Path $LogDir 'chatbot-runs.csv')"
    Write-Host 'Starting on http://localhost:8502, for this machine only. Stop it with Ctrl+C.'
    Push-Location -LiteralPath $Here
    try {
        # The address is given here so that nothing in the environment can widen it.
        & $Streamlit run app.py --server.address localhost
    } finally {
        Pop-Location
    }
} finally {
    if ($started -and -not $started.HasExited) {
        Stop-Process -Id $started.Id -Force -ErrorAction SilentlyContinue
    }
}
