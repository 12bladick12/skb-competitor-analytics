param([ValidateSet('download','recognize')][string]$Queue='recognize')
$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot
$runtimeRoot=Join-Path $projectRoot 'data\passport_runtime'
$env:OLLAMA_NO_CLOUD='1'
$env:OLLAMA_HOST='127.0.0.1:11434'
$env:OLLAMA_MODELS=Join-Path $runtimeRoot 'models'
$env:OLLAMA_NUM_PARALLEL='1'
$env:OLLAMA_MAX_LOADED_MODELS='1'
$env:OLLAMA_VULKAN='0'
$env:PATH=(Join-Path $runtimeRoot 'tesseract')+';'+$env:PATH
$env:TESSDATA_PREFIX=Join-Path $runtimeRoot 'tesseract\tessdata'
$env:PYTHONPATH=(Join-Path $projectRoot 'data\passport_deps')+';'+$env:PYTHONPATH
if ($Queue -eq 'recognize') {
    try { $null=Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3 }
    catch {
        Start-Process -FilePath (Join-Path $runtimeRoot 'ollama\ollama.exe') -ArgumentList 'serve' -WindowStyle Hidden `
            -RedirectStandardOutput (Join-Path $runtimeRoot 'ollama.stdout.log') -RedirectStandardError (Join-Path $runtimeRoot 'ollama.stderr.log')
        Start-Sleep -Seconds 3
    }
}
python -m price_monitor.passport_worker --queue $Queue --settings .streamlit/secrets.toml
