"""일레븐랩스(Scribe)로 화자 다시 나누기.

회의가 끝난 뒤 녹음 전체를 일레븐랩스에 보내 '누가 언제 말했는지'만 받아 옵니다.
글자는 말결이 받아쓴 것(용어집 교정·직접 고친 내용 포함)을 그대로 두고,
한 문장 안에서 사람이 바뀌면 그 지점에서 문장을 나눕니다.
"""
from __future__ import annotations

import bisect
import logging
from pathlib import Path

log = logging.getLogger(__name__)
URL = "https://api.elevenlabs.io/v1/speech-to-text"
MODEL = "scribe_v2"


def fetch_words(wav: Path, key: str, language: str = "ko") -> list[tuple[float, float, str]]:
    """녹음을 보내 [(시작, 끝, 화자)] 단어 목록을 받습니다."""
    import httpx

    lang = {"ko": "kor", "en": "eng"}.get(language, language or "kor")
    with open(wav, "rb") as fh:
        resp = httpx.post(
            URL,
            headers={"xi-api-key": key},
            data={"model_id": MODEL, "language_code": lang, "diarize": "true",
                  "timestamps_granularity": "word", "tag_audio_events": "false"},
            files={"file": (wav.name, fh, "audio/wav")},
            timeout=1800,
        )
    if resp.status_code in (401, 403):
        raise RuntimeError("일레븐랩스 API 키가 맞지 않습니다. 설정에서 키를 확인해 주세요.")
    if resp.status_code != 200:
        raise RuntimeError(f"일레븐랩스 요청이 실패했습니다 ({resp.status_code}): {resp.text[:200]}")
    words = [(float(w["start"]), float(w["end"]), str(w.get("speaker_id") or ""))
             for w in resp.json().get("words", []) if w.get("type") == "word"]
    if not words:
        raise RuntimeError("일레븐랩스에서 알아들은 말이 없습니다.")
    return words


class _Timeline:
    """일레븐랩스 단어 목록에서 '이 시각에 말한 사람'을 찾습니다."""

    def __init__(self, words: list[tuple[float, float, str]]):
        self.words = sorted(words)
        self.starts = [w[0] for w in self.words]

    def at(self, t: float) -> str:
        i = bisect.bisect_right(self.starts, t) - 1
        near = self.words[max(0, i):i + 2]
        if i >= 0 and self.words[i][0] <= t <= self.words[i][1]:
            return self.words[i][2]
        # 단어 사이 빈틈이면 가장 가까운 단어의 화자
        return min(near, key=lambda w: min(abs(w[0] - t), abs(w[1] - t)))[2]

    def majority(self, start: float, end: float) -> str:
        spent: dict[str, float] = {}
        lo = bisect.bisect_left(self.starts, start - 30)
        for s, e, spk in self.words[lo:]:
            if s > end:
                break
            overlap = min(e, end) - max(s, start)
            if overlap > 0:
                spent[spk] = spent.get(spk, 0.0) + overlap
        return max(spent, key=spent.get) if spent else self.at((start + end) / 2)


def resplit(segments: list, words: list[tuple[float, float, str]], make) -> list:  # noqa: ANN001
    """문장마다 화자를 다시 붙이고, 문장 안에서 사람이 바뀌면 나눕니다.

    make(**fields) 로 새 문장을 만듭니다. 직접 고쳤거나 용어집으로 교정한 문장은
    글자가 단어 목록과 달라 나누지 않고, 가장 오래 말한 사람을 붙입니다.
    """
    tl = _Timeline(words)
    labels: dict[str, str] = {}

    def label(spk: str) -> str:
        if spk not in labels:
            labels[spk] = f"화자 {len(labels) + 1}"
        return labels[spk]

    out = []
    for seg in segments:
        whole = seg.edited or seg.corrections or not seg.words
        if whole:
            out.append(make(start=seg.start, end=seg.end, text=seg.text, raw_text=seg.raw_text,
                            words=seg.words, corrections=seg.corrections, edited=seg.edited,
                            speaker=label(tl.majority(seg.start, seg.end))))
            continue
        runs: list[list] = []          # [화자, 단어들]
        for w in seg.words:
            spk = tl.at((w["s"] + w["e"]) / 2)
            if runs and runs[-1][0] == spk:
                runs[-1][1].append(w)
            else:
                runs.append([spk, [w]])
        for spk, ws in runs:
            text = "".join(w["w"] for w in ws).strip()
            if text:
                out.append(make(start=ws[0]["s"], end=ws[-1]["e"], text=text, raw_text=text,
                                words=ws, speaker=label(spk)))
    return out


if __name__ == "__main__":    # 간단한 자체 점검
    from types import SimpleNamespace as NS

    words = [(0.0, 1.0, "a"), (1.0, 2.0, "a"), (2.2, 3.0, "b"), (3.0, 4.0, "b")]
    seg = NS(start=0.0, end=4.0, text="가 나 다 라", raw_text="가 나 다 라", edited=False, corrections=[],
             words=[{"s": 0.0, "e": 1.0, "w": " 가"}, {"s": 1.0, "e": 2.0, "w": " 나"},
                    {"s": 2.2, "e": 3.0, "w": " 다"}, {"s": 3.0, "e": 4.0, "w": " 라"}])
    edited = NS(**{**vars(seg), "edited": True, "start": 2.0, "end": 4.0})
    got = resplit([seg, edited], words, lambda **f: f)
    assert [(g["text"], g["speaker"]) for g in got] == [("가 나", "화자 1"), ("다 라", "화자 2"), ("가 나 다 라", "화자 2")], got
    assert _Timeline(words).at(2.15) == "b"
    print("ok")
