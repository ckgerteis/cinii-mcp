<#
.SYNOPSIS
    Install the bibliographic MCP server family and point them at one receipts folder.

.DESCRIPTION
    Six servers, one deposit. This script installs whichever of them you name into
    a single Python environment, registers each with Claude Desktop, and gives them
    all one receipts folder to write to.

        cinii   jstage   ndl   korea_scholarship   openalex   semantic_scholar

    The folder, not a file. Appending to the hash-chained log is
    read-the-last-hash-then-write, and the lock around it holds within one process
    and not between several. Six servers are six processes: pointed at one file,
    two answering at the same moment both read the same predecessor and both claim
    it. Measured, not theorised — six processes writing 150 lines to one file
    produced fourteen forks. So each server gets its own chain inside a shared
    folder, MCP_RECEIPT_DIR names the folder, and the deposit is described by one
    manifest over all six files rather than by six separate assertions.

    Vendored byte-identical into all six repositories, as mediation.py and
    ledger.py are. Run it from any one of them.

.PARAMETER Servers
    Which to install. Default: all six.

.PARAMETER ReceiptsDir
    The receipts folder. If not passed, the script asks; the answer offered is
    whatever the already-registered servers use, and failing that
    %APPDATA%\Claude\mcp-receipts. No path is written into this file.

.PARAMETER Session
    The project or article slug written into every ledger line. It is what groups
    a project's queries, so it must be the same across all six. Asked for like
    ReceiptsDir if not passed.

.PARAMETER NoReceipts
    Register the servers without a receipts folder. Searches will run and leave no
    record. There is no reason to want this except a deliberate one.

.PARAMETER NotificationFiled
    NDL only, and only when ndl is among -Servers. Date you registered with the
    National Diet Library, YYYY-MM-DD. Recorded to NDL-API-NOTIFICATION.txt.

.PARAMETER PythonVersion
    Python launcher tag used only if no shared venv is found. Defaults to '3.13'.

.EXAMPLE
    .\install.ps1
    .\install.ps1 -Servers ndl,korea_scholarship -NotificationFiled 2026-08-19
    .\install.ps1 -ReceiptsDir "D:\research\receipts" -Session rhs-transactions-2026
#>

[CmdletBinding()]
param(
    [ValidateSet("cinii","jstage","ndl","korea_scholarship","openalex","semantic_scholar")]
    [string[]]$Servers = @("cinii","jstage","ndl","korea_scholarship","openalex","semantic_scholar"),
    [string]$ReceiptsDir,
    [string]$Session,
    [switch]$NoReceipts,
    [string]$NotificationFiled,
    [string]$PythonVersion = "3.13"
)

$ErrorActionPreference = "Stop"

$ScriptRoot   = $PSScriptRoot
$ServersRoot  = Join-Path $env:APPDATA "Claude\mcp-servers"
$SharedPython = Join-Path $ServersRoot ".venv\Scripts\python.exe"
$ConfigPath   = Join-Path $env:APPDATA "Claude\claude_desktop_config.json"
$DefaultDir   = Join-Path $env:APPDATA "Claude\mcp-receipts"
$FormUrl      = "https://form2.ndl.go.jp/form/pub/ndl07/api"
$TermsUrl     = "https://ndlsearch.ndl.go.jp/help/api"
$Interactive  = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected

$CATALOGUE = [ordered]@{
    cinii             = @{ dist = "cinii-mcp";             pkg = "cinii_mcp";             cmd = "cinii-mcp";             creds = @("CINII_APPID") }
    jstage            = @{ dist = "jstage-mcp";            pkg = "jstage_mcp";            cmd = "jstage-mcp";            creds = @() }
    ndl               = @{ dist = "ndl-mcp";               pkg = "ndl_mcp";               cmd = "ndl-mcp";               creds = @() }
    korea_scholarship = @{ dist = "korea-scholarship-mcp"; pkg = "korea_scholarship_mcp"; cmd = "korea-scholarship-mcp"; creds = @("KCI_API_KEY") }
    openalex          = @{ dist = "openalex-mcp";          pkg = "openalex_mcp";          cmd = "openalex-mcp";          creds = @("OPENALEX_API_KEY","OPENALEX_EMAIL") }
    semantic_scholar  = @{ dist = "semantic-scholar-mcp";  pkg = "semantic_scholar_mcp";  cmd = "semantic-scholar-mcp";  creds = @("SEMANTIC_SCHOLAR_API_KEY") }
}

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Get-Source($dist) {
    # A checkout beside this one, or this one, is preferred over the network: it
    # is what the person running the script is looking at.
    foreach ($cand in @((Join-Path (Split-Path $ScriptRoot -Parent) $dist), $ScriptRoot)) {
        if ((Split-Path $cand -Leaf) -ne $dist) { continue }
        if (Test-Path (Join-Path $cand "pyproject.toml")) { return @{ kind = "local"; value = $cand } }
    }
    return @{ kind = "git"; value = "git+https://github.com/ckgerteis/$dist" }
}

# -- 0. Existing configuration ------------------------------------------------

Write-Step "Reading the current Claude Desktop configuration"

if (-not (Test-Path $ConfigPath)) {
    $config = [pscustomobject]@{ mcpServers = [pscustomobject]@{} }
    Write-Host "    No config yet; one will be created."
} else {
    $config = (Get-Content $ConfigPath -Raw) | ConvertFrom-Json
    if (-not ($config.PSObject.Properties.Name -contains "mcpServers")) {
        $config | Add-Member -MemberType NoteProperty -Name mcpServers -Value ([pscustomobject]@{})
    }
    Write-Host "    Registered now: $((($config.mcpServers.PSObject.Properties.Name) | Sort-Object) -join ', ')"
}

$existing   = @{}
$knownDirs  = @()
$knownSess  = @()
$legacyLogs = @()
foreach ($prop in $config.mcpServers.PSObject.Properties) {
    $existing[$prop.Name] = $prop.Value
    $e = $prop.Value.env
    if (-not $e) { continue }
    if ($e.MCP_RECEIPT_DIR)     { $knownDirs  += [string]$e.MCP_RECEIPT_DIR }
    if ($e.MCP_RECEIPT_SESSION) { $knownSess  += [string]$e.MCP_RECEIPT_SESSION }
    if ($e.MCP_RECEIPT_LOG)     { $legacyLogs += [string]$e.MCP_RECEIPT_LOG }
}
$distinctDirs = @($knownDirs | Sort-Object -Unique)
$distinctSess = @($knownSess | Sort-Object -Unique)
$legacyLogs   = @($legacyLogs | Sort-Object -Unique)

# -- 1. The receipts folder ---------------------------------------------------

Write-Step "Receipts folder"

# NOT $receiptsDir / $session. PowerShell variable names are case-insensitive,
# so a local by that name IS the parameter, and initialising it to $null here
# silently discarded -ReceiptsDir and -Session before anything read them. The
# install then reported "not interactive; using <default>" and registered every
# server against a folder the caller had not chosen. Caught by running it.
$chosenDir     = $null
$chosenSession = $null

if ($NoReceipts) {
    Write-Host "    -NoReceipts: searches will run and leave no record." -ForegroundColor Yellow
} else {
    if ($ReceiptsDir) {
        $chosenDir = $ReceiptsDir
    } else {
        $suggested = if ($distinctDirs.Count -eq 1) { $distinctDirs[0] } else { $DefaultDir }
        if ($distinctDirs.Count -gt 1) {
            throw "The registered servers already use $($distinctDirs.Count) different receipts folders:`n  $($distinctDirs -join "`n  ")`nPass -ReceiptsDir to say which this install should join."
        }
        if ($Interactive) {
            Write-Host ""
            Write-Host "  Every server writes its own hash-chained file into one folder."
            Write-Host "  Put it somewhere you back up and would be willing to deposit."
            Write-Host ""
            $answer = Read-Host "  Receipts folder [$suggested]"
            $chosenDir = if ([string]::IsNullOrWhiteSpace($answer)) { $suggested } else { $answer.Trim('"').Trim() }
        } else {
            $chosenDir = $suggested
            Write-Host "    Not interactive; using $chosenDir"
        }
    }
    $chosenDir = [System.IO.Path]::GetFullPath($chosenDir)

    if ($Session) {
        $chosenSession = $Session
    } elseif ($distinctSess.Count -eq 1) {
        $chosenSession = $distinctSess[0]
        Write-Host "    Session slug: $chosenSession (from the registered servers)"
    } elseif ($distinctSess.Count -gt 1) {
        throw "The registered servers use $($distinctSess.Count) different session slugs:`n  $($distinctSess -join "`n  ")`nThe slug groups a project's queries. Pass -Session to say which."
    } elseif ($Interactive) {
        $answer = Read-Host "  Session slug (a project or article name)"
        $chosenSession = if ([string]::IsNullOrWhiteSpace($answer)) { $null } else { $answer.Trim() }
    }
    if (-not $chosenSession) {
        Write-Host "    No session slug. Lines will carry an empty label." -ForegroundColor Yellow
    }

    if (-not (Test-Path $chosenDir)) {
        New-Item -ItemType Directory -Path $chosenDir -Force | Out-Null
        Write-Host "    Created $chosenDir"
    } else {
        Write-Host "    Using $chosenDir"
    }
}

# -- 2. Python ----------------------------------------------------------------

Write-Step "Resolving Python"

if (Test-Path $SharedPython) {
    $Python = $SharedPython
    Write-Host "    Using the shared mcp-servers venv."
} else {
    $VenvDir = Join-Path $ServersRoot ".venv"
    $Python  = Join-Path $VenvDir "Scripts\python.exe"
    if (-not (Test-Path $Python)) {
        if (-not (Test-Path $ServersRoot)) { New-Item -ItemType Directory -Path $ServersRoot -Force | Out-Null }
        & py "-$PythonVersion" -m venv $VenvDir
        if ($LASTEXITCODE -ne 0) { throw "py -$PythonVersion failed. Install Python $PythonVersion or pass -PythonVersion." }
    }
    Write-Host "    Created the shared venv at $VenvDir."
}
$ScriptsDir = Split-Path $Python -Parent

# -- 3. The NDL notification --------------------------------------------------

if ($Servers -contains "ndl") {
    Write-Step "NDL API notification"

    $ndlSource = Get-Source "ndl-mcp"
    $MarkerFile = if ($ndlSource.kind -eq "local") { Join-Path $ndlSource.value "NDL-API-NOTIFICATION.txt" } else { $null }

    if ($NotificationFiled) {
        if ($NotificationFiled -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "-NotificationFiled must be YYYY-MM-DD." }
        if (-not $MarkerFile) { throw "-NotificationFiled needs a local ndl-mcp checkout to record the date into." }
        @"
NDL Search API - notification of continuous access
Filed: $NotificationFiled
Form:  $FormUrl
Terms: $TermsUrl
Contact: di-api@ndl.go.jp

Registered through the form described at section 17 of the NDL Search API help:
contact details and nature of use. The library confirmed in August 2026 that this
registration is no longer required, though still welcome. Providers used:
iss-ndl-opac, iss-ndl-opac-national, zassaku, zassaku-online, ndl-dl-open - all
NDL-created, none requiring a usage application for scholarly work.

Undertakings given, and implemented in src/ndl_mcp/server.py:
  serial requests, no concurrency        -> _rate_lock held across each request
  minimum one-second interval            -> MIN_REQUEST_INTERVAL
  a cap on records per search            -> MAX_RECORDS, no auto-pagination
  no harvesting interface                -> OAI-PMH not implemented
  credit on every response               -> ATTRIBUTION + provider_credit()
  metadata displayed, not accumulated    -> no cache, no local store

The undertakings above are kept because they are good practice toward a public
service, not because a filing compels them. If the provider set or any undertaking
changes, update this file so the record stays true.
"@ | Out-File -FilePath $MarkerFile -Encoding utf8
        Write-Host "    Recorded to $MarkerFile"
    } elseif ($MarkerFile -and (Test-Path $MarkerFile)) {
        Write-Host "    Registration on record:"
        Get-Content $MarkerFile -TotalCount 2 | ForEach-Object { Write-Host "      $_" }
    } else {
        Write-Host ""
        Write-Host "  Registering with the NDL is recommended, and not required." -ForegroundColor Yellow
        Write-Host "  The library confirmed in August 2026 that notification of continuous"
        Write-Host "  use is no longer mandatory, though it remains welcome."
        Write-Host ""
        Write-Host "  Do it anyway. It takes a few minutes, it tells the library who is"
        Write-Host "  using the interface and for what, and a national library that can"
        Write-Host "  see researchers using its API has an argument for keeping it open."
        Write-Host ""
        Write-Host "  Form:  $FormUrl"
        Write-Host "  Terms: $TermsUrl"
        if ($Interactive) {
            $answer = Read-Host "  Open the form now? [y/N]"
            if ($answer -match '^[Yy]') { Start-Process $FormUrl }
        }
        Write-Host "  Continuing. Rerun with -NotificationFiled YYYY-MM-DD once registered." -ForegroundColor Yellow
    }
}

# -- 4. Install ---------------------------------------------------------------

Write-Step "Installing"

& $Python -m pip install --upgrade --quiet pip
$installed = [ordered]@{}
foreach ($name in $Servers) {
    $m   = $CATALOGUE[$name]
    $src = Get-Source $m.dist
    & $Python -m pip install --quiet --upgrade $src.value
    if ($LASTEXITCODE -ne 0) { throw "pip install failed for $($m.dist) from $($src.value)." }
    $ver = (& $Python -c "import $($m.pkg) as p; print(p.__version__)").Trim()
    $exe = Join-Path $ScriptsDir "$($m.cmd).exe"
    if (-not (Test-Path $exe)) { throw "$($m.cmd) console script not found at $exe after install." }
    $installed[$name] = @{ version = $ver; exe = $exe }
    Write-Host ("    {0,-18} {1,-8} {2}" -f $m.dist, $ver, $src.kind)
}

# -- 5. Verify ----------------------------------------------------------------

Write-Step "Verifying the installed packages"

$probe = Join-Path $env:TEMP "mcp_family_probe_$PID.py"
$pkgList = ($Servers | ForEach-Object { $CATALOGUE[$_].pkg }) -join ","
@"
import importlib
for name in "$pkgList".split(","):
    pkg = importlib.import_module(name)
    srv = importlib.import_module(name + ".server")
    tools = sorted(srv.mcp._tool_manager._tools)
    line = "OK - %-24s %-8s %d tools" % (name, pkg.__version__, len(tools))
    try:
        med = importlib.import_module(name + ".mediation")
        line += "  ledger:%s" % ("reachable" if med.ledger_available() else "MISSING")
    except ModuleNotFoundError:
        ledger = importlib.import_module(name + ".ledger")
        line += "  ledger:reachable"
    print(line)
    if name == "ndl_mcp":
        assert srv.MIN_REQUEST_INTERVAL >= 1.0, "interval below what was filed with the NDL"
        assert srv.MAX_RECORDS <= 500, "record cap above the NDL ceiling"
        assert set(srv.ALL_DPIDS) <= set(srv.PROVIDERS), "undeclared provider"
        print("OK - ndl undertakings: interval %ss, cap %s records, providers declared"
              % (srv.MIN_REQUEST_INTERVAL, srv.MAX_RECORDS))
"@ | Out-File -FilePath $probe -Encoding utf8
try {
    & $Python $probe
    if ($LASTEXITCODE -ne 0) { throw "Installed-package check failed." }
} finally {
    Remove-Item $probe -ErrorAction SilentlyContinue
}

# -- 6. The receipts folder's own README --------------------------------------

if ($chosenDir) {
    Write-Step "Writing the receipts folder README"
    $anyCmd = $CATALOGUE[$Servers[0]].cmd
    $readme = Join-Path $chosenDir "README.md"
    @"
# Query receipts

Written by the bibliographic MCP server family. Every search these servers answer
deposits one line here: the query as supplied, the term as normalised, its script,
the parameters sent, a timestamp, a SHA-256 over query and parameters, and the
identifiers of the records returned. Credentials are redacted before a line is
composed. The records themselves are not held - logging a query is not
accumulating a database.

## Layout

    <server>.jsonl    one append-only, hash-chained file per server
    manifest.json     written by ``$anyCmd-ledger manifest``; the thing to cite

One file per server, and one writer per file. Appending is
read-the-last-hash-then-write and the lock around it does not hold between
processes, so several servers sharing one file will fork the chain. That is a
configuration fault rather than tampering, and ``verify`` names it as such, but
the layout exists so it cannot arise.

## Verifying

    $anyCmd-ledger verify-dir  "$chosenDir"
    $anyCmd-ledger manifest    "$chosenDir"
    $anyCmd-ledger summary     "$chosenDir\<server>.jsonl"
    $anyCmd-ledger csv         "$chosenDir\<server>.jsonl" out.csv

``verify`` exits non-zero if a chain does not verify, and distinguishes a fork
(concurrent writers), a missing line, a reordering, and an edited line.

## Session slug

Every line carries ``MCP_RECEIPT_SESSION``. It groups a project's queries, so it
should be the same across all six servers for one piece of research and changed
deliberately when the research changes. It is written per line and cannot be
corrected afterwards without breaking the chain.

Configured $(Get-Date -Format 'yyyy-MM-dd') by install.ps1.
"@ | Out-File -FilePath $readme -Encoding utf8
    Write-Host "    $readme"
}

# -- 7. Register --------------------------------------------------------------

Write-Step "Updating Claude Desktop config"

if (Test-Path $ConfigPath) {
    $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
    Copy-Item $ConfigPath "$ConfigPath.$stamp.bak"
    Write-Host "    Backed up config to $ConfigPath.$stamp.bak"
}

$serversHash = @{}
foreach ($prop in $config.mcpServers.PSObject.Properties) { $serversHash[$prop.Name] = $prop.Value }

foreach ($name in $Servers) {
    $m = $CATALOGUE[$name]
    $envBlock = [ordered]@{}

    # Credentials already registered are carried across. This script never asks
    # for a key it can read, and never blanks one it cannot.
    $prior = $existing[$name]
    foreach ($c in $m.creds) {
        if ($prior -and $prior.env -and $prior.env.$c) { $envBlock[$c] = [string]$prior.env.$c }
    }
    $missing = @($m.creds | Where-Object { -not $envBlock.Contains($_) -and $_ -notmatch 'EMAIL$' })
    if ($missing.Count) {
        Write-Host ("    {0}: no {1} registered; the server will install and its keyed tools will fail until one is set." -f $name, ($missing -join ", ")) -ForegroundColor Yellow
    }

    if ($chosenDir) { $envBlock["MCP_RECEIPT_DIR"] = $chosenDir }
    if ($chosenSession)     { $envBlock["MCP_RECEIPT_SESSION"] = $chosenSession }

    $entry = [ordered]@{ command = $installed[$name].exe }
    if ($envBlock.Count) { $entry["env"] = $envBlock }
    $serversHash[$name] = $entry
}

$newConfig = [ordered]@{}
foreach ($prop in $config.PSObject.Properties) {
    if ($prop.Name -ne "mcpServers") { $newConfig[$prop.Name] = $prop.Value }
}
$newConfig["mcpServers"] = $serversHash

$configDir = Split-Path $ConfigPath -Parent
if (-not (Test-Path $configDir)) { New-Item -ItemType Directory -Path $configDir | Out-Null }
($newConfig | ConvertTo-Json -Depth 10) | Out-File -FilePath $ConfigPath -Encoding utf8

# -- 8. Summary ---------------------------------------------------------------

Write-Step "Done"
Write-Host ""
Write-Host "Installed and registered:" -ForegroundColor Green
foreach ($name in $Servers) {
    Write-Host ("  {0,-18} {1,-8} {2}" -f $name, $installed[$name].version, $installed[$name].exe)
}
Write-Host ""
if ($chosenDir) {
    Write-Host "Receipts:" -ForegroundColor Green
    Write-Host "  folder : $chosenDir"
    Write-Host "  session: $(if ($chosenSession) { $chosenSession } else { '(none - lines will carry an empty label)' })"
    Write-Host "  verify : $($CATALOGUE[$Servers[0]].cmd)-ledger verify-dir `"$chosenDir`""
} else {
    Write-Host "Receipts: NOT DEPOSITED - no MCP_RECEIPT_DIR set." -ForegroundColor Yellow
    Write-Host "          Searches will run and leave no record." -ForegroundColor Yellow
}
if ($legacyLogs.Count) {
    Write-Host ""
    Write-Host "A previous single-file log was registered and is no longer written to:" -ForegroundColor Yellow
    $legacyLogs | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
    Write-Host "  Nothing has been moved or deleted. Keep it with the new folder if its" -ForegroundColor Yellow
    Write-Host "  lines belong to the same research; a chain is per-file and the two do" -ForegroundColor Yellow
    Write-Host "  not join." -ForegroundColor Yellow
}
Write-Host ""
Write-Host "mcpServers now in config:" -ForegroundColor Green
$serversHash.Keys | Sort-Object | ForEach-Object { Write-Host "  - $_" }
Write-Host ""
Write-Host "Restart Claude Desktop to load them." -ForegroundColor Yellow
