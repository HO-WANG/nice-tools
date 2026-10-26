param([string]$Python = "python", [string]$SapTestClients = "")
$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "Build nice-tools.exe on Windows x64."
}

$Root = Split-Path -Parent $PSScriptRoot
Push-Location $Root
try {
    & $Python -c "import sys, struct, platform; assert sys.version_info[:2] == (3, 12), 'Python 3.12 required'; assert struct.calcsize('P') == 8 and platform.machine().lower() in ('amd64', 'x86_64'), 'Windows x64 Python required'"
    if ($LASTEXITCODE -ne 0) { throw "Unsupported build Python." }

    & $Python -m venv .venv-build
    if ($LASTEXITCODE -ne 0) { throw "Could not create build environment." }
    $BuildPython = Join-Path $Root ".venv-build\Scripts\python.exe"

    & $BuildPython -m pip install --disable-pip-version-check -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Build dependency installation failed." }
    & $BuildPython -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Build dependencies are inconsistent." }

    & $BuildPython -m PyInstaller --noconfirm --clean packaging/nice-tools.spec
    if ($LASTEXITCODE -ne 0) { throw "Executable build failed." }

    $Files = @(Get-ChildItem -LiteralPath (Join-Path $Root "dist") -Force)
    if ($Files.Count -ne 1 -or $Files[0].Name -ne "nice-tools.exe" -or $Files[0].PSIsContainer) {
        throw "Distribution must contain exactly one file: nice-tools.exe."
    }
    $VerificationArgs = @("scripts/verify_bundle.py", "dist/nice-tools.exe")
    if ($SapTestClients) { $VerificationArgs += @("--sap-test-clients", $SapTestClients) }
    & $BuildPython @VerificationArgs
    if ($LASTEXITCODE -ne 0) { throw "Executable verification failed." }
    & $BuildPython scripts/package_windows.py dist/nice-tools.exe release/nice-tools.z01
    if ($LASTEXITCODE -ne 0) { throw "Distribution archive verification failed." }
    Write-Host "Verified executable: dist/nice-tools.exe"
    Write-Host "Distribution: release/nice-tools.z01 (rename to .zip to extract)"
} finally {
    Pop-Location
}
