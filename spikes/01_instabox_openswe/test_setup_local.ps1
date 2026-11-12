# Offline regression check for Windows PowerShell 5.1; no real credentials/network.
$ErrorActionPreference = 'Stop'
$taskSetupPath = Join-Path $PSScriptRoot 'setup_local.ps1'
$taskExamplePath = Join-Path $PSScriptRoot 'models.local.example.json'
$taskMockRoles = Get-Content -LiteralPath $taskExamplePath -Raw | ConvertFrom-Json
$taskMockCatalog = [pscustomobject]@{
    data = @($taskMockRoles.PSObject.Properties | ForEach-Object {
        [pscustomobject]@{ id = ($_.Value -creplace '^9router:', '') }
    })
}
$script:taskRequestCount = 0
function Invoke-RestMethod {
    param($Uri, $Headers, $TimeoutSec, $ErrorAction)
    if ($Uri -ne 'http://127.0.0.1:20128/v1/models') { throw 'Unexpected URL' }
    if ($Headers.Authorization -ne 'Bearer offline-test-key') { throw 'Unexpected test credential' }
    $script:taskRequestCount++
    return $taskMockCatalog
}
function Read-Host { throw 'Offline test must never prompt for real credentials' }
$env:NINE_ROUTER_API_KEY = 'offline-test-key'
$env:NINE_ROUTER_MODEL = 'stale-single-model'
$env:DEEPAGENTS_MODEL = 'stale-legacy-model'

# Reproduce dot-sourcing without ModelsPath, even outside the project directory.
Push-Location ([IO.Path]::GetTempPath())
try {
    . $taskSetupPath
    if ($env:NINE_ROUTER_BASE_URL -ne 'http://127.0.0.1:20128/v1') { throw 'Local endpoint missing' }
    $taskLoadedRoles = $env:NINE_ROUTER_ROLE_MODELS | ConvertFrom-Json
    if ($taskLoadedRoles.dev -ne $taskMockRoles.dev) { throw 'Default model mapping not loaded' }
    if ($env:NINE_ROUTER_MODEL -or $env:DEEPAGENTS_MODEL) { throw 'Stale single-model defaults remain' }

    # The explicit path must still work.
    . $taskSetupPath -ModelsPath $taskExamplePath
    if ($script:taskRequestCount -ne 2) { throw 'Expected two mocked catalog requests' }
} finally {
    Pop-Location
}
Write-Output 'PASS: default and explicit ModelsPath; dot-source from another directory; no live API.'
