param(
    [switch]$NoModel,
    [switch]$NoLlm,
    [switch]$NoShortcut,
    [switch]$Check,
    [switch]$NoPremierePlugin,
    [switch]$InstallPremierePlugin,
    [switch]$ChooseInstallDirectory
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -LiteralPath $root

function Test-PythonCommand {
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$Prefix = @()
    )
    # Windows PowerShell 5 turns some native stderr output (notably
    # ``py -3.12`` when that runtime is absent) into a NativeCommandError.
    # A failed candidate is expected during discovery and must not abort the
    # whole bootstrap while $ErrorActionPreference is Stop.
    $previousPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'SilentlyContinue'
        & $Exe @Prefix -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    } finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Find-Python {
    foreach ($relative in @('.venv\Scripts\python.exe', 'runtime\python\python.exe')) {
        $candidate = Join-Path $root $relative
        if (Test-Path -LiteralPath $candidate -PathType Leaf) {
            if (Test-PythonCommand -Exe $candidate) {
                return @{ Exe = $candidate; Prefix = @() }
            }
        }
    }
    $launcher = Get-Command py -ErrorAction SilentlyContinue
    if ($launcher) {
        if (Test-PythonCommand -Exe $launcher.Source -Prefix @('-3.12')) {
            return @{ Exe = $launcher.Source; Prefix = @('-3.12') }
        }
        if (Test-PythonCommand -Exe $launcher.Source -Prefix @('-3')) {
            return @{ Exe = $launcher.Source; Prefix = @('-3') }
        }
    }
    foreach ($name in @('python', 'python3')) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if ($command) {
            if (Test-PythonCommand -Exe $command.Source) {
                return @{ Exe = $command.Source; Prefix = @() }
            }
        }
    }
    foreach ($parent in @((Join-Path $env:LOCALAPPDATA 'Programs\Python'), $env:ProgramFiles)) {
        if (Test-Path -LiteralPath $parent) {
            $candidates = Get-ChildItem -LiteralPath $parent -Directory -Filter 'Python3*' -ErrorAction SilentlyContinue |
                Sort-Object Name -Descending
            foreach ($directory in $candidates) {
                $candidate = Join-Path $directory.FullName 'python.exe'
                if (Test-Path -LiteralPath $candidate -PathType Leaf) {
                    if (Test-PythonCommand -Exe $candidate) {
                        return @{ Exe = $candidate; Prefix = @() }
                    }
                }
            }
        }
    }
    return $null
}

$requiredFiles = @('installer.py', 'product_info.py', 'requirements.txt',
                   'constraints-runtime.txt', 'app_main.py', '启动SmartCut.bat',
                   'brains\__init__.py', 'conf\decision.json',
                   'conf\coords_matrix.json', 'conf\spec_manifest.json',
                   'conf\editor_model.json')
if ($ChooseInstallDirectory) { $requiredFiles += '安装器.py' }
foreach ($required in $requiredFiles) {
    if (-not (Test-Path -LiteralPath (Join-Path $root $required) -PathType Leaf)) {
        Write-Host "安装包不完整，缺少：$required。请重新下载完整项目。" -ForegroundColor Red
        exit 2
    }
}

$packagedExe = Join-Path $root '智能剪辑助手.exe'
$packagedInternal = Join-Path $root '_internal'
$packagedReady = (Test-Path -LiteralPath $packagedExe -PathType Leaf) -and
    (Test-Path -LiteralPath $packagedInternal -PathType Container) -and
    (@(Get-ChildItem -LiteralPath $packagedInternal -Filter 'python*.dll' -File -ErrorAction SilentlyContinue).Count -gt 0)
if ($packagedReady) {
    $python = @{ Exe = $packagedExe; Prefix = @('--run-script') }
    Write-Host '检测到成品程序，将由 EXE 自举安装；无需本机 Python。' -ForegroundColor Cyan
} else {
    $python = Find-Python
}
if (-not $python -and -not $Check) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Write-Host '未找到 Python，正在为当前用户安装 Python 3.12…' -ForegroundColor Cyan
        & $winget.Source install --exact --id Python.Python.3.12 --scope user `
            --accept-package-agreements --accept-source-agreements --disable-interactivity
        if ($LASTEXITCODE -eq 0) {
            $python = Find-Python
        }
    }
}
if (-not $python -and -not $Check) {
    $pythonVersion = '3.12.10'
    $pythonInstaller = Join-Path $env:TEMP "smartcut-python-$pythonVersion-amd64.exe"
    $pythonUrl = "https://www.python.org/ftp/python/$pythonVersion/python-$pythonVersion-amd64.exe"
    try {
        Write-Host 'winget 不可用或安装失败，正在从 Python 官方下载备用安装器…' -ForegroundColor Cyan
        Invoke-WebRequest -UseBasicParsing -Uri $pythonUrl -OutFile $pythonInstaller
        $signature = Get-AuthenticodeSignature -LiteralPath $pythonInstaller
        $subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
        if ($signature.Status -ne 'Valid' -or $subject -notmatch 'Python Software Foundation') {
            throw "Python 官方安装器签名验证失败：$($signature.Status) $subject"
        }
        $process = Start-Process -FilePath $pythonInstaller -Wait -PassThru -WindowStyle Hidden `
            -ArgumentList @('/quiet', 'InstallAllUsers=0', 'PrependPath=1',
                            'Include_test=0', 'Include_launcher=1')
        if ($process.ExitCode -eq 0) {
            $python = Find-Python
        }
    } catch {
        Write-Host "Python 官方安装器自动安装失败：$($_.Exception.Message)" -ForegroundColor Yellow
    } finally {
        Remove-Item -LiteralPath $pythonInstaller -Force -ErrorAction SilentlyContinue
    }
}
if (-not $python) {
    Write-Host '未找到可用的 Python 3.10+。请安装 Python 3.12，并勾选 Add python.exe to PATH，随后重新运行本脚本。' -ForegroundColor Red
    Write-Host '下载地址：https://www.python.org/downloads/windows/'
    if ($Check) { Write-Host '当前为只读体检，没有安装 Python 或下载依赖。' }
    exit 2
}

$entryScript = if ($ChooseInstallDirectory) { '安装器.py' } else { 'installer.py' }
$argsList = @($python.Prefix) + @($entryScript)
if ($Check) {
    if ($ChooseInstallDirectory) {
        Write-Host '-Check 不能与 -ChooseInstallDirectory 同时使用。' -ForegroundColor Red
        exit 2
    }
    # Keep the read-only health check literal: importing installer modules must
    # not leave __pycache__ behind in a freshly extracted release.
    $env:PYTHONDONTWRITEBYTECODE = '1'
    $argsList += '--check'
} else {
    if (-not $ChooseInstallDirectory) {
        if ($NoModel) { $argsList += '--no-model' }
        if ($NoLlm) { $argsList += '--no-llm' }
        if ($NoShortcut) { $argsList += '--no-shortcut' }
        if ($NoPremierePlugin) { $argsList += '--no-premiere-plugin' }
    }
}

Write-Host '正在配置 SmartCut 智能剪辑助手…' -ForegroundColor Cyan
if ($packagedReady) {
    # GUI 子系统 EXE 由 PowerShell 的调用运算符启动时不会可靠设置
    # $LASTEXITCODE；等待进程并直接读取 ExitCode，避免把成功误报成失败。
    $process = Start-Process -FilePath $python.Exe -ArgumentList $argsList `
        -WorkingDirectory $root -Wait -PassThru
    $installerExitCode = $process.ExitCode
} else {
    & $python.Exe @argsList
    $installerExitCode = $LASTEXITCODE
}
if ($installerExitCode -ne 0) {
    if ($Check) {
        Write-Host "体检发现未就绪项，退出码：$installerExitCode；未执行安装或下载。" -ForegroundColor Yellow
    } else {
        Write-Host "安装未完成，退出码：$installerExitCode。请查看 logs 目录。" -ForegroundColor Red
    }
    exit $installerExitCode
}

if (-not $Check -and -not $ChooseInstallDirectory) {
    if ($InstallPremierePlugin) {
        Write-Host 'Premiere 插件现已默认自动扫描安装，无需再传 -InstallPremierePlugin。' -ForegroundColor DarkGray
    }
    Write-Host '安装完成，SmartCut 已启动。' -ForegroundColor Green
}

