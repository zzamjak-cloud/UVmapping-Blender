$ErrorActionPreference = "Stop"
Set-StrictMode -Version 2.0

$ScriptDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $ScriptDirectory ".."))
$AddonId = "uvmapping_blender"
$ProfileName = "UVmappingBlenderDev"
$ManifestPath = Join-Path $RepositoryRoot "blender_manifest.toml"
$BootstrapPath = Join-Path $ScriptDirectory "dev_bootstrap.py"

if (-not (Test-Path -LiteralPath $ManifestPath -PathType Leaf)) {
    throw "blender_manifest.toml을 찾을 수 없습니다: $ManifestPath"
}

$ManifestIdMatch = Select-String -LiteralPath $ManifestPath -Pattern '^\s*id\s*=\s*"uvmapping_blender"\s*$'
if ($null -eq $ManifestIdMatch) {
    throw "매니페스트 id는 uvmapping_blender여야 합니다."
}

if ([string]::IsNullOrWhiteSpace($env:UVMAPPING_BLENDER_BINARY)) {
    $BlenderBinary = Join-Path $RepositoryRoot ".blender\blender.exe"
} else {
    $BlenderBinary = $env:UVMAPPING_BLENDER_BINARY
}
$BlenderBinary = [System.IO.Path]::GetFullPath($BlenderBinary)

if (-not (Test-Path -LiteralPath $BlenderBinary -PathType Leaf)) {
    throw "Blender 실행 파일을 찾을 수 없습니다: $BlenderBinary`nUVMAPPING_BLENDER_BINARY로 프로젝트 전용 포터블 Blender를 지정하세요."
}

$BlenderDirectory = Split-Path -Parent $BlenderBinary
$PortableRoot = Join-Path $BlenderDirectory "portable"
$ExtensionRoot = Join-Path $PortableRoot "extensions\user_default"
$AddonLink = Join-Path $ExtensionRoot $AddonId

New-Item -ItemType Directory -Path $ExtensionRoot -Force | Out-Null

$ExistingItem = Get-Item -LiteralPath $AddonLink -Force -ErrorAction SilentlyContinue
if ($null -ne $ExistingItem) {
    $IsReparsePoint = ($ExistingItem.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0
    if (-not $IsReparsePoint) {
        throw "개발 Extension 위치에 링크가 아닌 항목이 있습니다. 삭제하지 않고 중단합니다: $AddonLink"
    }

    $ExistingTarget = $ExistingItem.Target
    if ($ExistingTarget -is [System.Array]) {
        $ExistingTarget = $ExistingTarget[0]
    }
    if ([string]::IsNullOrWhiteSpace([string]$ExistingTarget)) {
        $ResolvedTarget = ""
    } elseif ([System.IO.Path]::IsPathRooted([string]$ExistingTarget)) {
        $ResolvedTarget = [System.IO.Path]::GetFullPath([string]$ExistingTarget)
    } else {
        $ResolvedTarget = [System.IO.Path]::GetFullPath((Join-Path $ExistingItem.Parent.FullName ([string]$ExistingTarget)))
    }

    if (-not [string]::Equals($ResolvedTarget.TrimEnd('\'), $RepositoryRoot.TrimEnd('\'), [System.StringComparison]::OrdinalIgnoreCase)) {
        Remove-Item -LiteralPath $AddonLink -Force
        $ExistingItem = $null
    }
}

if ($null -eq $ExistingItem) {
    New-Item -ItemType Junction -Path $AddonLink -Target $RepositoryRoot | Out-Null
}

$Mode = "Gui"
$PythonSource = ""
$BlenderArguments = New-Object System.Collections.Generic.List[string]
$ArgumentIndex = 0
while ($ArgumentIndex -lt $args.Count) {
    $CurrentArgument = [string]$args[$ArgumentIndex]
    switch ($CurrentArgument.ToLowerInvariant()) {
        "--mode" {
            $ArgumentIndex += 1
            if ($ArgumentIndex -ge $args.Count) { throw "--mode 값이 필요합니다." }
            $Mode = [string]$args[$ArgumentIndex]
        }
        "-mode" {
            $ArgumentIndex += 1
            if ($ArgumentIndex -ge $args.Count) { throw "-Mode 값이 필요합니다." }
            $Mode = [string]$args[$ArgumentIndex]
        }
        "--script" {
            $ArgumentIndex += 1
            if ($ArgumentIndex -ge $args.Count) { throw "--script 값이 필요합니다." }
            $PythonSource = [string]$args[$ArgumentIndex]
        }
        "-script" {
            $ArgumentIndex += 1
            if ($ArgumentIndex -ge $args.Count) { throw "-Script 값이 필요합니다." }
            $PythonSource = [string]$args[$ArgumentIndex]
        }
        "--" {
            for ($RemainingIndex = $ArgumentIndex + 1; $RemainingIndex -lt $args.Count; $RemainingIndex += 1) {
                $BlenderArguments.Add([string]$args[$RemainingIndex])
            }
            $ArgumentIndex = $args.Count
            continue
        }
        default {
            $BlenderArguments.Add($CurrentArgument)
        }
    }
    $ArgumentIndex += 1
}

$ValidModes = @("gui", "link", "background", "expression", "file")
$NormalizedMode = $Mode.ToLowerInvariant()
if ($ValidModes -notcontains $NormalizedMode) {
    throw "지원하지 않는 모드입니다: $Mode (Gui, Link, Background, Expression, File)"
}

$env:BLENDER_USER_RESOURCES = $PortableRoot
$env:UVMAPPING_REPOSITORY_ROOT = $RepositoryRoot
$env:UVMAPPING_ADDON_ID = $AddonId
$env:UVMAPPING_PROFILE_ROOT = $PortableRoot

Write-Host "개발 프로필: $PortableRoot ($ProfileName)"
Write-Host "Extension 소스: $AddonLink -> $RepositoryRoot"

if ($NormalizedMode -eq "link") {
    exit 0
}

& $BlenderBinary --background --python-exit-code 1 --python $BootstrapPath
$BootstrapExitCode = $LASTEXITCODE
if ($BootstrapExitCode -ne 0) {
    exit $BootstrapExitCode
}

$LaunchArguments = New-Object System.Collections.Generic.List[string]
switch ($NormalizedMode) {
    "background" {
        $LaunchArguments.Add("--background")
    }
    "expression" {
        if ([string]::IsNullOrWhiteSpace($PythonSource)) { throw "Expression 모드에는 --script Python식이 필요합니다." }
        $LaunchArguments.Add("--background")
        $LaunchArguments.Add("--python-expr")
        $LaunchArguments.Add($PythonSource)
    }
    "file" {
        if ([string]::IsNullOrWhiteSpace($PythonSource)) { throw "File 모드에는 --script 파일 경로가 필요합니다." }
        if (-not [System.IO.Path]::IsPathRooted($PythonSource)) {
            $PythonSource = Join-Path $RepositoryRoot $PythonSource
        }
        $PythonSource = [System.IO.Path]::GetFullPath($PythonSource)
        if (-not (Test-Path -LiteralPath $PythonSource -PathType Leaf)) { throw "Python 파일을 찾을 수 없습니다: $PythonSource" }
        $LaunchArguments.Add("--background")
        $LaunchArguments.Add("--python")
        $LaunchArguments.Add($PythonSource)
    }
}

foreach ($BlenderArgument in $BlenderArguments) {
    $LaunchArguments.Add($BlenderArgument)
}

$Automated = $NormalizedMode -in @("background", "expression", "file")
if (-not $Automated) {
    foreach ($LaunchArgument in $LaunchArguments) {
        if ($LaunchArgument -in @("--background", "--python", "--python-expr")) {
            $Automated = $true
            break
        }
    }
}
if ($Automated) {
    $LaunchArguments.Insert(0, "1")
    $LaunchArguments.Insert(0, "--python-exit-code")
}

& $BlenderBinary $LaunchArguments.ToArray()
exit $LASTEXITCODE

