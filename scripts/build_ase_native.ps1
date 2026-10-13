param(
    [string]$Python = "python",
    [string]$CMake = "cmake",
    [string]$Perl = ""
)
$ErrorActionPreference = "Stop"

if ([System.Environment]::OSVersion.Platform -ne [System.PlatformID]::Win32NT -or
    -not [Environment]::Is64BitOperatingSystem) {
    throw "Build ASE native dependencies on Windows x64."
}

$Root = Split-Path -Parent $PSScriptRoot
$Work = Join-Path $Root "build\ase-native-work"
$Stage = Join-Path $Root "build\ase-native"
$NewStage = Join-Path $Work "stage-new"
$Vcpkg = Join-Path $Work "vcpkg"
$FreeTdsCommit = "4db0a7bb97c9c6a476c6d5a9c47283f65685a644"
$FreeTdsSha256 = "0a1d0fa066e23c5236c0085d5df3aa3af90c2e68026d50a9e985176790109feb"
$VcpkgCommit = "e456309491fd875487bf02b8a0dca76c1b1b00cc"
$LibiconvSha512 = "1e8150f9bca907579330cd9c44ebbee46a260271fbe8f50d5ee24a39ef29c8d254505e85c3409324f7440596da711a8bd49e89f848a6be0cb3238a58c24aaecd"
$Utf8 = New-Object System.Text.UTF8Encoding($false)

function Run-Native([string]$File, [string[]]$Arguments) {
    $Command = Get-Command $File -CommandType Application -ErrorAction Stop | Select-Object -First 1
    $AseCommandOutput = @()
    $SavedErrorPreference = $ErrorActionPreference
    try {
        # PowerShell 5 represents redirected native stderr as ErrorRecord items.
        # Continue here preserves all compiler diagnostics and the native exit code.
        $ErrorActionPreference = "Continue"
        & $Command.Source @Arguments 2>&1 | Tee-Object -Variable AseCommandOutput
        $AseCommandExit = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $SavedErrorPreference
    }
    if ($AseCommandExit -ne 0) {
        $Tail = (@($AseCommandOutput | Select-Object -Last 20 | ForEach-Object { $_.ToString() }) -join "`n")
        throw "$File failed with exit code $AseCommandExit.`n$Tail"
    }
}

function Write-Utf8([string]$Path, [string]$Text) {
    [System.IO.File]::WriteAllText($Path, $Text, $Utf8)
}

function Get-VerifiedDownload([string]$Url, [string]$Path, [string]$Algorithm, [string]$Digest) {
    if (-not (Test-Path -LiteralPath $Path)) {
        Invoke-WebRequest -Uri $Url -OutFile $Path -UseBasicParsing
    }
    if ((Get-FileHash -LiteralPath $Path -Algorithm $Algorithm).Hash.ToLowerInvariant() -ne $Digest) {
        throw "Checksum mismatch for $Path; refusing to use the download."
    }
}

New-Item -ItemType Directory -Path $Work -Force | Out-Null
$OldVcpkgRoot = $env:VCPKG_ROOT
Push-Location $Root
try {
    Run-Native $Python @("-c", "import sys, struct; assert sys.version_info[:2] == (3, 12) and struct.calcsize('P') == 8, 'Python 3.12 x64 required'")

    # CMake discovers MSVC itself; locate dumpbin and redistributables without
    # importing or printing a developer shell's entire environment.
    $VsWhere = Join-Path ${env:ProgramFiles(x86)} "Microsoft Visual Studio\Installer\vswhere.exe"
    if (-not (Test-Path -LiteralPath $VsWhere)) { throw "Visual Studio 2022 C++ build tools are required." }
    $VsRoot = & $VsWhere -latest -version "[17.0,18.0)" -products "*" -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
    if ($LASTEXITCODE -ne 0 -or -not $VsRoot) { throw "Visual Studio 2022 x64 C++ tools were not found." }
    $Msvc = Get-ChildItem -LiteralPath (Join-Path $VsRoot "VC\Tools\MSVC") -Directory | Sort-Object Name -Descending | Select-Object -First 1
    $Dumpbin = Join-Path $Msvc.FullName "bin\Hostx64\x64\dumpbin.exe"
    if (-not (Test-Path -LiteralPath $Dumpbin)) { throw "MSVC dumpbin.exe was not found." }
    if (-not $Perl) {
        $PerlCommand = Get-Command perl -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($PerlCommand) { $Perl = $PerlCommand.Source }
        else {
            $GitCommand = Get-Command git -CommandType Application -ErrorAction Stop | Select-Object -First 1
            $GitRoot = Split-Path -Parent (Split-Path -Parent $GitCommand.Source)
            $Perl = Join-Path $GitRoot "usr\bin\perl.exe"
        }
    }
    if (-not (Test-Path -LiteralPath $Perl)) { throw "Perl is required to generate the pinned FreeTDS headers; pass -Perl if needed." }
    Run-Native $Perl @("-e", "exit 0")

    if (-not (Test-Path -LiteralPath (Join-Path $Vcpkg ".git"))) {
        Run-Native "git" @("clone", "--depth=1", "--filter=blob:none", "--no-checkout", "https://github.com/microsoft/vcpkg.git", $Vcpkg)
    }
    Run-Native "git" @("-C", $Vcpkg, "fetch", "--depth=1", "origin", $VcpkgCommit)
    Run-Native "git" @("-C", $Vcpkg, "checkout", "--detach", $VcpkgCommit)
    $ObservedCommit = & git -C $Vcpkg rev-parse HEAD
    if ($LASTEXITCODE -ne 0 -or $ObservedCommit.Trim() -ne $VcpkgCommit) { throw "The vcpkg baseline differs." }
    $env:VCPKG_ROOT = $Vcpkg
    Run-Native (Join-Path $Vcpkg "bootstrap-vcpkg.bat") @("-disableMetrics")
    Run-Native (Join-Path $Vcpkg "vcpkg.exe") @("install", "libiconv:x64-windows", "openssl:x64-windows", "gperf:x64-windows", "--disable-metrics")
    $Installed = Join-Path $Vcpkg "installed\x64-windows"

    $Archive = Join-Path $Work "freetds-$FreeTdsCommit.tar.gz"
    Get-VerifiedDownload "https://codeload.github.com/FreeTDS/freetds/tar.gz/$FreeTdsCommit" $Archive "SHA256" $FreeTdsSha256
    $Source = Join-Path $Work "freetds-$FreeTdsCommit"
    if (Test-Path -LiteralPath $Source) { Remove-Item -LiteralPath $Source -Recurse -Force }
    Run-Native "tar.exe" @("-xzf", $Archive, "-C", $Work)
    Run-Native $Python @((Join-Path $Root "packaging\ase_native_patches.py"), $Source)

    # Upstream FreeTDS CMake uses this explicit path instead of FindIconv.
    New-Item -ItemType Directory -Path (Join-Path $Source "iconv\include"), (Join-Path $Source "iconv\lib") -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Installed "include\iconv.h") -Destination (Join-Path $Source "iconv\include\iconv.h")
    $IconvLibrary = @("libiconv.lib", "iconv.lib") | ForEach-Object { Join-Path $Installed "lib\$_" } | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
    if (-not $IconvLibrary) { throw "GNU libiconv import library was not installed." }
    Copy-Item -LiteralPath $IconvLibrary -Destination (Join-Path $Source "iconv\lib\iconv.lib")
    $Gperf = Get-ChildItem -LiteralPath (Join-Path $Installed "tools\gperf") -Filter gperf.exe -Recurse | Select-Object -First 1
    if (-not $Gperf) { throw "The pinned gperf host tool was not installed." }

    $Build = Join-Path $Work "cmake-build"
    Run-Native $CMake @("-S", $Source, "-B", $Build, "-G", "Visual Studio 17 2022", "-A", "x64",
        "-DCMAKE_TOOLCHAIN_FILE=$Vcpkg/scripts/buildsystems/vcpkg.cmake", "-DVCPKG_TARGET_TRIPLET=x64-windows",
        "-DWITH_OPENSSL=ON", "-DENABLE_MSDBLIB=OFF", "-DENABLE_KRB5=OFF",
        "-DPERL_EXECUTABLE=$Perl", "-DGPERF=$($Gperf.FullName)")
    $NativeConfig = Get-Content -LiteralPath (Join-Path $Build "include\config.h") -Raw
    if ($NativeConfig -notmatch '#define HAVE_ICONV 1' -or $NativeConfig -notmatch '#define HAVE_OPENSSL 1' -or
        $NativeConfig -match '#define MSDBLIB 1') {
        throw "FreeTDS must use GNU iconv, OpenSSL and the Sybase DB-Library ABI."
    }
    Run-Native $CMake @("--build", $Build, "--config", "Release", "--target", "sybdb", "--parallel", "4")

    if (Test-Path -LiteralPath $NewStage) { Remove-Item -LiteralPath $NewStage -Recurse -Force }
    New-Item -ItemType Directory -Path $NewStage -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Build "src\dblib\Release\sybdb.dll") -Destination $NewStage
    Get-ChildItem -LiteralPath (Join-Path $Installed "bin") -Filter *.dll | Copy-Item -Destination $NewStage

    # Include only the CRT DLLs actually imported, taking them from Microsoft's
    # redistributable directory instead of an arbitrary machine's System32.
    $CrtCandidates = @(Get-ChildItem -Path (Join-Path $VsRoot "VC\Redist\MSVC\*\x64\Microsoft.VC*.CRT\*.dll") -File | Sort-Object FullName -Descending)
    $SystemDlls = @("kernel32.dll", "ntdll.dll", "ws2_32.dll", "crypt32.dll", "advapi32.dll", "bcrypt.dll", "user32.dll", "gdi32.dll", "shell32.dll", "secur32.dll", "iphlpapi.dll", "normaliz.dll", "ole32.dll", "oleaut32.dll", "rpcrt4.dll", "comdlg32.dll", "comctl32.dll", "msvcrt.dll", "ucrtbase.dll")
    $Checked = @{}
    do {
        $Pending = @(Get-ChildItem -LiteralPath $NewStage -Filter *.dll | Where-Object { -not $Checked.ContainsKey($_.Name.ToLowerInvariant()) })
        foreach ($Dll in $Pending) {
            $Checked[$Dll.Name.ToLowerInvariant()] = $true
            $Imports = & $Dumpbin /dependents $Dll.FullName
            if ($LASTEXITCODE -ne 0) { throw "Could not inspect imports of $($Dll.Name)." }
            foreach ($Line in $Imports) {
                if ($Line -match '^\s+([^\s]+\.dll)\s*$') {
                    $Name = $Matches[1]
                    $Lower = $Name.ToLowerInvariant()
                    if (Test-Path -LiteralPath (Join-Path $NewStage $Name)) { continue }
                    if ($SystemDlls -contains $Lower -or $Lower -match '^(api|ext)-ms-win-') { continue }
                    $Crt = $CrtCandidates | Where-Object { $_.Name -ieq $Name } | Select-Object -First 1
                    if (-not $Crt) { throw "Unbundled native dependency $Name, imported by $($Dll.Name)." }
                    Copy-Item -LiteralPath $Crt.FullName -Destination $NewStage
                }
            }
        }
    } while ($Pending.Count -gt 0)

    $Licenses = Join-Path $NewStage "licenses"
    $Sources = Join-Path $NewStage "source"
    New-Item -ItemType Directory -Path $Licenses, $Sources -Force | Out-Null
    Copy-Item -LiteralPath (Join-Path $Source "COPYING_LIB.txt") -Destination (Join-Path $Licenses "FreeTDS-LGPL.txt")
    Copy-Item -LiteralPath (Join-Path $Source "COPYING.txt") -Destination (Join-Path $Licenses "FreeTDS-GPL.txt")
    foreach ($Dependency in @("libiconv", "openssl")) {
        Copy-Item -LiteralPath (Join-Path $Installed "share\$Dependency\copyright") -Destination (Join-Path $Licenses "$Dependency-copyright.txt")
    }
    Copy-Item -LiteralPath $Archive -Destination $Sources
    Copy-Item -LiteralPath (Join-Path $Root "packaging\ase_native_patches.py") -Destination $Sources
    Copy-Item -LiteralPath $PSCommandPath -Destination $Sources
    Copy-Item -LiteralPath (Join-Path $Source "nice-tools-patch-report.json") -Destination $Sources

    # Supply the LGPL conversion library's original source and vcpkg patches.
    $IconvArchive = Join-Path $Work "libiconv-1.19.tar.gz"
    $CachedIconvArchive = Join-Path $Vcpkg "downloads\libiconv-1.19.tar.gz"
    if (-not (Test-Path -LiteralPath $IconvArchive) -and (Test-Path -LiteralPath $CachedIconvArchive)) {
        Copy-Item -LiteralPath $CachedIconvArchive -Destination $IconvArchive
    }
    Get-VerifiedDownload "https://ftp.gnu.org/gnu/libiconv/libiconv-1.19.tar.gz" $IconvArchive "SHA512" $LibiconvSha512
    Copy-Item -LiteralPath $IconvArchive -Destination $Sources
    Copy-Item -LiteralPath (Join-Path $Vcpkg "ports\libiconv") -Destination (Join-Path $Sources "libiconv-vcpkg-port") -Recurse
    Write-Utf8 (Join-Path $NewStage "NOTICE.txt") @"
nice-tools includes FreeTDS 1.5.19 (LGPL), GNU libiconv 1.19 (LGPL),
OpenSSL 3.6.5 (Apache-2.0), and required Microsoft Visual C++ runtime DLLs.
The libraries are dynamically linked. Copyright/license texts are in licenses/.
Original FreeTDS/libiconv sources and the applied patches/build script are in source/.
FreeTDS source: https://github.com/FreeTDS/freetds/tree/$FreeTdsCommit
Build/dependency baseline: https://github.com/microsoft/vcpkg/tree/$VcpkgCommit
OpenSSL source: https://github.com/openssl/openssl/tree/openssl-3.6.5
Microsoft runtime redistribution terms are supplied with the Visual Studio build tools:
https://learn.microsoft.com/visualstudio/releases/2022/redistribution
Recipients may rebuild and replace these LGPL libraries; do not restrict debugging
or reverse engineering needed to modify them. The source files are bundled resources,
not programs the end user needs to install or run.
"@

    $CheckCode = @'
import ctypes, json, os, pathlib, struct, sys
sys.path.insert(0, sys.argv[2])
from nice_tools.ase_native import _bind
root = pathlib.Path(sys.argv[1])
for file in root.glob("*.dll"):
    raw = file.read_bytes()
    offset = struct.unpack_from("<I", raw, 0x3c)[0]
    assert raw[offset:offset+4] == b"PE\0\0" and struct.unpack_from("<H", raw, offset+4)[0] == 0x8664, file.name
with os.add_dll_directory(str(root)):
    library = _bind(ctypes.CDLL(str(root / "sybdb.dll")))
    library.dbversion.restype = ctypes.c_char_p
    version = library.dbversion().decode("ascii")
    assert "1.5.19" in version, version
print("Verified x64 DLL loading and DB-Library/BCP exports:", version)
'@
    # A file avoids Windows PowerShell 5.1 stripping quotes in python -c.
    $CheckScript = Join-Path $Work "verify_native.py"
    Write-Utf8 $CheckScript $CheckCode
    Run-Native $Python @($CheckScript, $NewStage, $Root)
    $Files = [ordered]@{}
    Get-ChildItem -LiteralPath $NewStage -Filter *.dll | Sort-Object Name | ForEach-Object {
        $Files[$_.Name] = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
    }
    $Manifest = [ordered]@{
        schema_version = 1
        freetds_version = "1.5.19"
        freetds_commit = $FreeTdsCommit
        source_sha256 = $FreeTdsSha256
        vcpkg_commit = $VcpkgCommit
        compiler_msvc = $Msvc.Name
        patch_revision = 1
        files = $Files
        source_patch_report = Get-Content -LiteralPath (Join-Path $Source "nice-tools-patch-report.json") -Raw | ConvertFrom-Json
    }
    Write-Utf8 (Join-Path $NewStage "native-manifest.json") (($Manifest | ConvertTo-Json -Depth 8) + "`n")
    if (Test-Path -LiteralPath $Stage) { Remove-Item -LiteralPath $Stage -Recurse -Force }
    Move-Item -LiteralPath $NewStage -Destination $Stage
    Write-Host "Verified ASE native DLLs staged in build/ase-native (no database connection tested)."
} catch {
    $Annotation = $_.Exception.Message.Replace('%', '%25').Replace("`r", '%0D').Replace("`n", '%0A')
    Write-Host "::error::$Annotation"
    throw
} finally {
    $env:VCPKG_ROOT = $OldVcpkgRoot
    Pop-Location
}
