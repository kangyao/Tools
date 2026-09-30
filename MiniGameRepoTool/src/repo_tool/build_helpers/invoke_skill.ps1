param([Parameter(Mandatory = $true)][string]$RequestFile)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding

try {
    $request = Get-Content -Raw -LiteralPath $RequestFile -Encoding UTF8 | ConvertFrom-Json
    $parameters = @{}
    foreach ($property in $request.Parameters.PSObject.Properties) {
        $parameters[$property.Name] = $property.Value
    }
    & $request.Script @parameters
    exit 0
}
catch {
    Write-Output $_.Exception.Message
    exit 1
}
