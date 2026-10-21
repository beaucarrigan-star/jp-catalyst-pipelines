# Deploy the contract ingest pipeline (con_api + con_worker) to Zoho Catalyst.
# Run from this folder in PowerShell:   powershell -ExecutionPolicy Bypass -File .\deploy-contracts.ps1
#
# Modelled line for line on solicitation-pipeline\deploy-catalyst.ps1 (live since Sep 25), with the same
# fixes: private Python 3.9 for packaging, Linux wheels for compiled packages, no pip bin\ launchers,
# BOM-free configs, --project on the command, whoami read as text.
#
# Already done in the Catalyst console: project "contract-pipeline" (id 31190000000040038, Development)
# and Function job pool "conjobs" (id 31190000000040063, 1 GB, created Sep 27).
# Credentials: the Zoho client/refresh token are read from %USERPROFILE%\.solpipe.env (the solicitation
# pipeline's Self Client -- its ZohoCRM.modules.ALL / coql.READ scopes cover Contracts, attachments,
# Notes and the custom modules). The con_api token lives in %USERPROFILE%\.conpipe.env, created on first
# run. The build happens in %LOCALAPPDATA%\conpipe-build, outside OneDrive, because the credentials are
# injected into the function config there.

$ErrorActionPreference = "Stop"
$src   = $PSScriptRoot
$build = Join-Path $env:LOCALAPPDATA "conpipe-build"
$solEnv = Join-Path $env:USERPROFILE ".solpipe.env"
$conEnv = Join-Path $env:USERPROFILE ".conpipe.env"
$ProjectId = "31190000000040038"

if (-not (Test-Path $solEnv)) { throw "Missing $solEnv - run python ..\solicitation-pipeline\setup_token.py first" }
if (-not (Test-Path $conEnv)) {
    $bytes = New-Object byte[] 32
    [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $tok = [Convert]::ToBase64String($bytes).TrimEnd("=").Replace("+", "-").Replace("/", "_")
    [IO.File]::WriteAllText($conEnv, "CON_PIPELINE_TOKEN=$tok`n", (New-Object System.Text.UTF8Encoding($false)))
    Write-Host "Created $conEnv with a new CON_PIPELINE_TOKEN."
}

if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "Node.js 18+ is required (https://nodejs.org)" }
if (-not (Get-Command catalyst -ErrorAction SilentlyContinue)) {
    Write-Host "Installing the Catalyst CLI..."
    cmd /c "npm install -g zcatalyst-cli 2>&1"
}
$cliOut = cmd /c "catalyst --version 2>&1"
if ($LASTEXITCODE -ne 0) {
    Write-Host ($cliOut | Out-String)
    throw "The Catalyst CLI does not start. Reinstall it: npm uninstall -g zcatalyst-cli ; npm install -g zcatalyst-cli"
}
Write-Host "Catalyst CLI $($cliOut | Select-Object -Last 1)"
if (-not (Get-Command python -ErrorAction SilentlyContinue)) { throw "Python 3.9+ is required on PATH" }

# ---- Python 3.9 for Catalyst packaging (reuses the solicitation pipeline's private copy if present) ----
$py39 = $null
foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python39\python.exe", "C:\Program Files\Python39\python.exe",
                 "$env:LOCALAPPDATA\solpipe-python39\tools\python.exe", "$env:LOCALAPPDATA\conpipe-python39\tools\python.exe")) {
    if (Test-Path $c) { $py39 = $c; break }
}
if (-not $py39) {
    $pyHome = Join-Path $env:LOCALAPPDATA "conpipe-python39"
    $py39 = Join-Path $pyHome "tools\python.exe"
    Write-Host "Python 3.9 not found - downloading a private copy for Catalyst packaging (about 15 MB)..."
    New-Item -ItemType Directory -Force -Path $pyHome | Out-Null
    $pkg = Join-Path $pyHome "python-3.9.13.zip"
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"
    Invoke-WebRequest -UseBasicParsing -Uri "https://www.nuget.org/api/v2/package/python/3.9.13" -OutFile $pkg
    Expand-Archive -Path $pkg -DestinationPath $pyHome -Force
    Remove-Item $pkg
    cmd /c "`"$py39`" -m pip --version >nul 2>&1"
    if ($LASTEXITCODE -ne 0) { cmd /c "`"$py39`" -m ensurepip --upgrade 2>&1" }
}
# No quotes around the path: the CLI stores them literally and then cannot find the file.
if ($py39 -match " ") { $py39 = (New-Object -ComObject Scripting.FileSystemObject).GetFile($py39).ShortPath }
cmd /c "catalyst config:set python3_9.bin=$py39 2>&1"

# ---- Golden gate: every test must pass or nothing is deployed ----
Write-Host "Running the golden tests (rates, terms, clauses, worker end-to-end)..."
cmd /c "python -c ""import pytest, pdfplumber, reportlab"" >nul 2>&1"
if ($LASTEXITCODE -ne 0) {
    Write-Host "Installing pytest, pdfplumber, reportlab for the tests (user site, one time)..."
    cmd /c "python -m pip install --user --quiet pytest pdfplumber reportlab 2>&1"
}
Push-Location $src
try {
    cmd /c "python -m pytest tests -q -p no:cacheprovider 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "Golden tests failed - NOT deploying. Fix or re-calibrate (tests\calibrate_3d.py) first." }
} finally { Pop-Location }

# ---- Fresh build tree, keeping .catalystrc between runs ----
New-Item -ItemType Directory -Force -Path $build | Out-Null
Get-ChildItem $build -Exclude ".catalystrc" | Remove-Item -Recurse -Force
Copy-Item (Join-Path $src "catalyst.json") $build
foreach ($fn in "con_api", "con_worker") {
    $dst = Join-Path $build "functions\$fn"
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Get-ChildItem (Join-Path $src $fn) -File | Where-Object { $_.Name -ne "write_back.py" } |
        Copy-Item -Destination $dst
    Copy-Item (Join-Path $src "fn_config\$fn.json") (Join-Path $dst "catalyst-config.json")
}
# The worker's PDF/Word/Excel readers, vendored as LINUX wheels for Catalyst's python_3_9 stack (lxml,
# Pillow, pypdfium2 and cryptography are compiled; a plain Windows pip install would ship Windows
# binaries that fail on Catalyst). pdfminer.six 20250506 is the last release for Python 3.9. This exact
# set was import- and test-checked on Linux Python 3.9 on Sep 27.
$w = Join-Path $build "functions\con_worker"
python -m pip install --quiet --target $w --platform manylinux2014_x86_64 --python-version 3.9 --implementation cp `
    --only-binary=:all: --no-warn-conflicts "pdfminer.six==20250506" "pdfplumber==0.11.7" "python-docx==1.2.0" "openpyxl==3.1.5"
if ($LASTEXITCODE -ne 0) { throw "Could not vendor the PDF/Word/Excel readers for con_worker" }
foreach ($fn in "con_api", "con_worker") {
    $dst = Join-Path $build "functions\$fn"
    # pip writes Windows .exe launchers into bin\ - Catalyst rejects the upload ("Invalid input value for Zip File")
    Remove-Item (Join-Path $dst "bin") -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem $dst -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

# ---- Inject credentials as function environment variables ----
$vars = @{}
foreach ($f in $solEnv, $conEnv) {
    Get-Content $f | Where-Object { $_ -match "^\s*[A-Z_]+\s*=" } | ForEach-Object {
        $k, $v = $_ -split "=", 2; $vars[$k.Trim()] = $v.Trim()
    }
}
$map = @{
    "con_api"    = [ordered]@{ "CON_PIPELINE_TOKEN" = $vars["CON_PIPELINE_TOKEN"] }
    "con_worker" = [ordered]@{
        "ZOHO_CLIENT_ID" = $vars["ZOHO_CLIENT_ID"]; "ZOHO_CLIENT_SECRET" = $vars["ZOHO_CLIENT_SECRET"]
        "ZOHO_REFRESH_TOKEN" = $vars["ZOHO_REFRESH_TOKEN"]
        "ZOHO_ACCOUNTS_DOMAIN" = $(if ($vars["ZOHO_ACCOUNTS_URL"]) { $vars["ZOHO_ACCOUNTS_URL"] } else { "https://accounts.zoho.com" })
        "ZOHO_CRM_DOMAIN" = $(if ($vars["ZOHO_API_DOMAIN"]) { $vars["ZOHO_API_DOMAIN"] } else { "https://www.zohoapis.com" })
    }
}
foreach ($fn in "con_api", "con_worker") {
    foreach ($k in $map[$fn].Keys) { if (-not $map[$fn][$k]) { throw "Missing $k in $solEnv / $conEnv" } }
    $cfgPath = Join-Path $build "functions\$fn\catalyst-config.json"
    $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
    $cfg.deployment.env_variables = $map[$fn]
    # Written WITHOUT a byte-order mark: the CLI cannot read a BOM ("Cannot read properties of undefined (reading 'name')")
    [IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
}

Push-Location $build
try {
    $who = cmd /c "catalyst whoami 2>&1" | Out-String
    if ($LASTEXITCODE -ne 0 -or $who -match "haven't logged in|not logged in|login") {
        Write-Host "Logging in to Catalyst (a browser window opens)..."
        cmd /c "catalyst login"
        if ($LASTEXITCODE -ne 0) { throw "catalyst login failed" }
    }
    cmd /c "catalyst deploy --only functions --project $ProjectId 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "catalyst deploy failed - see the output above" }
} finally { Pop-Location }

Write-Host ""
Write-Host "Deployed. Check https://contract-pipeline-911327312.development.catalystserverless.com/server/con_api/health"
Write-Host "Then paste the con_api URL (ending /server/con_api/ingest) and CON_PIPELINE_TOKEN from $conEnv"
Write-Host "into the Deluge helper contract_pipeline_call."
