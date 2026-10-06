$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent $PSScriptRoot
$taskSecrets = Join-Path $taskRepo '.runtime/langfuse.env'
$taskCompose = Join-Path $taskRepo 'compose.langfuse.yaml'
$taskAppEnv = Join-Path $taskRepo '.env'
function New-TaskSecret { [Convert]::ToHexString([System.Security.Cryptography.RandomNumberGenerator]::GetBytes(32)).ToLowerInvariant() }
if (-not (Test-Path -LiteralPath $taskSecrets)) {
    # New secrets cannot open existing data: a reset needs
    # `docker compose --env-file .runtime/langfuse.env -f compose.langfuse.yaml down -v` first.
    if (docker volume ls -q --filter 'name=^bidmate-langfuse_postgres_data$') {
        throw 'bidmate-langfuse volumes exist but .runtime/langfuse.env is missing; restore the file or remove the volumes.'
    }
    [System.IO.Directory]::CreateDirectory((Split-Path -Parent $taskSecrets)) | Out-Null
    $taskLines = @(
        "LANGFUSE_POSTGRES_PASSWORD=$(New-TaskSecret)",
        "LANGFUSE_CLICKHOUSE_PASSWORD=$(New-TaskSecret)",
        "LANGFUSE_REDIS_PASSWORD=$(New-TaskSecret)",
        "LANGFUSE_MINIO_PASSWORD=$(New-TaskSecret)",
        "LANGFUSE_SALT=$(New-TaskSecret)",
        "LANGFUSE_ENCRYPTION_KEY=$(New-TaskSecret)",
        "LANGFUSE_NEXTAUTH_SECRET=$(New-TaskSecret)",
        "LANGFUSE_INIT_USER_EMAIL=admin@bidmate.local",
        "LANGFUSE_INIT_USER_PASSWORD=$(New-TaskSecret)",
        "LANGFUSE_PUBLIC_KEY=pk-lf-$(New-TaskSecret)",
        "LANGFUSE_SECRET_KEY=sk-lf-$(New-TaskSecret)"
    )
    [System.IO.File]::WriteAllText($taskSecrets, ($taskLines -join "`n") + "`n", [System.Text.UTF8Encoding]::new($false))
}
docker compose --project-directory $taskRepo --env-file $taskSecrets -f $taskCompose up -d --wait
if ($LASTEXITCODE -ne 0) { throw 'Langfuse startup failed; check Docker Desktop and ports 3100 and 9190.' }
# The application reads these three from the process environment or .env; keep .env in step with the project keys.
# LANGFUSE_BASE_URL is dropped too: SDK tools prefer it to LANGFUSE_HOST, so a stale one would send these keys elsewhere.
# 127.0.0.2, not localhost: the ports are IPv4 loopback only (see compose.langfuse.yaml), and a browser may resolve
# localhost to ::1.
$taskClient = @('LANGFUSE_HOST=http://127.0.0.2:3100') +
    @(Get-Content -LiteralPath $taskSecrets | Where-Object { $_ -match '^LANGFUSE_(PUBLIC_KEY|SECRET_KEY)=' })
[string[]]$taskKept = @(if (Test-Path -LiteralPath $taskAppEnv) {
    [System.IO.File]::ReadAllLines($taskAppEnv) |
        Where-Object { $_ -notmatch '^\s*LANGFUSE_(HOST|BASE_URL|PUBLIC_KEY|SECRET_KEY)\s*=' }
})
[System.IO.File]::WriteAllLines($taskAppEnv, [string[]]@($taskKept + $taskClient), [System.Text.UTF8Encoding]::new($false))
Write-Output 'Langfuse is ready at http://127.0.0.2:3100 (login: LANGFUSE_INIT_USER_* in .runtime/langfuse.env).'
Write-Output 'Tracing turns on for the next application start; .env now holds LANGFUSE_HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY.'
