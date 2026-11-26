# 智能剪辑 PR 桥：命令行备用安装器。
# 默认安装当前用户 CEP；可用 -TargetRoot 指定 extensions 根目录。
[CmdletBinding()]
param([string]$TargetRoot = '', [switch]$SkipDebug)

$ErrorActionPreference = 'Stop'
$extensionId = 'com.smartcut.prbridge'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$source = [IO.Path]::GetFullPath((Join-Path $here 'pr_bridge'))

function Get-PluginHashes([string]$Folder) {
    $hashes = @{}
    $prefix = $Folder.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    foreach ($file in Get-ChildItem -LiteralPath $Folder -Recurse -Force) {
        if ($file.Attributes -band [IO.FileAttributes]::ReparsePoint) {
            throw "插件目录包含链接，已停止安装：$($file.FullName)"
        }
        if (-not $file.PSIsContainer) {
            $relative = $file.FullName.Substring($prefix.Length)
            $stream = [IO.File]::OpenRead($file.FullName)
            $sha256 = [Security.Cryptography.SHA256]::Create()
            try {
                $hashes[$relative] = [BitConverter]::ToString($sha256.ComputeHash($stream)).Replace('-', '')
            } finally {
                $sha256.Dispose()
                $stream.Dispose()
            }
        }
    }
    return $hashes
}

function Assert-PluginHashes([string]$Folder, [hashtable]$Expected) {
    $actual = Get-PluginHashes $Folder
    if ($actual.Count -ne $Expected.Count) { throw '插件文件数量校验失败' }
    foreach ($name in $Expected.Keys) {
        if ($actual[$name] -ne $Expected[$name]) { throw "插件哈希校验失败：$name" }
    }
}

function Remove-OwnStaging([string]$Staging, [string]$AllowedRoot) {
    $resolved = [IO.Path]::GetFullPath($Staging)
    $parent = [IO.Path]::GetDirectoryName($resolved)
    $leaf = [IO.Path]::GetFileName($resolved)
    if (-not $parent.Equals($AllowedRoot, [StringComparison]::OrdinalIgnoreCase) -or
        -not $leaf.StartsWith('.smartcut-stage-', [StringComparison]::Ordinal)) {
        throw "拒绝清理非安装暂存目录：$resolved"
    }
    if (Test-Path -LiteralPath $resolved) {
        Remove-Item -LiteralPath $resolved -Recurse -Force
    }
}

try {
    foreach ($entry in @('CSXS\manifest.xml', 'index.html', 'js\main.js', 'js\CSInterface.js', 'jsx\host.jsx')) {
        if (-not (Test-Path -LiteralPath (Join-Path $source $entry) -PathType Leaf)) {
            throw "插件包不完整：缺少 $entry"
        }
    }
    if (-not $TargetRoot.Trim()) {
        $TargetRoot = Join-Path $env:APPDATA 'Adobe\CEP\extensions'
    }
    $root = [IO.Path]::GetFullPath([Environment]::ExpandEnvironmentVariables($TargetRoot)).TrimEnd('\', '/')
    if ([IO.Path]::GetFileName($root) -eq $extensionId) {
        $root = [IO.Path]::GetDirectoryName($root)
    }
    $target = [IO.Path]::GetFullPath((Join-Path $root $extensionId))
    $sourcePrefix = $source.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if ($root.Equals($source, [StringComparison]::OrdinalIgnoreCase) -or
        $root.StartsWith($sourcePrefix, [StringComparison]::OrdinalIgnoreCase) -or
        $target.Equals($source, [StringComparison]::OrdinalIgnoreCase)) {
        throw '安装目标不能覆盖或位于插件源目录内'
    }
    if (($root + '\').Equals([IO.Path]::GetPathRoot($root), [StringComparison]::OrdinalIgnoreCase)) {
        throw '请选择 CEP extensions 文件夹，不能安装到磁盘根目录'
    }
    if (Test-Path -LiteralPath $target) {
        $existing = Get-Item -LiteralPath $target -Force
        if (-not $existing.PSIsContainer -or
            ($existing.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw "现有目标不是普通插件目录：$target"
        }
    }
    $expected = Get-PluginHashes $source
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $lockPath = Join-Path $root '.smartcut-install.lock'
    $lockStream = [IO.File]::Open($lockPath, [IO.FileMode]::CreateNew,
                               [IO.FileAccess]::Write, [IO.FileShare]::None)
    $lockStream.Dispose()
    $staging = Join-Path $root ('.smartcut-stage-' + [guid]::NewGuid().ToString('N'))
    $backup = $null
    $published = $false
    $restoreSucceeded = $true
    try {
        Copy-Item -LiteralPath $source -Destination $staging -Recurse -Force
        Assert-PluginHashes $staging $expected
        if (Test-Path -LiteralPath $target) {
            $backupRoot = Join-Path ([IO.Path]::GetDirectoryName($root)) 'SmartCut-CEP备份'
            $backupRoot = Join-Path $backupRoot $extensionId
            New-Item -ItemType Directory -Path $backupRoot -Force | Out-Null
            $stamp = Get-Date -Format 'yyyyMMdd_HHmmss_fffffff'
            $backup = Join-Path $backupRoot ($stamp + '_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
            Move-Item -LiteralPath $target -Destination $backup
        }
        try {
            Move-Item -LiteralPath $staging -Destination $target
            $published = $true
            Assert-PluginHashes $target $expected
        } catch {
            $installError = $_
            try {
                if ($published) { Move-Item -LiteralPath $target -Destination $staging }
                if ($backup) { Move-Item -LiteralPath $backup -Destination $target }
            } catch {
                $restoreSucceeded = $false
                throw "无法自动恢复，请从备份恢复：$backup；$($_.Exception.Message)"
            }
            throw $installError
        }
    } finally {
        if ($restoreSucceeded -and $staging) { Remove-OwnStaging $staging $root }
        if (Test-Path -LiteralPath $lockPath) { Remove-Item -LiteralPath $lockPath -Force }
    }
    Write-Host "已安装并校验：$target" -ForegroundColor Green
    if ($backup) { Write-Host "旧版备份：$backup" }
    foreach ($version in $(if ($SkipDebug) { @() } else { 8..12 })) {
        try {
            $key = "HKCU:\Software\Adobe\CSXS.$version"
            New-Item -Path $key -Force | Out-Null
            New-ItemProperty -Path $key -Name 'PlayerDebugMode' -Value '1' -PropertyType String -Force | Out-Null
        } catch {
            Write-Warning "插件已安装，但 CSXS.$version 调试开关设置失败：$($_.Exception.Message)"
        }
    }
    Write-Host '请手动重启 Premiere，再从“窗口 → 扩展（旧版）→ 智能剪辑桥”打开。' -ForegroundColor Cyan
    Write-Host '旧 PR 不支持自动字幕轨时，简繁 SRT 会导入项目面板，工程照常保存。'
    Write-Host '署名与贡献：软件安装目录 NOTICE.md'
} catch {
    Write-Host "安装未完成：$($_.Exception.Message)" -ForegroundColor Red
    Write-Host '系统目录权限不足时，请使用默认的当前用户 CEP 目录，或以管理员权限运行。'
    exit 1
}
