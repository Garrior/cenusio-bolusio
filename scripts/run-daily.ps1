[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$logDirectory = Join-Path $repoRoot 'logs'
New-Item -ItemType Directory -Path $logDirectory -Force | Out-Null
$logPath = Join-Path $logDirectory ("daily-price-{0}.log" -f (Get-Date -Format 'yyyyMMdd-HHmmss'))
$transcriptStarted = $false

function Invoke-CheckedNative {
    param(
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    & $Executable @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw ("Command '{0}' failed with exit code {1}." -f ($Executable + ' ' + ($Arguments -join ' ')), $LASTEXITCODE)
    }
}

try {
    Start-Transcript -Path $logPath | Out-Null
    $transcriptStarted = $true
    Set-Location $repoRoot

    $branch = (& git rev-parse --abbrev-ref HEAD).Trim()
    if ($LASTEXITCODE -ne 0) { throw 'Could not determine the current Git branch.' }
    if ($branch -ne 'main') { throw "Expected branch 'main', found '$branch'. Switch to main before running." }

    $status = & git status --porcelain
    if ($LASTEXITCODE -ne 0) { throw 'Could not read Git working-tree status.' }
    if ($status) { throw 'Working tree has local changes. Commit or stash them before running the scheduled scraper.' }

    Invoke-CheckedNative -Executable 'git' -Arguments @('pull', '--ff-only', 'origin', 'main')

    $python = Join-Path $repoRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $python)) {
        $pythonCommand = Get-Command python -ErrorAction Stop
        $python = $pythonCommand.Source
    }
    Invoke-CheckedNative -Executable $python -Arguments @('scraper.py')

    Invoke-CheckedNative -Executable 'git' -Arguments @('add', '-A', '--', 'data/')
    & git diff --cached --quiet
    $diffExitCode = $LASTEXITCODE
    if ($diffExitCode -eq 0) {
        Write-Output 'Scrape completed; there are no data changes to commit.'
    } elseif ($diffExitCode -eq 1) {
        Invoke-CheckedNative -Executable 'git' -Arguments @('commit', '-m', 'chore: update game catalog and prices')
        Invoke-CheckedNative -Executable 'git' -Arguments @('push', 'origin', 'main')
    } else {
        throw "Could not inspect staged changes (git diff exit code $diffExitCode)."
    }
} finally {
    if ($transcriptStarted) {
        Stop-Transcript | Out-Null
    }
}
