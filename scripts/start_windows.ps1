param(
    [switch]$SkipRedisContainer,
    [switch]$SkipTelegramBot
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $projectRoot

try {
    $python = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (-not (Test-Path $python)) {
        throw 'Virtual environment not found. Run .\scripts\setup_windows.ps1 first.'
    }
    if (-not (Test-Path '.env')) {
        throw '.env not found. Run .\scripts\setup_windows.ps1 first.'
    }

    $envContent = [System.IO.File]::ReadAllText((Join-Path $projectRoot '.env'))
    if (-not $SkipRedisContainer) {
        $docker = Get-Command docker -ErrorAction SilentlyContinue
        if ($docker) {
            & $docker.Source compose up -d redis
            if ($LASTEXITCODE -ne 0) {
                throw 'Docker could not start Redis. Start Redis yourself or pass -SkipRedisContainer.'
            }
        } else {
            Write-Host 'Docker was not found; expecting Redis to already be running at REDIS_URL.' -ForegroundColor Yellow
        }
    }

    $redisUrl = 'redis://localhost:6379/0'
    $redisMatch = [regex]::Match($envContent, '(?m)^REDIS_URL\s*=\s*(.*?)\s*$')
    if ($redisMatch.Success -and -not [string]::IsNullOrWhiteSpace($redisMatch.Groups[1].Value)) {
        $redisUrl = $redisMatch.Groups[1].Value.Trim()
    }
    $redisUri = [Uri]$redisUrl
    $redisPort = if ($redisUri.Port -gt 0) { $redisUri.Port } else { 6379 }
    $redisReady = $false
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        $client = New-Object System.Net.Sockets.TcpClient
        try {
            $connect = $client.BeginConnect($redisUri.Host, $redisPort, $null, $null)
            if ($connect.AsyncWaitHandle.WaitOne(1000, $false)) {
                $client.EndConnect($connect)
                $redisReady = $true
                break
            }
        } catch {
            Start-Sleep -Seconds 1
        } finally {
            $client.Close()
        }
    }
    if (-not $redisReady) {
        throw "Redis at $($redisUri.Host):$redisPort is not reachable. Start Redis/Docker or pass -SkipRedisContainer only when Redis is already available."
    }

    $celeryApp = 'global_opportunity_agent.celery'
    Write-Host 'Starting Celery worker and Beat in separate windows...' -ForegroundColor Cyan
    Start-Process -FilePath $python -ArgumentList @(
        '-m', 'celery', '-A', $celeryApp, 'worker', '-P', 'solo', '-l', 'info'
    ) -WorkingDirectory $projectRoot
    Start-Process -FilePath $python -ArgumentList @(
        '-m', 'celery', '-A', $celeryApp, 'beat', '-l', 'info',
        '--schedule', (Join-Path $projectRoot 'celerybeat-schedule')
    ) -WorkingDirectory $projectRoot

    if (-not $SkipTelegramBot -and $envContent -match '(?m)^TELEGRAM_BOT_TOKEN\s*=\s*\S+' -and
        $envContent -match '(?m)^TELEGRAM_ADMIN_CHAT_IDS\s*=\s*\S+') {
        Write-Host 'Starting the configured Telegram admin bot...' -ForegroundColor Cyan
        Start-Process -FilePath $python -ArgumentList @(
            'manage.py', 'telegram_bot'
        ) -WorkingDirectory $projectRoot
    }

    Write-Host 'Starting Django at http://127.0.0.1:8000/ (Ctrl+C stops the web server).' -ForegroundColor Green
    & $python manage.py runserver 127.0.0.1:8000
    if ($LASTEXITCODE -ne 0) {
        throw 'Django development server exited with an error.'
    }
} finally {
    Pop-Location
}
