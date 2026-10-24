param([switch]$NoBrowser, [int]$Port = 0)
$ErrorActionPreference = 'Stop'
$toolDirectory = $PSScriptRoot
$projectDirectory = Split-Path -Parent $toolDirectory
$candidates = @(
    (Join-Path $projectDirectory '.venv\Scripts\python.exe'),
    (Join-Path $toolDirectory '.venv\Scripts\python.exe')
)
$pythonCommand = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pythonCommand -and $pythonCommand.Source -notlike '*\WindowsApps\*') {
    $candidates += $pythonCommand.Source
}
$pyCommand = Get-Command py.exe -ErrorAction SilentlyContinue
if ($pyCommand) {
    $pyExecutable = & $pyCommand.Source -3 -c 'import sys; print(sys.executable)'
    if ($LASTEXITCODE -eq 0 -and $pyExecutable) { $candidates += $pyExecutable.Trim() }
}
$pythonPath = $null
foreach ($candidate in ($candidates | Select-Object -Unique)) {
    if (Test-Path -LiteralPath $candidate) {
        & $candidate -c 'import sys, PIL, fitz; sys.exit(0 if sys.version_info >= (3, 10) else 1)'
        if ($LASTEXITCODE -eq 0) { $pythonPath = $candidate; break }
    }
}
if (-not $pythonPath) {
    Write-Host '需要 Python 3.10+、Pillow 和 PyMuPDF。请按照项目 README 安装运行依赖。'
    exit 1
}
$windowlessPython = Join-Path (Split-Path -Parent $pythonPath) 'pythonw.exe'
if (Test-Path -LiteralPath $windowlessPython) { $pythonPath = $windowlessPython }
$serverPath = Join-Path $toolDirectory 'server.py'
$serverArguments = @(('"' + $serverPath + '"'), '--port', "$Port")
if ($NoBrowser) { $serverArguments += '--no-browser' }
Start-Process -FilePath $pythonPath -ArgumentList $serverArguments -WorkingDirectory $toolDirectory -WindowStyle Hidden
