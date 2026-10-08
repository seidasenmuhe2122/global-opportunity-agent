param(
    [switch]$CreateSuperuser
)

$ErrorActionPreference = 'Stop'
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Push-Location $projectRoot

try {
    Write-Host '=== Global Opportunity Agent - Windows Setup ===' -ForegroundColor Cyan

    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if (-not $pythonCommand) {
        throw 'Python was not found. Install Python 3.12 and make sure python.exe is on PATH.'
    }

    foreach ($requiredFile in @('manage.py', 'requirements.txt', '.env.example')) {
        if (-not (Test-Path $requiredFile -PathType Leaf)) {
            throw "Required project file '$requiredFile' was not found."
        }
    }

    if (-not (Test-Path '.venv\Scripts\python.exe')) {
        & $pythonCommand.Source -m venv .venv
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not create the virtual environment.'
        }
    }

    $python = (Resolve-Path '.venv\Scripts\python.exe').Path
    $pythonVersion = & $python -c 'import sys; print(".".join(map(str, sys.version_info[:3])))'
    if ($LASTEXITCODE -ne 0 -or [version]$pythonVersion -lt [version]'3.12') {
        throw "Python 3.12 or newer is required for setup; the virtual environment uses Python $pythonVersion."
    }

    & $python -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not upgrade pip.'
    }
    & $python -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not install project requirements.'
    }
    & $python -m playwright install chromium
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not install Playwright Chromium.'
    }

    if (-not (Test-Path '.env')) {
        Copy-Item '.env.example' '.env'
    }
    if (-not (Test-Path '.env')) {
        throw 'Could not create .env from .env.example.'
    }

    $envContent = [System.IO.File]::ReadAllText((Join-Path $projectRoot '.env'))
    foreach ($key in @('DJANGO_SECRET_KEY', 'CREDENTIAL_ENCRYPTION_KEY')) {
        $pattern = "(?m)^$key\s*=\s*(.*?)\s*$"
        $match = [regex]::Match($envContent, $pattern)
        if (-not $match.Success -or [string]::IsNullOrWhiteSpace($match.Groups[1].Value)) {
            $secret = (& $python -c 'import secrets; print(secrets.token_urlsafe(64))').Trim()
            if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($secret)) {
                throw "Could not generate $key."
            }
            if ($match.Success) {
                $replacement = "$key=$secret"
                $envContent = [regex]::Replace(
                    $envContent,
                    $pattern,
                    [System.Text.RegularExpressions.MatchEvaluator]{
                        param($item)
                        return $replacement
                    },
                    1
                )
            } else {
                $envContent = $envContent.TrimEnd() + "`r`n$key=$secret`r`n"
            }
        }
    }
    $encoding = New-Object System.Text.UTF8Encoding -ArgumentList $false
    [System.IO.File]::WriteAllText((Join-Path $projectRoot '.env'), $envContent, $encoding)

    & $python manage.py migrate
    if ($LASTEXITCODE -ne 0) {
        throw 'Database migrations failed.'
    }
    & $python manage.py setup_roles
    if ($LASTEXITCODE -ne 0) {
        throw 'Could not create the standard roles.'
    }
    & $python manage.py check
    if ($LASTEXITCODE -ne 0) {
        throw 'Django system checks failed.'
    }

    if ($CreateSuperuser) {
        & $python manage.py createsuperuser
        if ($LASTEXITCODE -ne 0) {
            throw 'Could not create the Django superuser.'
        }
    }

    Write-Host ''
    Write-Host 'Setup complete. Start the web app and background workers with:' -ForegroundColor Green
    Write-Host '  .\scripts\start_windows.ps1' -ForegroundColor Cyan
    if (-not $CreateSuperuser) {
        Write-Host 'To create an admin account now, run:' -ForegroundColor Yellow
        Write-Host '  .\.venv\Scripts\python.exe manage.py createsuperuser' -ForegroundColor Yellow
    }
} finally {
    Pop-Location
}
