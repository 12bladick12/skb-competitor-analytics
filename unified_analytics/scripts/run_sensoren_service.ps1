# Run by the current user's Task Scheduler entry. No credentials in arguments.
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
    $dependencyPath = Join-Path $projectRoot 'data\cloud_deps'
    if (-not (Test-Path -LiteralPath (Join-Path $dependencyPath 'psycopg'))) {
        $dependencyPath = Join-Path (Split-Path -Parent $projectRoot) 'price_monitor_app\data\cloud_deps'
    }
    if (Test-Path -LiteralPath (Join-Path $dependencyPath 'psycopg')) {
        $env:PYTHONPATH = $dependencyPath
    }
}
$env:PYTHONIOENCODING = 'utf-8'
$logRoot = Join-Path $projectRoot 'data\sensoren_agent\logs'
New-Item -ItemType Directory -Path $logRoot -Force | Out-Null
$stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
$collector = Start-Process -FilePath $pythonPath -ArgumentList @(
    '-u', '-m', 'price_monitor.sensoren_supervisor', '--enable', '--fresh-connections', '--transport', 'browser'
) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logRoot ($stamp + '.stdout.log')) `
    -RedirectStandardError (Join-Path $logRoot ($stamp + '.stderr.log'))
$collector.Id | Set-Content -LiteralPath (Join-Path $projectRoot 'data\sensoren_supervisor.pid')
$null = $collector.Handle
$collector.WaitForExit()
$collector.Refresh()
exit $collector.ExitCode
