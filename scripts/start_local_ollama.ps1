$ErrorActionPreference='Stop'
$projectRoot=Split-Path -Parent $PSScriptRoot
$runtimeRoot=Join-Path $projectRoot 'data\passport_runtime'
$env:OLLAMA_NO_CLOUD='1'
$env:OLLAMA_HOST='127.0.0.1:11434'
$env:OLLAMA_MODELS=Join-Path $runtimeRoot 'models'
$env:OLLAMA_NUM_PARALLEL='1'
$env:OLLAMA_MAX_LOADED_MODELS='1'
$env:OLLAMA_VULKAN='0'
try { $null=Invoke-RestMethod 'http://127.0.0.1:11434/api/tags' -TimeoutSec 3; Write-Output 'Local Ollama is already running' }
catch {
    $server=Start-Process -FilePath (Join-Path $runtimeRoot 'ollama\ollama.exe') -ArgumentList 'serve' -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $runtimeRoot 'ollama.stdout.log') -RedirectStandardError (Join-Path $runtimeRoot 'ollama.stderr.log')
    $server.Id | Set-Content -LiteralPath (Join-Path $runtimeRoot 'ollama.pid')
    Write-Output ('Started local Ollama process '+$server.Id)
}
