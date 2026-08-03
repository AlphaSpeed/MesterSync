param(
    [switch]$UseCurrentPython,
    [string]$PythonCommand = "python",
    [string]$InnoCompiler = ""
)

$ErrorActionPreference = "Stop"
$projectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
Set-Location $projectRoot

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

if (-not $InnoCompiler) {
    $candidates = @(
        (Join-Path ${env:ProgramFiles(x86)} "Inno Setup 6\ISCC.exe"),
        (Join-Path $env:ProgramFiles "Inno Setup 6\ISCC.exe")
    ) | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
    if ($candidates.Count -gt 0) { $InnoCompiler = $candidates[0] }
}
if (-not $InnoCompiler -or -not (Test-Path -LiteralPath $InnoCompiler)) {
    throw "Inno Setup 6 was not found. Install it from https://jrsoftware.org/isdl.php and run this build again."
}

& $InnoCompiler "/DMyAppVersion=$version" (Join-Path $projectRoot "packaging\MesterSync.iss")
if ($LASTEXITCODE -ne 0) { throw "Inno Setup could not build the installer." }

$installer = Join-Path $projectRoot "dist\installer\MesterSync-Setup-$version.exe"
if (-not (Test-Path -LiteralPath $installer)) { throw "The installer was not created." }
$hash = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
Set-Content -LiteralPath "$installer.sha256" -Value "$hash  MesterSync-Setup-$version.exe" -Encoding ASCII
Write-Host "Installer ready: $installer"

