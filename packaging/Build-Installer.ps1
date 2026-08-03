param(
    [switch]$UseCurrentPython,
    [string]$PythonCommand = "python",
    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

function Find-InnoCompiler {
    param([string]$RequestedPath = "")
    if ($RequestedPath -and (Test-Path -LiteralPath $RequestedPath)) {
        return (Resolve-Path -LiteralPath $RequestedPath).Path
    }
    $candidates = @()
    if (${env:ProgramFiles(x86)}) {
        $candidates += Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"
    }
    if ($env:ProgramFiles) {
        $candidates += Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe"
    }
    if ($env:LOCALAPPDATA) {
        $candidates += Join-Path $env:LOCALAPPDATA "Programs\Inno Setup 6\ISCC.exe"
    }
    $candidates += Join-Path $projectRoot ".build-tools\Inno Setup 6\ISCC.exe"
    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    return ""
}

function Install-InnoCompiler {
    $toolsDir = Join-Path $projectRoot ".build-tools"
    New-Item -ItemType Directory -Path $toolsDir -Force | Out-Null
    $setupPath = Join-Path $toolsDir "innosetup-6.7.3.exe"
    $downloadUrl = "https://github.com/jrsoftware/issrc/releases/download/is-6_7_3/innosetup-6.7.3.exe"
    Write-Host "Inno Setup 6 was not found. Downloading the signed compiler..."
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -Uri $downloadUrl -OutFile $setupPath -UseBasicParsing
    } catch {
        throw "Could not download Inno Setup 6. Check the internet connection and try again. $($_.Exception.Message)"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $setupPath
    if ($signature.Status -ne "Valid" -or $signature.SignerCertificate.Subject -notlike "*Pyrsys B.V.*") {
        Remove-Item -LiteralPath $setupPath -Force -ErrorAction SilentlyContinue
        throw "The downloaded Inno Setup compiler did not have the expected valid Pyrsys B.V. signature, so it was not opened."
    }
    Unblock-File -LiteralPath $setupPath
    Write-Host "Installing Inno Setup 6 for the current Windows user..."
    $process = Start-Process -FilePath $setupPath -ArgumentList @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/CURRENTUSER"
    ) -Wait -PassThru -WindowStyle Hidden
    if ($process.ExitCode -ne 0) {
        throw "Inno Setup installation failed with exit code $($process.ExitCode)."
    }
    Remove-Item -LiteralPath $setupPath -Force -ErrorAction SilentlyContinue
    $installedCompiler = Find-InnoCompiler
    if (-not $installedCompiler) {
        throw "Inno Setup finished installing, but ISCC.exe could not be found."
    }
    return $installedCompiler
}

if ($UseCurrentPython) {
    $pythonExe = $PythonCommand
} else {
    $venvDir = Join-Path $projectRoot ".build-venv"
    if (-not (Test-Path (Join-Path $venvDir "Scripts\python.exe"))) {
        & $PythonCommand -m venv $venvDir
        if ($LASTEXITCODE -ne 0) { throw "Could not create the build environment." }
    }
    $pythonExe = Join-Path $venvDir "Scripts\python.exe"
    & $pythonExe -m pip install --disable-pip-version-check -r requirements.txt -r requirements-build.txt
    if ($LASTEXITCODE -ne 0) { throw "Could not install the build requirements." }
}

$version = (& $pythonExe -c "import sys; sys.path.insert(0, 'app'); from version import APP_VERSION; print(APP_VERSION)").Trim()
if ($LASTEXITCODE -ne 0 -or -not $version) { throw "Could not read the MesterSync version." }
$versionParts = @($version.Split('.') | ForEach-Object { [int]$_ })
while ($versionParts.Count -lt 4) { $versionParts += 0 }
$versionComma = ($versionParts[0..3] -join ', ')

$buildDir = Join-Path $projectRoot "build"
New-Item -ItemType Directory -Path $buildDir -Force | Out-Null
$versionInfo = @"
VSVersionInfo(
  ffi=FixedFileInfo(filevers=($versionComma), prodvers=($versionComma), mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
    StringStruct('CompanyName', 'AlphaSpeed'),
    StringStruct('FileDescription', 'MesterSync video workflow'),
    StringStruct('FileVersion', '$version'),
    StringStruct('InternalName', 'MesterSync'),
    StringStruct('OriginalFilename', 'MesterSync.exe'),
    StringStruct('ProductName', 'MesterSync'),
    StringStruct('ProductVersion', '$version')
  ])]), VarFileInfo([VarStruct('Translation', [1033, 1200])])]
)
"@
Set-Content -LiteralPath (Join-Path $buildDir "version_info.txt") -Value $versionInfo -Encoding UTF8

$env:MESTERSYNC_PROJECT_ROOT = $projectRoot
& $pythonExe -m PyInstaller --clean --noconfirm (Join-Path $projectRoot "packaging\MesterSync.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller could not build MesterSync." }

$InnoCompiler = Find-InnoCompiler -RequestedPath $InnoCompiler
if (-not $InnoCompiler) {
    $InnoCompiler = Install-InnoCompiler
}

& $InnoCompiler "/DMyAppVersion=$version" (Join-Path $projectRoot "packaging\MesterSync.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup could not build the installer." }

$installer = Join-Path $projectRoot "dist\installer\MesterSync-Setup-$version.exe"
if (-not (Test-Path -LiteralPath $installer)) { throw "The installer was not created." }
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$installer.sha256" -Value "$hash  MesterSync-Setup-$version.exe" -Encoding ASCII
Write-Host "Installer ready: $installer"
