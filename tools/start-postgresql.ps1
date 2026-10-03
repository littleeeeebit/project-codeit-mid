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
Write-Output 'PostgreSQL is ready on port 55432. No application database was selected; RFP_DATABASE_DSN is unchanged.'
