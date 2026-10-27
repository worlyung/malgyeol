"""UI 검증용 가상 회의. 사용자 data 폴더에는 쓰지 않습니다."""
import json
import tempfile
import wave
from pathlib import Path


def main():
    root = Path(tempfile.gettempdir()) / 'livecaption-ui-review-20261027'
    samples = [
        ('20261027-ui-demo', '문화예술 행사 운영회의 · UI 검증용', '문화행사'),
        ('20261026-ui-demo', '지역축제 현장 점검 · UI 검증용', '문화행사'),
        ('20261025-ui-demo', '주간 업무회의 · UI 검증용', ''),
    ]
    lines = [
        ('김하늘', '오늘은 가을 문화행사의 운영 계획과 현장 동선을 함께 점검하겠습니다.'),
        ('이도윤', '공연장 입구 안내 인력을 두 명 추가하고, 관객 대기 공간을 넓히는 안을 준비했습니다.'),
        ('김하늘', '좋습니다. 휠체어 이용 관객이 쉽게 이동할 수 있는 통로도 확인해 주세요.'),
        ('박서연', '현장 안내판은 금요일까지 제작하고, 안내 문구는 내일 오전에 공유하겠습니다.'),
        ('이도윤', '자원활동가 교육은 행사 전날 오후 두 시에 진행하는 것으로 정리하겠습니다.'),
        ('김하늘', '담당자별로 일정과 준비 사항을 확인한 뒤 다음 회의에서 다시 점검하겠습니다.'),
    ]
    for sid, title, folder in samples:
        dest = root / 'sessions' / sid
        dest.mkdir(parents=True, exist_ok=True)
        meta = dict(id=sid, title=title, folder=folder, created=1793066400, duration=60, state='finished', segments=len(lines), speaker_modes=dict(face=False, voice=True, context=True))
        segments = [dict(id=i+1, start=i*8, end=i*8+7, text=text, speaker=who, speaker_source='manual', cands={}) for i,(who,text) in enumerate(lines)]
        (dest/'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
        (dest/'transcript.json').write_text(json.dumps({'segments':segments}, ensure_ascii=False), encoding='utf-8')
        (dest/'summary.md').write_text('## 회의 개요\n화면 검증을 위해 작성한 가상 회의입니다.\n\n## 결정 사항\n- 입구 안내 인력 두 명 추가\n- 접근 가능한 관객 동선 확인\n- 금요일까지 안내판 제작\n\n## 다음 할 일\n1. 안내 문구 공유 — 박서연\n2. 자원활동가 교육 준비 — 이도윤', encoding='utf-8')
        with wave.open(str(dest/'audio.wav'), 'wb') as wav:
            wav.setparams((1,2,16000,0,'NONE','not compressed'))
            wav.writeframes(bytes(16000*2*60))
    print(f'UI fixture only: {root}')


if __name__ == '__main__':
    main()
