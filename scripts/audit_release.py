"""공개 배포 전 추적 파일과 설치 꾸러미의 민감정보 포함 여부를 검사합니다."""
import json
import re
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def audit(source_only=False):
    tracked = subprocess.check_output(['git', 'ls-files', '-z'], cwd=ROOT).decode('utf-8').split('\0')
    secrets = []
    env = ROOT / 'data/.env'
    if env.exists():
        for line in env.read_text(encoding='utf-8-sig').splitlines():
            if '=' in line and not line.lstrip().startswith('#'):
                value = line.split('=', 1)[1].strip().strip('\"\'')
                if len(value) >= 12:
                    secrets.append(value.encode())
    pattern = re.compile(rb'(?:sk-(?:proj-)?[A-Za-z0-9_-]{24,}|gh[pousr]_[A-Za-z0-9]{25,}|AIza[A-Za-z0-9_-]{30,}|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)')
    issues = []
    for name in filter(None, tracked):
        path = Path(name)
        if path.name in {'.env', 'credentials.json', 'token.json'} or name.startswith('data/'):
            issues.append(name + ': excluded data file')
        data = (ROOT / path).read_bytes()
        if pattern.search(data) or any(secret in data for secret in secrets):
            issues.append(name + ': possible credential')
    if source_only:
        print(json.dumps({'tracked_files': len(list(filter(None, tracked))), 'issues': issues}, ensure_ascii=False))
        if issues:
            raise SystemExit(1)
        return
    payload = ROOT / '배포/_빌드/payload.zip'
    with zipfile.ZipFile(payload) as archive:
        names = archive.namelist()
        for name in names:
            if name.startswith('data/') and not (name.startswith('data/models/') or name == 'data/sessions/'):
                issues.append(name + ': unexpected user data')
            if Path(name).name in {'.env', 'credentials.json', 'token.json'}:
                issues.append(name + ': excluded credential file')
        for name in names:
            if name.startswith(('app/', 'web/', 'scripts/')) and not name.endswith('/'):
                data = archive.read(name)
                if pattern.search(data) or any(secret in data for secret in secrets):
                    issues.append(name + ': possible credential in payload')
        assert b'VERSION = "1.01"' in archive.read('app/version.py')
        assert 'LICENSE' in names
    result = {'tracked_files': len(list(filter(None, tracked))), 'payload_files': len(names), 'issues': issues}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if issues:
        raise SystemExit(1)


if __name__ == '__main__':
    import sys
    audit('--source-only' in sys.argv)
