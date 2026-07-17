$ErrorActionPreference = "Stop"

$project = Split-Path -Parent $MyInvocation.MyCommand.Path
$assetDirectory = Join-Path $project "assets"
$icon = Join-Path $assetDirectory "patcher.ico"
$output = Join-Path $project "dist\Portal-2-The-Final-Hours-Patcher.exe"
Set-Location -LiteralPath $project

Remove-Item -LiteralPath $output -Force -ErrorAction SilentlyContinue

uv run --with pyinstaller pyinstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --icon "$icon" `
    --workpath "build\work" `
    --specpath "build" `
    --distpath "dist" `
    --name "Portal-2-The-Final-Hours-Patcher" `
    --add-data "$assetDirectory;assets" `
    patcher.py

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

if (-not (Test-Path -LiteralPath $output -PathType Leaf)) {
    throw "PyInstaller finished without creating the expected executable."
}

Write-Host ""
Write-Host "Built: $output"
