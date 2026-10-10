# Points this checkout's tracing at the team's shared Langfuse on codeit (runbook 3.7). Reads the shared project's
# keys over SSH from the root-only /etc/bidmate/langfuse.env and writes them into the gitignored .env, with the tunnel
# address and BIDMATE_LOCAL_MEMBER, so every local trace names who sent it. Nothing is written if the keys cannot be
# read. Usage: ./tools/infra/use-shared-langfuse.ps1 -User spai13xx [-Identity "$HOME/.ssh/<key>"] [-Member <name>]
param(
    [Parameter(Mandatory)][string]$User,
    [string]$Identity,
    [string]$Member = $User,
    [string]$Server = '35.255.64.243'
)
$ErrorActionPreference = 'Stop'
$taskRepo = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$taskAppEnv = Join-Path $taskRepo '.env'
$taskSsh = @('-o', 'BatchMode=yes') + @(if ($Identity) { '-i', $Identity, '-o', 'IdentitiesOnly=yes' })
[string[]]$taskKeys = @(ssh @taskSsh "$User@$Server" "sudo grep -E '^LANGFUSE_(PUBLIC|SECRET)_KEY=' /etc/bidmate/langfuse.env")
if ($LASTEXITCODE -ne 0 -or $taskKeys.Count -ne 2 -or -not ($taskKeys -match '^LANGFUSE_PUBLIC_KEY=pk-lf-\S+$') -or
        -not ($taskKeys -match '^LANGFUSE_SECRET_KEY=sk-lf-\S+$')) {
    throw "Could not read the shared keys as $User@$Server; .env is unchanged. Check -Identity and that sudo works there."
}
# 127.0.0.3:3300 is the tunnel's local end (runbook 3.7), the one address the shared stack's NEXTAUTH_URL names.
# LANGFUSE_BASE_URL is dropped too: SDK tools prefer it to LANGFUSE_HOST, so a stale one would send these keys elsewhere.
$taskClient = @('LANGFUSE_HOST=http://127.0.0.3:3300', "BIDMATE_LOCAL_MEMBER=$Member") + $taskKeys
[string[]]$taskKept = @(if (Test-Path -LiteralPath $taskAppEnv) {
    [System.IO.File]::ReadAllLines($taskAppEnv) |
        Where-Object { $_ -notmatch '^\s*(LANGFUSE_(HOST|BASE_URL|PUBLIC_KEY|SECRET_KEY)|BIDMATE_LOCAL_MEMBER)\s*=' }
})
[System.IO.File]::WriteAllLines($taskAppEnv, [string[]]@($taskKept + $taskClient), [System.Text.UTF8Encoding]::new($false))
Write-Output "Tracing now goes to the shared Langfuse as $Member. Open the tunnel (runbook 3.7) before starting the app."
