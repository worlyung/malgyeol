"""실시간(스트리밍) 받아쓰기 처리기.

Lightning-SimulWhisper / whisper_streaming 과 같은 구조입니다.

    insert_audio(chunk)  ->  process_iter()  ->  (확정 단어, 미확정 단어)

확정 방식은 LocalAgreement-2 입니다. 같은 단어가 연속된 두 번의 디코딩에서
같은 자리에 나오면 그때 '확정'합니다. 확정된 텍스트는 다시 바뀌지 않으므로
화면에 그대로 남고, 아직 흔들리는 뒷부분만 회색으로 보여줍니다.
"""
from __future__ import annotations

import logging
import re

import numpy as np

from .base import ASREngine, Word

log = logging.getLogger(__name__)

SR = 16000
SENTENCE_END = tuple(".?!…。？！")
# Whisper 인코더 창은 30초지만, 버퍼가 길수록 한 번 인식이 느려져 지연이 눈덩이처럼 커집니다.
# 쉬지 않고 이어지는 연설에서도 확정되지 않은 소리가 이 길이를 넘으면 한 번 끊어 확정합니다.
MAX_BUFFER_SEC = 15.0
# 알아들은 말이 하나도 없이 소리가 이만큼 쌓이면 잡음·음악으로 보고 앞부분을 버립니다.
NO_WORDS_DROP_SEC = 25.0
# 문장이 끝난 뒤 이만큼 더 들었으면 그 문장은 한 번의 인식으로 확정합니다.
SETTLE_SEC = 1.5

# 두 번의 인식을 비교할 때 무시하는 것: 띄어쓰기와 문장 부호.
# 한국어 인식은 "필요합니다"/"필요합니다.", "16배"/"16 배"처럼 이런 것만 자주 달라지는데,
# 글자 그대로 비교하면 그 한 낱말에서 막혀 뒤의 말이 전혀 확정되지 않았습니다.
_IGNORE = re.compile(r"[\s.,?!…。？！、·'\"“”‘’()\[\]-]")


def _norm(text: str) -> str:
    return _IGNORE.sub("", text or "").lower()


def is_prompt_echo(words: list[Word], prompt: str | None) -> bool:
    """인식 결과가 실제 말이 아니라 힌트(프롬프트)의 이름·용어 목록을 따라 읽은 것인지.

    Whisper 는 가끔 소리를 듣지 않고 "박종민, 홍길동," 처럼 힌트를 그대로 되풀이합니다.
    결과의 낱말 대부분이 힌트에 있는 낱말이면 따라 읽은 것으로 봅니다.
    """
    if not words or not prompt:
        return False
    vocab = {_norm(x) for x in re.split(r"[,\s:]+", prompt) if len(_norm(x)) >= 2}
    toks = [_norm(w.text) for w in words if _norm(w.text)]
    if len(toks) < 2 or not vocab:          # 한 낱말뿐인 짧은 발언은 따라 읽기로 보지 않습니다
        return False
    hit = sum(1 for t in toks if t in vocab)
    return hit / len(toks) >= 0.7


class HypothesisBuffer:
    """연속된 두 가설이 일치하는 앞부분만 확정하는 버퍼."""

    def __init__(self) -> None:
        self.committed_in_buffer: list[Word] = []
        self.buffer: list[Word] = []      # 직전 가설 중 아직 확정되지 않은 부분
        self.new: list[Word] = []
        self.last_committed_time = 0.0
        self.last_committed_word: str | None = None

    def insert(self, words: list[Word]) -> None:
        # 이미 확정된 시각 이전의 단어는 버림
        self.new = [w for w in words if w.start > self.last_committed_time - 0.1]
        if not self.new or not self.committed_in_buffer:
            return
        # 확정된 꼬리와 새 가설 머리가 겹치면(같은 말 반복 출력) 겹친 만큼 제거
        if abs(self.new[0].start - self.last_committed_time) < 1.0:
            for n in range(1, min(len(self.committed_in_buffer), len(self.new), 5) + 1):
                tail = _norm("".join(w.text for w in self.committed_in_buffer[-n:]))
                head = _norm("".join(w.text for w in self.new[:n]))
                if tail == head and tail:
                    del self.new[:n]
                    break

    def flush(self) -> list[Word]:
        """직전 가설과 이번 가설이 (띄어쓰기·부호를 빼고) 똑같이 시작하는 만큼 확정합니다.

        낱말 나눔이 달라도("16배"/"16 배") 글자가 같으면 같은 말로 봅니다.
        """
        prev = "".join(_norm(w.text) for w in self.buffer)
        cur = [_norm(w.text) for w in self.new]
        joined = "".join(cur)
        same = 0
        for a, b in zip(prev, joined):
            if a != b:
                break
            same += 1
        committed: list[Word] = []
        used = 0
        for w, n in zip(self.new, cur):
            if used + len(n) > same:
                break
            used += len(n)
            committed.append(w)
        # 부호만 남은 낱말이 바로 뒤에 붙어 있으면 함께 확정합니다
        while len(committed) < len(self.new) and not cur[len(committed)] and committed:
            committed.append(self.new[len(committed)])
        if committed:
            self.last_committed_word = committed[-1].text
            self.last_committed_time = committed[-1].end
        self.buffer = self.new[len(committed):]
        self.new = []
        self.committed_in_buffer.extend(committed)
        return committed

    def complete(self) -> list[Word]:
        """남은 미확정 단어를 모두 확정(발화 종료 시)."""
        rest = self.buffer
        self.buffer = []
        self.new = []
        if rest:
            self.committed_in_buffer.extend(rest)
            self.last_committed_time = rest[-1].end
            self.last_committed_word = rest[-1].text
        return rest

    def pop_before(self, t: float) -> None:
        while self.committed_in_buffer and self.committed_in_buffer[0].end <= t:
            self.committed_in_buffer.pop(0)


class OnlineASR:
    def __init__(
        self,
        engine: ASREngine,
        prompt_fn=None,
        min_chunk_sec: float = 1.0,
        trim_sec: float = 18.0,
    ) -> None:
        self.engine = engine
        self.prompt_fn = prompt_fn or (lambda tail: None)
        self.min_chunk_sec = min_chunk_sec
        self.trim_sec = trim_sec
        self.reset()

    def reset(self, offset: float = 0.0) -> None:
        self.audio = np.zeros(0, dtype=np.float32)
        self.buffer_time_offset = offset
        self.hypothesis = HypothesisBuffer()
        self.committed: list[Word] = []
        self._since_last_run = 0.0
        self.last_iteration_sec = 0.0

    # --- 입력 ---
    def insert_audio(self, chunk: np.ndarray) -> None:
        self.audio = np.concatenate([self.audio, chunk.astype(np.float32, copy=False)])
        self._since_last_run += len(chunk) / SR

    @property
    def buffer_sec(self) -> float:
        return len(self.audio) / SR

    @property
    def buffer_end_time(self) -> float:
        return self.buffer_time_offset + self.buffer_sec

    def ready(self) -> bool:
        return self._since_last_run >= self.min_chunk_sec and self.buffer_sec >= 0.5

    # --- 프롬프트 ---
    def _prompt(self) -> str | None:
        tail = "".join(w.text for w in self.committed[-60:]).strip()
        return self.prompt_fn(tail)

    # --- 처리 ---
    def process_iter(self, final: bool = False) -> tuple[list[Word], list[Word]]:
        """(이번에 확정된 단어들, 아직 미확정인 단어들)"""
        import time as _t

        if self.buffer_sec < 0.3:
            if final:
                rest = self.hypothesis.complete()
                self.committed.extend(rest)
                return rest, []
            return [], []

        self._since_last_run = 0.0
        t0 = _t.monotonic()
        prompt = self._prompt()
        words = self.engine.transcribe(self.audio, prompt=prompt, final=final)
        self.last_iteration_sec = _t.monotonic() - t0
        if is_prompt_echo(words, prompt):
            # 힌트를 따라 읽은 결과는 버리고, 힌트 없이 곧바로 다시 인식합니다
            log.info("힌트를 따라 읽은 인식 결과를 버리고 다시 인식: %s", "".join(w.text for w in words)[:60])
            words = self.engine.transcribe(self.audio, prompt=None, final=final)
            self.last_iteration_sec = _t.monotonic() - t0
            if is_prompt_echo(words, prompt):
                words = []

        abs_words = [w.shifted(self.buffer_time_offset) for w in words]
        self.hypothesis.insert(abs_words)
        committed = self.hypothesis.flush()
        if not final:
            committed += self._commit_finished_sentences()

        self.committed.extend(committed)

        if final:
            return committed + self._reset_buffer(), []

        self._trim()
        if self.buffer_sec > MAX_BUFFER_SEC:
            # 확정되는 단어 없이 버퍼만 커지는 경우: 지금까지 알아들은 말을 확정하고,
            # 그 뒤의 소리는 버리지 않고 남겨 다음 인식에서 다시 듣습니다.
            # (예전에는 바로 앞에서 조금이라도 잘라 냈으면 이 안전장치를 건너뛰어 1분 넘게 쌓였고,
            #  걸리면 버퍼의 소리를 통째로 버려 그 사이 말이 녹취록에서 빠질 수 있었습니다)
            committed = committed + self._commit_heard()

        return committed, list(self.hypothesis.buffer)

    def _commit_finished_sentences(self) -> list[Word]:
        """이미 끝난 문장은 한 번의 인식만으로 확정합니다.

        두 번의 인식이 같아야 확정하는 방식은 인식 한 번이 오래 걸리는 PC(10초 안팎)에서
        말이 녹취록에 오르기까지 20~30초가 걸립니다. 문장이 끝나고 그 뒤로 말이 1.5초 넘게
        이어졌다면, 그 문장은 앞뒤 소리를 충분히 듣고 인식한 것이라 다시 인식해도 거의 바뀌지 않습니다.
        """
        hb = self.hypothesis
        limit = self.buffer_end_time - SETTLE_SEC
        cut = -1
        for i, w in enumerate(hb.buffer):
            if w.end > limit:
                break
            if w.text.strip().endswith(SENTENCE_END):
                cut = i
        if cut < 0:
            return []
        done = hb.buffer[: cut + 1]
        hb.buffer = hb.buffer[cut + 1:]
        hb.committed_in_buffer.extend(done)
        hb.last_committed_time = done[-1].end
        hb.last_committed_word = done[-1].text
        return done

    def _commit_heard(self) -> list[Word]:
        """미확정 가설을 확정하고, 그 가설이 끝난 지점까지만 소리를 잘라 냅니다."""
        rest = self.hypothesis.complete()
        self.committed.extend(rest)
        if rest:
            cut = rest[-1].end
        elif self.buffer_sec > NO_WORDS_DROP_SEC:  # 한참 동안 알아들은 말이 없으면(잡음·음악) 끝 5초만 남깁니다
            cut = self.buffer_end_time - 5.0
        else:
            return []                           # 소리를 버리지 않고 다음 인식에서 다시 듣습니다
        cut = min(max(cut, self.buffer_time_offset), self.buffer_end_time)
        keep = int((cut - self.buffer_time_offset) * SR)
        self.audio = self.audio[keep:]
        self.buffer_time_offset = cut
        self.hypothesis.committed_in_buffer = []
        self.hypothesis.buffer = []
        self.hypothesis.new = []
        self.hypothesis.last_committed_time = cut
        return rest

    def _reset_buffer(self) -> list[Word]:
        """남은 가설을 모두 확정하고 버퍼를 비운다. 시간축은 이어서 간다."""
        rest = self.hypothesis.complete()
        self.committed.extend(rest)
        end_time = self.buffer_end_time      # 반드시 비우기 '전에' 읽는다
        self.audio = np.zeros(0, dtype=np.float32)
        self.buffer_time_offset = end_time
        self.hypothesis.committed_in_buffer = []
        self.hypothesis.buffer = []
        self.hypothesis.new = []
        self.hypothesis.last_committed_time = end_time
        return rest

    def _trim(self) -> bool:
        """버퍼가 길어지면 이미 확정된 지점(되도록 문장 끝)에서 잘라낸다."""
        if self.buffer_sec <= self.trim_sec:
            return False
        cut: float | None = None
        committed = self.hypothesis.committed_in_buffer
        for w in reversed(committed):
            if w.text.strip().endswith(SENTENCE_END) and w.end < self.buffer_end_time - 1.0:
                cut = w.end
                break
        if cut is None and committed:
            last = committed[-1]
            if last.end < self.buffer_end_time - 1.0:
                cut = last.end
        if cut is None:
            return False
        offset = cut - self.buffer_time_offset
        keep = max(0, int(offset * SR))
        self.audio = self.audio[keep:]
        self.buffer_time_offset = cut
        self.hypothesis.pop_before(cut)
        return True

    def finish(self) -> list[Word]:
        committed, _ = self.process_iter(final=True)
        return committed


class NativeStreamingASR:
    """엔진이 스스로 스트리밍 상태를 관리하는 경우(AlignAtt 등)의 처리기.

    OnlineASR과 같은 인터페이스를 제공하므로 세션 코드는 그대로 씁니다.
    """

    def __init__(self, engine, prompt_fn=None, min_chunk_sec: float = 1.0, trim_sec: float = 0.0):
        self.engine = engine
        self.prompt_fn = prompt_fn or (lambda tail: None)
        self.min_chunk_sec = min_chunk_sec
        self.committed: list[Word] = []
        self.last_iteration_sec = 0.0
        self.reset()

    def reset(self, offset: float = 0.0) -> None:
        self.buffer_time_offset = offset
        self._inserted = offset
        self._since_last_run = 0.0
        self.committed = []
        if hasattr(self.engine, "reset"):
            self.engine.reset()

    def insert_audio(self, chunk: np.ndarray) -> None:
        self.engine.insert_audio(chunk)
        self._inserted += len(chunk) / SR
        self._since_last_run += len(chunk) / SR

    @property
    def buffer_sec(self) -> float:
        return self._since_last_run

    @property
    def buffer_end_time(self) -> float:
        return self._inserted

    def ready(self) -> bool:
        return self._since_last_run >= self.min_chunk_sec

    def process_iter(self, final: bool = False) -> tuple[list[Word], list[Word]]:
        import time as _t

        if hasattr(self.engine, "set_static_prompt"):
            tail = "".join(w.text for w in self.committed[-40:]).strip()
            self.engine.set_static_prompt(self.prompt_fn(tail))
        self._since_last_run = 0.0
        t0 = _t.monotonic()
        words = self.engine.step(final=final)
        self.last_iteration_sec = _t.monotonic() - t0
        self.committed.extend(words)
        return words, []

    def finish(self) -> list[Word]:
        return self.process_iter(final=True)[0]
