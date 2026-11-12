# Run in your PowerShell session: . .\spikes\01_instabox_openswe\setup_local.ps1
# Sets process environment only. Does not write keys or call a model/create a VM.
[CmdletBinding()]
param(
    [string]$ModelsPath
)

# Windows PowerShell may not populate PSScriptRoot while binding defaults.
# Resolve script-relative paths in the body, after parameter binding completes.
if (-not $PSBoundParameters.ContainsKey('ModelsPath')) {
    $ModelsPath = Join-Path $PSScriptRoot 'models.local.example.json'
}

$taskRouterUrl = 'http://127.0.0.1:20128/v1'
$taskRoleJson = Get-Content -LiteralPath $ModelsPath -Raw -ErrorAction Stop
$taskRoleMap = $taskRoleJson | ConvertFrom-Json -ErrorAction Stop
if ($taskRoleMap -isnot [pscustomobject]) {
    throw 'Model mapping must be a JSON object of role -> model ID.'
}
$taskRoleEntries = @($taskRoleMap.PSObject.Properties)
if ($taskRoleEntries.Count -eq 0) {
    throw 'Model mapping must contain at least one role.'
}
foreach ($taskRoleEntry in $taskRoleEntries) {
    if ($taskRoleEntry.Name -cnotmatch '^[a-z][a-z0-9_-]{0,31}$' -or
        $taskRoleEntry.Value -isnot [string] -or
        [string]::IsNullOrWhiteSpace($taskRoleEntry.Value) -or
        $taskRoleEntry.Value -match '\s') {
        throw 'Each role must have a lowercase identifier and a non-empty model ID without whitespace.'
    }
}

# Keep router credentials in the process environment, never in a file or output.
if ([string]::IsNullOrWhiteSpace($env:NINE_ROUTER_API_KEY)) {
    $taskSecureKey = Read-Host '9Router API key (hidden; from the local dashboard)' -AsSecureString
    $taskKeyPointer = [IntPtr]::Zero
    try {
        $taskKeyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($taskSecureKey)
        $env:NINE_ROUTER_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($taskKeyPointer).Trim()
    } finally {
        if ($taskKeyPointer -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($taskKeyPointer)
        }
        $taskSecureKey.Dispose()
    }
}
if ([string]::IsNullOrWhiteSpace($env:NINE_ROUTER_API_KEY)) {
    throw 'A 9Router API key is required. No model or VM has been started.'
}

try {
    $taskCatalog = Invoke-RestMethod -Uri "$taskRouterUrl/models" -Headers @{
        Authorization = "Bearer $env:NINE_ROUTER_API_KEY"
    } -TimeoutSec 15 -ErrorAction Stop
} catch {
    throw 'Cannot read the local 9Router model catalog. Check that 9Router is running and the router key is correct.'
}
$taskModelIds = @($taskCatalog.data | ForEach-Object { $_.id })
foreach ($taskRoleEntry in $taskRoleEntries) {
    $taskModelId = $taskRoleEntry.Value -creplace '^9router:', ''
    if ($taskModelIds -cnotcontains $taskModelId) {
        throw "Model for role '$($taskRoleEntry.Name)' is not listed by local 9Router. Update the model mapping and retry."
    }
}

$env:NINE_ROUTER_BASE_URL = $taskRouterUrl
$env:NINE_ROUTER_ROLE_MODELS = $taskRoleJson
# Role mapping is the default for this session, instead of an older single model.
Remove-Item Env:NINE_ROUTER_MODEL -ErrorAction SilentlyContinue
Remove-Item Env:DEEPAGENTS_MODEL -ErrorAction SilentlyContinue
Write-Host "Local 9Router configured: $taskRouterUrl"
foreach ($taskRoleEntry in $taskRoleEntries) {
    Write-Host ("  {0}: {1}" -f $taskRoleEntry.Name, $taskRoleEntry.Value)
}
Write-Host 'Catalog checked only; inference/tool calling has not been tested. No VM created.'
Write-Host 'Settings apply to this PowerShell session. Run the Python runner in the same terminal.'
