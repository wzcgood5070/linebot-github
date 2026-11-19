param([Parameter(Mandatory=$true)][string]$ProjectPath)
$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath $ProjectPath).Path
$source = Split-Path -Parent $PSScriptRoot
$release = Join-Path $source 'work/linebot-release'
$base = Join-Path $project 'compose.yaml'
$overlay = Join-Path $release 'compose.release.yaml'
$rollback = Join-Path $release 'compose.rollback.yaml'
$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$newImage = "linebot-vnext:release-$stamp"
$oldImage = "linebot-vnext:rollback-$stamp"

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed ($LASTEXITCODE): $($args[0])" }
}

function Wait-Linebot {
    param([string]$ComposeOverlay)
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $id = Invoke-Docker compose -f $base -f $ComposeOverlay ps -q linebot
        if ($id) {
            $health = Invoke-Docker inspect --format '{{.State.Health.Status}}' $id
            if ($health -eq 'healthy') { return }
            if ($health -eq 'unhealthy') { break }
        }
        Start-Sleep -Seconds 2
    }
    throw 'The LINE Bot container did not become healthy.'
}

Invoke-Docker version --format '{{.Server.Version}}'
$existing = @(Invoke-Docker compose -f $base ps -q linebot)
if ($existing.Count -ne 1) { throw 'Expected one running LINE Bot container. No deployment changes were made.' }
$previous = Invoke-Docker inspect --format '{{.Image}}' $existing[0]
@'
import json, urllib.request
request = urllib.request.Request('http://n8n:5678/webhook/line-meeting-controller-ready-v3', data=b'{}', headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request, timeout=20) as response:
    result = json.load(response)
assert result.get('workflow_version') == 'n8n-v3', 'Publish the n8n v3 workflow before updating the Bot.'
print('N8N CONTROLLER PREFLIGHT: OK')
'@ | & docker exec -i $existing[0] python -
if ($LASTEXITCODE -ne 0) { throw 'n8n v3 is not ready. No deployment changes were made. Import and publish the v3 workflow first.' }
$runtimeEnv = (Invoke-Docker inspect --format '{{json .Config.Env}}' $existing[0]) | ConvertFrom-Json
$restoreEnv = @{}
foreach ($key in @('ORCHESTRATOR','INTERNAL_MEETING_JOBS','MEETING_SUBMISSION_CHECKS_ENABLED','N8N_INTAKE_URL')) {
    $entry = @($runtimeEnv | Where-Object { $_.StartsWith("$key=") })
    if ($entry.Count -eq 1) { $restoreEnv[$key] = $entry[0].Substring($key.Length + 1) }
}
New-Item -ItemType Directory -Path $release -Force | Out-Null

Invoke-Docker build -t $newImage $source
Invoke-Docker run --rm --entrypoint python $newImage -m pytest -q
Invoke-Docker image tag $previous $oldImage

$backupPath = "/tmp/linebot-before-$stamp.db"
$localBackup = Join-Path $release "linebot-before-$stamp.db"
@'
import os
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

database = Path(os.environ['DB_PATH']).resolve()
if not database.is_file():
    raise SystemExit('The active database was not found.')
with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as source:
    with closing(sqlite3.connect(sys.argv[1])) as destination:
        source.backup(destination)
'@ | & docker exec -i $existing[0] python - $backupPath
if ($LASTEXITCODE -ne 0) { throw 'Database backup failed. No deployment changes were made.' }
Invoke-Docker cp "$($existing[0]):$backupPath" $localBackup

@"
services:
  linebot:
    image: $newImage
    environment:
      ORCHESTRATOR: 'n8n'
      INTERNAL_MEETING_JOBS: 'false'
      N8N_INTAKE_URL: 'http://n8n:5678/webhook/line-meeting-intake-v3'
      MEETING_SUBMISSION_CHECKS_ENABLED: 'false'
"@ | Set-Content -LiteralPath $overlay -Encoding ascii
@"
services:
  linebot:
    image: $oldImage
    environment:
"@ | Set-Content -LiteralPath $rollback -Encoding ascii
foreach ($key in $restoreEnv.Keys) {
    $value = $restoreEnv[$key].Replace("'", "''")
    "      ${key}: '$value'" | Add-Content -LiteralPath $rollback -Encoding ascii
}

Invoke-Docker compose -f $base -f $overlay config --quiet
try {
    Invoke-Docker compose -f $base -f $overlay up -d --no-deps --no-build --force-recreate linebot
    Wait-Linebot $overlay
    $current = Invoke-Docker compose -f $base -f $overlay ps -q linebot
    @'
import json
import urllib.request

with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5) as response:
    state = json.load(response)
assert state['orchestrator'] == 'n8n' and state['workflow_version'] == 'n8n-v3'
assert not state['internal_meeting_jobs'] and not state['submission_checks_enabled']
request = urllib.request.Request('http://n8n:5678/webhook/line-meeting-controller-ready-v3', data=b'{}', headers={'Content-Type':'application/json'})
with urllib.request.urlopen(request, timeout=20) as response:
    controller = json.load(response)
assert controller['workflow_version'] == 'n8n-v3' and controller['core']['workflow_version'] == 'n8n-v3'
print('RELEASE STATUS: OK')
'@ | & docker exec -i $current python -
    if ($LASTEXITCODE -ne 0) { throw 'The LINE Bot release settings did not match.' }
} catch {
    Write-Warning 'The release failed its health check. Restoring the previous image.'
    Invoke-Docker compose -f $base -f $rollback up -d --no-deps --no-build --force-recreate linebot
    Wait-Linebot $rollback
    Write-Output 'RELEASE STATUS: ROLLED BACK'
    throw
}
