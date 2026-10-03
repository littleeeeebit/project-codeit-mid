$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
$taskSecrets = Join-Path $taskRepo '.runtime/postgresql.env'
if (-not (Test-Path -LiteralPath $taskSecrets)) {
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $taskSecrets)) | Out-Null
    $taskPassword = [Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
    [System.IO.File]::WriteAllText($taskSecrets, "BIDMATE_POSTGRES_PASSWORD=$taskPassword`n", [System.Text.UTF8Encoding]::new($false))
}
docker compose --project-directory $taskRepo --env-file $taskSecrets -f (Join-Path $taskRepo 'compose.postgresql.yaml') up -d --wait
if ($LASTEXITCODE -ne 0) { throw 'PostgreSQL startup failed; check Docker Desktop and port 55432.' }
$taskPassword = ([System.IO.File]::ReadAllText($taskSecrets)).Trim().Split('=', 2)[1]
$env:RFP_DATABASE_DSN = "postgresql://bidmate:${taskPassword}@127.0.0.1:55432/bidmate_rehearsal"
Write-Output 'PostgreSQL rehearsal is ready. RFP_DATABASE_DSN is set in this PowerShell session.'
