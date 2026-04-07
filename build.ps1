# Build panoverlay executable locally
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

Push-Location $PSScriptRoot
try {
    $venvDir = Join-Path $PSScriptRoot '.venv'
    if (-not (Test-Path $venvDir)) {
        Write-Host 'Creating virtual environment...'
        python -m venv $venvDir
    }

    Write-Host 'Activating virtual environment...'
    & (Join-Path $venvDir 'Scripts\Activate.ps1')

    Write-Host 'Installing dependencies...'
    pip install -r requirements.txt

    Write-Host 'Cleaning previous build...'
    foreach ($dir in 'build', 'dist') {
        $path = Join-Path $PSScriptRoot $dir
        if (Test-Path $path) {
            Remove-Item $path -Recurse -Force
            Write-Host "  Removed $dir/"
        }
    }

    Write-Host 'Building executable...'
    pyinstaller panoverlay.spec --noconfirm

    $exe = Join-Path $PSScriptRoot 'dist\panoverlay.exe'
    if (Test-Path $exe) {
        $info = Get-Item $exe
        Write-Host "Build complete: $exe ($([math]::Round($info.Length / 1MB, 1)) MB)"
    } else {
        Write-Error 'Build failed: panoverlay.exe not found in dist/'
    }
} finally {
    Pop-Location
}
