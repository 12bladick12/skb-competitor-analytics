$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$previewRoot = Join-Path $projectRoot 'data/comparison_preview'
$existing = Get-CimInstance Win32_Process | Where-Object {
    $_.CommandLine -like '*streamlit run tools/comparison_preview.py*' -and ($_.CommandLine -like '*8526*' -or $_.CommandLine -like '*8527*')
}
foreach ($process in $existing) { Stop-Process -Id $process.ProcessId }
$env:PYTHONIOENCODING = 'utf-8'
$pythonExecutable = (& python -c "import sys; print(sys.executable)").Trim()
$previewProcess = Start-Process -FilePath $pythonExecutable -ArgumentList @(
    '-m','streamlit','run','tools/comparison_preview.py','--server.address','127.0.0.1',
    '--server.port','8527','--server.headless','true','--browser.gatherUsageStats','false'
) -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $previewRoot 'server.stdout.log') `
    -RedirectStandardError (Join-Path $previewRoot 'server.stderr.log')
$previewProcess.Id | Set-Content -LiteralPath (Join-Path $previewRoot 'server.pid')
$ready = $false
for ($attempt = 0; $attempt -lt 20; $attempt++) {
    try {
        $response = Invoke-WebRequest -Uri 'http://127.0.0.1:8527/_stcore/health' -UseBasicParsing -TimeoutSec 1
        if ($response.StatusCode -eq 200) { $ready = $true; break }
    } catch { Start-Sleep -Milliseconds 500 }
}
if (-not $ready) { throw 'Preview health check failed; see server.stderr.log.' }
Write-Output "Preview verified: http://127.0.0.1:8527/ (PID $($previewProcess.Id))"
