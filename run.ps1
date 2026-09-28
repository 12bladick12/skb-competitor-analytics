param([int]$Port = 8501)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$pythonExe = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) {
    $pythonExe = (Get-Command python -ErrorAction Stop).Source
}
$env:PRICE_MONITOR_DB = Join-Path $PSScriptRoot 'data\prices.sqlite3'
$workerProcess = Start-Process -FilePath $pythonExe -ArgumentList @('-m', 'price_monitor.worker') -WorkingDirectory $PSScriptRoot -WindowStyle Hidden -PassThru
try {
    & $pythonExe -m streamlit run app.py --server.address=127.0.0.1 --server.port=$Port
} finally {
    if (-not $workerProcess.HasExited) { Stop-Process -Id $workerProcess.Id }
}
