# 바탕화면에 '회의 받아쓰기' 바로가기를 만듭니다. (처음 한 번)
# first_run.ps1 이 설치를 마친 뒤, 또는 이미 파이썬이 들어 있는 배포 꾸러미를 처음 열 때 부릅니다.
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root 'python'
$pyw = Join-Path $Py 'pythonw.exe'
if (-not (Test-Path $pyw)) { $pyw = Join-Path $Root '.venv\Scripts\pythonw.exe' }

$desk = if ($env:LT_SHORTCUT_DIR) { $env:LT_SHORTCUT_DIR } else { [Environment]::GetFolderPath('Desktop') }   # (시험용)
$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut((Join-Path $desk '말결.lnk'))
$lnk.TargetPath = $pyw
$lnk.Arguments = '-m app.gui'
$lnk.WorkingDirectory = $Root
$lnk.IconLocation = (Join-Path $Root 'assets\malgyeol.ico') + ',0'
$lnk.Description = '말결 · 대화를 기록으로 잇다'
$lnk.Save()
if (Test-Path $Py) { Set-Content -Path (Join-Path $Py '.shortcut') -Value (Get-Date -Format 's') -Encoding ASCII }
