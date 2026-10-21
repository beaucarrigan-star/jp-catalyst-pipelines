# Deploy the solicitation pipeline to Zoho Catalyst.
# Run from this folder in PowerShell:   powershell -ExecutionPolicy Bypass -File .\deploy.ps1
#
# Already done in the Catalyst console (Sep 23): project "solicitation-pipeline" (id 31190000000033001)
# and Function job pool "soljobs" (id 31190000000033028, 512 MB).
# First time only, before running this:
#   python setup_token.py     (writes %USERPROFILE%\.solpipe.env with the Zoho credentials)
#
# The build happens in %LOCALAPPDATA%\solpipe-build, outside any synced folder, because the
# credentials are injected into the function config there and must never reach OneDrive.

$ErrorActionPreference = "Stop"
$src   = $PSScriptRoot
$build = Join-Path $env:LOCALAPPDATA "solpipe-build"
$envf  = Join-Path $env:USERPROFILE ".solpipe.env"

if (-not (Test-Path $envf)) { throw "Missing $envf - run: python setup_token.py" }
if (-not (Get-Command node -ErrorAction SilentlyContinue)) { throw "Node.js 18+ is required (https://nodejs.org)" }
if (-not (Get-Command catalyst -ErrorAction SilentlyContinue)) {
    Write-Host "Installing the Catalyst CLI..."
    cmd /c "npm install -g zcatalyst-cli 2>&1"
}
# Health-check the CLI before building. Run through cmd so Node's stderr is shown as text
# instead of PowerShell turning it into a NativeCommandError.
$cliOut = cmd /c "catalyst --version 2>&1"
if ($LASTEXITCODE -ne 0) {
    Write-Host ($cliOut | Out-String)
    Write-Host "Node version: $(cmd /c 'node --version')"
    throw ("The Catalyst CLI does not start (see the Node error above). Reinstall it: " +
           "npm uninstall -g zcatalyst-cli ; npm cache clean --force ; npm install -g zcatalyst-cli . " +
           "If it still fails, install Node.js 20 LTS from nodejs.org and reinstall the CLI.")
}
Write-Host "Catalyst CLI $($cliOut | Select-Object -Last 1)"
$py = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $py) { throw "Python 3.9+ is required on PATH (the CLI uses it to package dependencies)" }

# Catalyst packages python_3_9 functions with a local Python 3.9 (your everyday Python can be newer).
$py39 = $null
foreach ($c in @("$env:LOCALAPPDATA\Programs\Python\Python39\python.exe", "C:\Program Files\Python39\python.exe")) {
    if (Test-Path $c) { $py39 = $c; break }
}
if (-not $py39 -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $p = cmd /c "py -3.9 -c ""import sys;print(sys.executable)"" 2>nul"
    if ($LASTEXITCODE -eq 0 -and $p) { $py39 = ($p | Select-Object -Last 1).Trim() }
}
if (-not $py39) {
    # No Python 3.9 installed: fetch the official python.org build packaged on NuGet (a plain zip, no
    # installer, no admin rights, pip included) into a private folder. Your normal Python is untouched.
    $pyHome = Join-Path $env:LOCALAPPDATA "solpipe-python39"
    $py39 = Join-Path $pyHome "tools\python.exe"
    if (-not (Test-Path $py39)) {
        Write-Host "Python 3.9 not found - downloading a private copy for Catalyst packaging (about 15 MB)..."
        New-Item -ItemType Directory -Force -Path $pyHome | Out-Null
        $pkg = Join-Path $pyHome "python-3.9.13.zip"
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        $ProgressPreference = "SilentlyContinue"   # the progress bar makes downloads 10x slower in PowerShell 5
        Invoke-WebRequest -UseBasicParsing -Uri "https://www.nuget.org/api/v2/package/python/3.9.13" -OutFile $pkg
        Expand-Archive -Path $pkg -DestinationPath $pyHome -Force
        Remove-Item $pkg
        if (-not (Test-Path $py39)) { throw "Downloaded Python 3.9 but $py39 is missing. Install it instead: winget install Python.Python.3.9" }
        cmd /c "`"$py39`" -m pip --version >nul 2>&1"
        if ($LASTEXITCODE -ne 0) { cmd /c "`"$py39`" -m ensurepip --upgrade 2>&1" }
    }
}
# No quotes around the path: the CLI stores them literally and then cannot find the file.
if ($py39 -match " ") { $py39 = (New-Object -ComObject Scripting.FileSystemObject).GetFile($py39).ShortPath }
$ver = cmd /c "`"$py39`" --version 2>&1"
Write-Host "Python for Catalyst packaging: $py39 ($ver)"
cmd /c "catalyst config:set python3_9.bin=$py39 2>&1"
cmd /c "catalyst config:get python3_9.bin 2>&1"

Write-Host "Running offline tests..."
foreach ($t in "test_pipeline.py", "test_ingest_flow.py", "test_scoring_engine.py", "test_rule_ingest.py") {
    python (Join-Path $src "tests\$t")
    if ($LASTEXITCODE -ne 0) { throw "Tests failed ($t) - not deploying." }
}

# fresh build tree, keeping .catalystrc (project binding) between runs
New-Item -ItemType Directory -Force -Path $build | Out-Null
Get-ChildItem $build -Exclude ".catalystrc" | Remove-Item -Recurse -Force
Copy-Item (Join-Path $src "catalyst.json") $build
Copy-Item (Join-Path $src "functions") $build -Recurse
foreach ($fn in "sol_api", "sol_worker") {
    Copy-Item (Join-Path $src "solpipe") (Join-Path $build "functions\$fn\solpipe") -Recurse
    # PDF text reader for rule ingest. Vendored as LINUX wheels for Catalyst's python_3_9 stack:
    # pdfminer.six needs cryptography, a compiled package, so a plain Windows pip install would ship
    # Windows binaries that fail on Catalyst. 20250506 is the last pdfminer.six release for Python 3.9.
    python -m pip install --quiet --target (Join-Path $build "functions\$fn") --platform manylinux2014_x86_64 `
        --python-version 3.9 --implementation cp --only-binary=:all: --no-warn-conflicts "pdfminer.six==20250506"
    if ($LASTEXITCODE -ne 0) { throw "Could not vendor pdfminer.six for $fn" }
    # pip also writes Windows .exe launchers into bin\ - not needed in the function and they make Catalyst
    # reject the upload ("Invalid input value for Zip File"). Drop them and any bytecode caches.
    Remove-Item (Join-Path $build "functions\$fn\bin") -Recurse -Force -ErrorAction SilentlyContinue
    Get-ChildItem (Join-Path $build "functions\$fn") -Recurse -Directory -Filter "__pycache__" |
        Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
}

# inject credentials as function environment variables (Zoho + pipeline token only; no AI keys, Sep 25)
$vars = @{}
Get-Content $envf | Where-Object { $_ -match "^\s*[A-Z_]+\s*=" } | ForEach-Object {
    $k, $v = $_ -split "=", 2; $vars[$k.Trim()] = $v.Trim()
}
foreach ($fn in "sol_api", "sol_worker") {
    $cfgPath = Join-Path $build "functions\$fn\catalyst-config.json"
    $cfg = Get-Content $cfgPath -Raw | ConvertFrom-Json
    $ev = [ordered]@{}
    foreach ($k in "ZOHO_CLIENT_ID","ZOHO_CLIENT_SECRET","ZOHO_REFRESH_TOKEN","ZOHO_ACCOUNTS_URL","ZOHO_API_DOMAIN","PIPELINE_TOKEN") {
        if ($vars.ContainsKey($k)) { $ev[$k] = $vars[$k] }
    }
    $cfg.deployment.env_variables = $ev
    # Write WITHOUT a byte-order mark: Windows PowerShell's "-Encoding UTF8" adds one, and the Catalyst
    # CLI then cannot read the config ("Cannot read properties of undefined (reading 'name')").
    [IO.File]::WriteAllText($cfgPath, ($cfg | ConvertTo-Json -Depth 6), (New-Object System.Text.UTF8Encoding($false)))
}

Push-Location $build
try {
    # whoami exits 0 even when logged out, so read what it says
    $who = cmd /c "catalyst whoami 2>&1" | Out-String
    if ($LASTEXITCODE -ne 0 -or $who -match "haven't logged in|not logged in|login") {
        Write-Host "Logging in to Catalyst (a browser window opens)..."
        cmd /c "catalyst login"
        if ($LASTEXITCODE -ne 0) { throw "catalyst login failed" }
    }
    # The build folder has catalyst.json but was never 'catalyst init'-ed, so name the project on the command
    # instead of project:use (which refuses with 'Not in Catalyst app directory').
    cmd /c "catalyst deploy --only functions --project 31190000000033001 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "catalyst deploy failed - see the output above" }
} finally { Pop-Location }

Write-Host ""
Write-Host "Deployed. Copy the sol_api URL printed above (ends in /server/sol_api/) into the Deluge helper"
Write-Host "solicitation_pipeline_call as <CATALYST_SOL_API_URL>, and PIPELINE_TOKEN from $envf as <PIPELINE_TOKEN>."
