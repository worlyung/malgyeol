# 회의 받아쓰기 — 처음 한 번 설치 (컴퓨터에 아무것도 없어도 됩니다)
#
# '말결 시작.bat' 이 처음 실행될 때 부릅니다. 관리자 권한도, 파이썬 설치도 필요 없습니다.
#   1) 이 폴더 안(python\)에 공식 파이썬(python.org, 설치 불필요판)을 풀고
#   2) 필요한 꾸러미를 받고
#   3) 음성인식·얼굴·화자 모델을 받은 뒤 (약 2GB, 처음 한 번)
#   4) 바탕화면에 바로가기를 만들고 프로그램을 엽니다.
# 중간에 끊겨도 다시 실행하면 이어서 합니다.

# 외부 프로그램(python, pip)은 경고를 화면에 써도 오류로 보지 않고, 끝난 뒤 종료 코드로만 성패를 봅니다.
# (Windows PowerShell 5.1 은 'Stop' 이면 그런 경고만으로도 멈춥니다)
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'          # 진행 막대를 끄면 내려받기가 훨씬 빠릅니다
[Console]::OutputEncoding = [Text.Encoding]::UTF8
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$Py = Join-Path $Root 'python'
$PyVersion = '3.12.10'
$PyUrl = "https://www.python.org/ftp/python/$PyVersion/python-$PyVersion-embed-amd64.zip"
$GetPipUrl = 'https://bootstrap.pypa.io/get-pip.py'
$Log = Join-Path $Root 'data\설치기록.txt'
New-Item -ItemType Directory -Force (Join-Path $Root 'data') | Out-Null

function Say($msg) { Write-Host $msg; Add-Content -Path $Log -Value ("[{0}] {1}" -f (Get-Date -Format 'HH:mm:ss'), $msg) -Encoding UTF8 }
function Step($n, $msg) { Write-Host ''; Say ("[$n/5] " + $msg) }

function Fail($msg) {
    Write-Host ''
    Write-Host '──────────────────────────────────────────' -ForegroundColor Red
    Say ("설치하지 못했습니다: " + $msg)
    Write-Host ''
    Write-Host '다음을 확인한 뒤 이 파일을 다시 실행해 주세요. (이미 받은 것은 다시 받지 않습니다)'
    Write-Host ' - 인터넷에 연결되어 있는지'
    Write-Host ' - 기관망이면 보안 장비가 python.org / github.com / huggingface.co 를 막고 있지 않은지'
    Write-Host ' - 계속 안 되면 data\설치기록.txt 파일을 담당자에게 보내 주세요.'
    Write-Host '──────────────────────────────────────────' -ForegroundColor Red
    exit 1
}

function Download($url, $dest) {
    if ((Test-Path $dest) -and (Get-Item $dest).Length -gt 1000) { return }
    $tmp = "$dest.part"
    for ($i = 1; $i -le 3; $i++) {
        try {
            Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing -TimeoutSec 600 -ErrorAction Stop
            Move-Item -Force $tmp $dest
            return
        } catch {
            Say ("  내려받기 실패($i/3): " + $_.Exception.Message)
            Start-Sleep -Seconds 3
        }
    }
    Fail "$url 을(를) 받지 못했습니다."
}

Write-Host '=============================================='
Write-Host '  말결 — 처음 한 번 설치'
Write-Host '  (10~30분 걸립니다. 창을 닫지 말고 기다려 주세요)'
Write-Host '=============================================='

try {
    # 인터넷에서 받은 폴더에 붙는 '인터넷에서 받은 파일' 표시를 지웁니다 (실행할 때마다 경고가 뜨지 않게)
    Get-ChildItem -Path $Root -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notlike "$Py\*" } |
        Unblock-File -ErrorAction SilentlyContinue

    # 1) 파이썬 (설치 불필요판을 이 폴더 안에 풉니다)
    Step 1 '파이썬 준비 (python.org 공식판, 이 폴더 안에만 풀립니다)'
    if (-not (Test-Path (Join-Path $Py 'python.exe'))) {
        $zip = Join-Path $Root "data\python-$PyVersion.zip"
        Download $PyUrl $zip
        Expand-Archive -Path $zip -DestinationPath $Py -Force -ErrorAction Stop
        Remove-Item $zip -Force
        # 설치 불필요판은 기본으로 꾸러미 폴더를 보지 않습니다. 열어 줍니다.
        Get-ChildItem $Py -Filter 'python*._pth' | ForEach-Object {
            $lines = Get-Content $_.FullName | ForEach-Object { if ($_.Trim() -eq '#import site') { 'import site' } else { $_ } }
            $lines += '..'                    # 프로그램 폴더(app)를 찾을 수 있게
            $lines += 'Lib\site-packages'
            Set-Content -Path $_.FullName -Value $lines -Encoding ASCII
        }
    }
    $PyExe = Join-Path $Py 'python.exe'
    & $PyExe -c "import sys; print('  파이썬', sys.version.split()[0])"

    # 2) pip
    Step 2 '꾸러미 설치 도구 준비'
    if (-not (Test-Path (Join-Path $Py 'Lib\site-packages\pip'))) {
        $getpip = Join-Path $Root 'data\get-pip.py'
        Download $GetPipUrl $getpip
        & $PyExe $getpip --no-warn-script-location -q
        if ($LASTEXITCODE -ne 0) { Fail 'pip 설치에 실패했습니다.' }
        Remove-Item $getpip -Force
    }

    # 3) 필요한 꾸러미 (최신 pip 는 Windows 인증서를 믿어서 기관 보안 장비가 있어도 받아집니다)
    Step 3 '필요한 꾸러미 받는 중 (약 800MB, 5~15분)'
    # 설치 불필요판 파이썬은 pip 의 임시 조립 공간에서 조립 도구를 못 찾으므로, 원본만 있는 꾸러미
    # (프로그램 창용 proxy_tools)는 조립 도구를 먼저 넣고 그것으로 바로 조립합니다.
    & $PyExe -m pip install --no-warn-script-location --disable-pip-version-check -q setuptools wheel
    & $PyExe -m pip install --no-warn-script-location --disable-pip-version-check -q --no-build-isolation proxy_tools
    if ($LASTEXITCODE -ne 0) { Fail '프로그램 창 꾸러미를 준비하지 못했습니다.' }
    & $PyExe -m pip install --no-warn-script-location --disable-pip-version-check `
        -r (Join-Path $Root 'requirements.txt')
    if ($LASTEXITCODE -ne 0) { Fail '꾸러미를 받지 못했습니다.' }

    # 4) 모델 (음성인식 약 1.6GB + 얼굴·화자)
    Step 4 '음성인식·얼굴·화자 모델 받는 중 (약 1.7GB, 5~15분)'
    & $PyExe -m scripts.download_models
    if ($LASTEXITCODE -ne 0) { Fail '모델을 받지 못했습니다.' }

    # 5) 바탕화면 바로가기
    Step 5 '바탕화면에 바로가기 만드는 중'
    & (Join-Path $PSScriptRoot 'make_shortcut.ps1')

    Set-Content -Path (Join-Path $Py '.ready') -Value (Get-Date -Format 's') -Encoding ASCII
    Write-Host ''
    Say '설치가 끝났습니다. 프로그램을 엽니다.'
    Write-Host '다음부터는 바탕화면의 [말결] 아이콘을 두 번 누르면 됩니다.'
    if (-not $env:LT_NO_LAUNCH) {                   # (시험용: 설치만 하고 열지 않기)
        Start-Process -FilePath (Join-Path $Py 'pythonw.exe') -ArgumentList '-m', 'app.gui' -WorkingDirectory $Root
        Start-Sleep -Seconds 4
    }
    exit 0
} catch {
    Fail $_.Exception.Message
}
