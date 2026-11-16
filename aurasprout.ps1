$ErrorActionPreference = "Stop"

$FoundPython = $null

function Test-ValidPython($ExePath) {
    if (-not $ExePath -or -not (Test-Path $ExePath)) { return $false }
    try {
        $item = Get-Item $ExePath -ErrorAction SilentlyContinue
        # Filter out 0-byte WindowsApps store redirection stubs
        if ($item.Length -eq 0) { return $false }
        # Check execution
        $res = & $ExePath -c "import sys; print(sys.version_info[0])" 2>$null
        if ($LASTEXITCODE -eq 0 -and $res -match "^\d+") {
            return $true
        }
    } catch {}
    return $false
}

# 1. Try PATH
$PythonInPath = Get-Command "python.exe" -ErrorAction SilentlyContinue
if ($PythonInPath -and (Test-ValidPython $PythonInPath.Source)) {
    $FoundPython = $PythonInPath.Source
}

# 2. Try Registry PEP 514
if (-not $FoundPython) {
    $RegistryPaths = @(
        "HKCU:\Software\Python\PythonCore",
        "HKLM:\Software\Python\PythonCore"
    )
    
    foreach ($RegPath in $RegistryPaths) {
        if (Test-Path $RegPath) {
            $Versions = Get-ChildItem -Path $RegPath -ErrorAction SilentlyContinue
            foreach ($Version in $Versions) {
                $InstallPathKey = Join-Path -Path $Version.PSPath -ChildPath "InstallPath"
                if (Test-Path $InstallPathKey) {
                    $InstallPath = (Get-ItemProperty -Path $InstallPathKey).'(default)'
                    $ExePath = Join-Path -Path $InstallPath -ChildPath "python.exe"
                    if (Test-ValidPython $ExePath) {
                        $FoundPython = $ExePath
                        break
                    }
                }
            }
        }
        if ($FoundPython) { break }
    }
}

# 3. Check for bootstrapped Python in sibling auraseed or user dirs
$SiblingSeed = Join-Path $PSScriptRoot "..\auraseed"
if (-not $FoundPython -and (Test-Path $SiblingSeed)) {
    $CheckRepoExe = Join-Path $SiblingSeed ".bootstrapped_python\python.exe"
    if (Test-ValidPython $CheckRepoExe) {
        $FoundPython = $CheckRepoExe
    }
}

if (-not $FoundPython) {
    Write-Error "[AuraSprout Bootstrapper] Error: Could not locate a valid base Python executable on the host."
    exit 1
}

# Ensure aurasprout/src and sibling core libraries are present on PYTHONPATH
$SrcDir = Join-Path $PSScriptRoot "src"
$PythonPaths = @($SrcDir)

$ParentDir = Split-Path $PSScriptRoot -Parent
foreach ($Dep in @("auraseed", "auragrid-api", "auragrid-py", "aurarouter", "auracode")) {
    $DepDir = Join-Path $ParentDir $Dep
    if (Test-Path $DepDir) {
        $DepSrc = Join-Path $DepDir "src"
        if (Test-Path $DepSrc) {
            $PythonPaths += $DepSrc
        } else {
            $PythonPaths += $DepDir
        }
    }
}

if ($env:PYTHONPATH) {
    $env:PYTHONPATH = (($PythonPaths -join ";") + ";$env:PYTHONPATH")
} else {
    $env:PYTHONPATH = ($PythonPaths -join ";")
}

# Execute the aurasprout module directly
& $FoundPython -m aurasprout $args
exit $LASTEXITCODE

