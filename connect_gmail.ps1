# Connects the Airbyte Gmail connector. Run in your own PowerShell window:
#   powershell -ExecutionPolicy Bypass -File "connect_gmail.ps1"
# Your browser will open -> sign in to the Gmail account you'll SEND FROM -> approve.

$cli = "$env:LOCALAPPDATA\airbyte-agent\airbyte-agent.exe"

# Load Airbyte credentials from .env
Get-Content "$PSScriptRoot\.env" | Where-Object { $_ -match '^AIRBYTE_(CLIENT_ID|CLIENT_SECRET|ORGANIZATION_ID)=' } | ForEach-Object {
    $parts = $_ -split '=', 2
    Set-Item -Path "Env:$($parts[0])" -Value $parts[1]
}

# Write request JSON to a file to avoid PowerShell quote-stripping
$createReq = '{"workspace":"default","name":"gmail"}'
$createReq | Out-File -FilePath "$PSScriptRoot\_create_req.json" -Encoding ascii -NoNewline

$listReq = '{"workspace":"default"}'
$listReq | Out-File -FilePath "$PSScriptRoot\_list_req.json" -Encoding ascii -NoNewline

Write-Host "Opening Google authorization in your browser..." -ForegroundColor Cyan
Write-Host "Sign in to the account Angent will SEND FROM, then approve access." -ForegroundColor Cyan
Write-Host "(If Google warns 'unverified app', click Advanced -> continue.)" -ForegroundColor Yellow
Write-Host ""

& $cli connectors create --json "@$PSScriptRoot\_create_req.json"

Write-Host ""
Write-Host "Verifying the connector was created..." -ForegroundColor Cyan
& $cli connectors list --json "@$PSScriptRoot\_list_req.json"

# Cleanup
Remove-Item "$PSScriptRoot\_create_req.json", "$PSScriptRoot\_list_req.json" -ErrorAction SilentlyContinue
