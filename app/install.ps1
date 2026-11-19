# GlassesClaude installer. "1 - Install.bat" runs this file.
# It is safe to run again at any time (it repairs or updates the setup).
# Plain ASCII on purpose, so Windows PowerShell 5.1 reads it correctly.

$ErrorActionPreference = 'Continue'
$Root = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $Root

function Step($n, $text) { Write-Host ""; Write-Host "[$n/5] $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "      OK  $text" -ForegroundColor Green }
function Warn($text) { Write-Host "      !!  $text" -ForegroundColor Yellow }
function Fail($text) {
    Write-Host ""
    Write-Host "  SETUP STOPPED: $text" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# Returns $true if this Python is version 3.11, 3.12 or 3.13 (the versions the
# speech libraries support best).
function Test-Python($exe, [string[]]$pyArgs) {
    try {
        $v = & $exe @pyArgs -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($LASTEXITCODE -eq 0 -and ("$v".Trim() -match '^3\.(11|12|13)$')) { return $true }
    } catch { }
    return $false
}

function Find-Python {
    foreach ($ver in @('3.12', '3.13', '3.11')) {
        if (Get-Command py -ErrorAction SilentlyContinue) {
            if (Test-Python 'py' @("-$ver")) { return @{ Exe = 'py'; Args = @("-$ver") } }
        }
    }
    if (Get-Command python -ErrorAction SilentlyContinue) {
        if (Test-Python 'python' @()) { return @{ Exe = 'python'; Args = @() } }
    }
    foreach ($folder in @('Python312', 'Python313', 'Python311')) {
        $p = Join-Path $env:LOCALAPPDATA "Programs\Python\$folder\python.exe"
        if ((Test-Path -LiteralPath $p) -and (Test-Python $p @())) { return @{ Exe = $p; Args = @() } }
    }
    return $null
}

Write-Host ""
Write-Host "  GlassesClaude setup" -ForegroundColor White
Write-Host "  Folder: $Root"

# ---------------------------------------------------------------- 1. Python
Step 1 "Looking for Python 3.12 ..."
$py = Find-Python
if (-not $py) {
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        Warn "Python 3.12 not found. Installing it with winget (this can take a few minutes) ..."
        winget install -e --id Python.Python.3.12 --scope user --accept-package-agreements --accept-source-agreements
        $py = Find-Python
    }
}
if (-not $py) {
    Fail "Please install Python 3.12 from https://www.python.org/downloads/ (tick 'Add python.exe to PATH'), then double-click '1 - Install' again."
}
Ok ("Using " + $py.Exe + " " + ($py.Args -join ' '))

# ------------------------------------------------- 2. private environment
Step 2 "Creating GlassesClaude's own Python environment (.venv) ..."
$venvPy = Join-Path $Root ".venv\Scripts\python.exe"
if ((Test-Path -LiteralPath $venvPy) -and -not (Test-Python $venvPy @())) {
    Warn "Old environment uses an unsupported Python - rebuilding it."
    Remove-Item -LiteralPath (Join-Path $Root ".venv") -Recurse -Force -ErrorAction SilentlyContinue
}
if (-not (Test-Path -LiteralPath $venvPy)) {
    $pyExe = $py.Exe
    $pyArgs = $py.Args
    & $pyExe @pyArgs -m venv (Join-Path $Root ".venv")
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPy)) { Fail "Could not create the .venv folder." }
}
Ok ".venv ready"

# ----------------------------------------------------------- 3. packages
Step 3 "Installing the speech and audio packages (first time: a few minutes) ..."
& $venvPy -m pip install --upgrade pip --disable-pip-version-check -q
& $venvPy -m pip install -r (Join-Path $Root "requirements.txt") --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { Fail "Installing packages failed. Check your internet connection and run '1 - Install' again." }
# The speech libraries need Microsoft's Visual C++ runtime (most PCs already have it).
& $venvPy -c "import ctranslate2, onnxruntime, av, sounddevice, numpy" 2>$null
if ($LASTEXITCODE -ne 0) {
    Warn "Some libraries need the Microsoft Visual C++ runtime. Installing it ..."
    if (Get-Command winget -ErrorAction SilentlyContinue) {
        winget install -e --id Microsoft.VCRedist.2015+.x64 --accept-package-agreements --accept-source-agreements
    }
    & $venvPy -c "import ctranslate2, onnxruntime, av, sounddevice, numpy"
    if ($LASTEXITCODE -ne 0) {
        Fail "The speech libraries can't load. Install 'Microsoft Visual C++ Redistributable (x64)' from microsoft.com, then run '1 - Install' again."
    }
}
Ok "packages installed"

# ------------------------------------------------------- 4. speech model
Step 4 "Downloading the speech model (about 0.5 GB, only once) ..."
$env:PYTHONUTF8 = "1"
& $venvPy -m app.main --download-models
if ($LASTEXITCODE -ne 0) { Fail "Downloading the speech model failed. Check your internet connection and try again." }
Ok "speech model ready"

# ------------------------------------------------------ 5. Claude Code
Step 5 "Checking Claude Code and your audio devices ..."
& $venvPy -m app.main --check
$claudeOk = ($LASTEXITCODE -eq 0)

Write-Host ""
Write-Host "  =================================================================" -ForegroundColor White
if ($claudeOk) {
    Write-Host "  Setup finished!" -ForegroundColor Green
} else {
    Write-Host "  Setup finished, but Claude Code was not found." -ForegroundColor Yellow
    Write-Host "  Install it: open PowerShell and run   irm https://claude.ai/install.ps1 | iex" -ForegroundColor Yellow
    Write-Host "  then type   claude   once to sign in." -ForegroundColor Yellow
}
Write-Host "  Next:"
Write-Host "   1. Pair your glasses with this PC (Windows Settings > Bluetooth & devices > Add device)."
Write-Host "   2. Double-click '2 - Start'."
Write-Host "   3. Press Ctrl + Alt + Space, talk, then pause."
Write-Host "  Optional: '3 - Set up away mode' lets you talk from anywhere through your phone."
Write-Host "  =================================================================" -ForegroundColor White
Write-Host ""
exit 0
