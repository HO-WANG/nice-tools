param([string]$Output = "")
$ErrorActionPreference = "Stop"
if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT) {
    throw "Compile the disposable SAP console fixtures on Windows x64."
}
$Root = Split-Path -Parent $PSScriptRoot
if (-not $Output) { $Output = Join-Path $Root "build\sap-test-clients" }
$Output = [System.IO.Path]::GetFullPath($Output)
New-Item -ItemType Directory -Path $Output -Force | Out-Null

# This compiler is for developer validation only. These files are never bundled
# or uploaded as the application distribution and do not emulate an ASE server.
$VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $VsWhere)) { throw "VS 2022 C++ tools are required for console fixtures." }
$VsRoot = & $VsWhere -latest -version "[17.0,18.0)" -products "*" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or -not $VsRoot) { throw "VS 2022 x64 C++ tools were not found." }
$DevShell = Join-Path $VsRoot "Common7\Tools\Microsoft.VisualStudio.DevShell.dll"
Import-Module $DevShell
Enter-VsDevShell -VsInstallPath $VsRoot -SkipAutomaticLocation -DevCmdArguments "-arch=x64 -host_arch=x64"
$Compiler = Get-Command cl.exe -CommandType Application -ErrorAction Stop | Select-Object -First 1
$Source = Join-Path $PSScriptRoot "fixtures\sap_console_client.c"
Push-Location $Output
try {
    foreach ($Tool in @("bcp")) {
        $CompileOutput = & $Compiler.Source /nologo /W4 /WX /O2 /MT /utf-8 /D_CRT_SECURE_NO_WARNINGS $Source "/Fe:$Tool.exe" "/Fo:$Tool.obj" 2>&1
        $CompileExit = $LASTEXITCODE
        $CompileOutput | ForEach-Object { Write-Host $_ }
        if ($CompileExit -ne 0) {
            $Detail = ($CompileOutput | Select-Object -Last 30) -join "`n"
            $Detail = $Detail.Replace('%', '%25').Replace("`r", '%0D').Replace("`n", '%0A')
            Write-Host "::error::Could not compile $Tool console fixture: $Detail"
            throw "Could not compile the $Tool console fixture."
        }
    }
    Write-Host "Built disposable external bcp.exe console fixture (not SAP)."
} finally { Pop-Location }
