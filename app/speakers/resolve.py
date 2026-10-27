"""화자 결정: 얼굴·목소리·맥락 결과 중 켜 둔 것만으로 화자를 정합니다.

문장마다 방법별 결과를 따로 적어 둡니다 (seg.cands). 화면에 보이는 이름(seg.speaker)은
켜 둔 방법만 보고 그때그때 다시 계산하므로, 녹취 중이든 끝난 뒤든 스위치를 바꾸면
녹취록에 바로 반영됩니다.

  seg.cands   {"face"|"voice"|"context": {"id", "name", "score"}}
  seg.anon    이름 모를 목소리 묶음 ("화자 3")
  seg.follow  바로 앞 발언과 이어지는 말인지 (짧은 대꾸, 같은 목소리)
  seg.checked 이미 따져 본 방법 ("voice", "context") — 나중에 켤 때 빠진 것만 채웁니다
"""
from __future__ import annotations

import logging
import re
from typing import Callable, Iterable

import numpy as np

log = logging.getLogger(__name__)

MODES = ("face", "voice", "context")
UNKNOWN = "화자 미상"


def default_modes(scfg: dict) -> dict:
    return {
        "face": bool(scfg.get("face_enabled", True)),
        "voice": bool(scfg.get("voice_enabled", True)),
        "context": bool(scfg.get("context_enabled", True)),
    }


def clean_modes(modes: dict | None, fallback: dict) -> dict:
    out = dict(fallback)
    for k in MODES:
        if modes and k in modes:
            out[k] = bool(modes[k])
    return out


def migrate(seg) -> None:  # noqa: ANN001
    """이 기능 이전에 만든 녹취록: 지금 붙은 이름을 그 방법의 결과로 옮겨 둡니다."""
    if seg.cands or seg.anon or seg.checked or seg.speaker_source == "manual":
        return
    src = seg.speaker_source
    if src in MODES and seg.speaker_id:
        seg.cands[src] = {"id": seg.speaker_id, "name": seg.speaker, "score": seg.speaker_score}
    elif src == "voice" and re.match(r"^화자\s*\d+$", seg.speaker or ""):
        seg.anon = seg.speaker


def follows(seg, emb, prev_end: float | None, prev_emb, prev_known: bool) -> bool:  # noqa: ANN001
    """명부로 못 알아본 문장이 바로 앞 사람의 말을 잇는지.

    회의에서는 한 사람이 여러 문장을 이어 말하므로, 문장마다 새 화자를 만들지 않도록
    '간격'과 '성문 유사도'를 함께 봅니다.
    """
    if prev_end is None:
        return False
    gap = seg.start - prev_end
    if emb is None:                         # 너무 짧아 성문을 못 뽑은 경우 ("감사합니다")
        return gap < 2.5
    if prev_emb is not None:
        similarity = float(np.dot(prev_emb, emb))
        if (gap < 2.5 and similarity >= 0.42) or (gap < 15.0 and similarity >= 0.55):
            return True
    return seg.end - seg.start < 1.5 and gap < 2.5 and prev_known


# ---------------------------------------------------------------------------- 결과 합치기
# 방법마다 믿을 만한 정도가 다릅니다.
#   맥락: 사회자가 이름을 불러 지명한 것 — 틀리는 일이 드뭅니다.
#   목소리: 성문이 충분히 비슷하면 카메라와 상관없이 맞습니다.
#   얼굴: 발언자를 비추는 경우가 많지만, 의장이 말하는 동안 '듣고 있는 의원'을 비추기도 합니다.
WEIGHT = {"context": .9, "voice": .82, "face": .75}
FACE_TH, FACE_SURE = .36, .62         # 얼굴 유사도: 기준값 ~ 이 정도면 확실
VOICE_TH, VOICE_SURE = .6, .85        # 성문 유사도


def _clamp(x: float) -> float:
    return max(0.0, min(1.0, x))


def confidence(method: str, cand: dict) -> float:
    """한 방법의 결과가 맞을 가능도 (0~1)."""
    s = float(cand.get("score") or 0.0)
    if method == "context":
        conf = .85
    elif method == "face":
        conf = .45 + .55 * _clamp((s - FACE_TH) / (FACE_SURE - FACE_TH))
        if "ratio" in cand:                       # 구간 내내 그 얼굴이 보였는지
            conf *= .55 + .45 * _clamp(float(cand["ratio"]))
    else:
        conf = .4 + .6 * _clamp((s - VOICE_TH) / (VOICE_SURE - VOICE_TH))
    return WEIGHT.get(method, .5) * conf


def fuse(cands: dict, modes: dict) -> tuple[dict | None, str, float, dict]:
    """켜 둔 방법들의 결과를 사람별로 합쳐 가장 그럴듯한 사람을 고릅니다.

    같은 사람을 가리키는 방법이 많을수록 확신이 커집니다 (noisy-OR: 1 − Π(1 − p)).
    돌려주는 값: (고른 결과, 가장 크게 기여한 방법, 합친 확신, 사람별 점수)
    """
    per: dict[str, dict] = {}
    for m in MODES:
        cand = cands.get(m)
        if not modes.get(m) or not cand or not cand.get("id"):
            continue
        p = confidence(m, cand)
        slot = per.setdefault(cand["id"], {"miss": 1.0, "best": (0.0, m, cand)})
        slot["miss"] *= 1 - p
        if p > slot["best"][0]:
            slot["best"] = (p, m, cand)
    if not per:
        return None, "none", 0.0, {}
    scores = {mid: 1 - v["miss"] for mid, v in per.items()}
    winner = max(scores, key=lambda k: (scores[k], per[k]["best"][0]))
    _p, method, cand = per[winner]["best"]
    return cand, method, scores[winner], scores


CLEARED = "cleared"     # seg.checked 에 이 표시가 있으면 사용자가 이름을 지운 문장


def resolve_one(seg, prev, modes: dict, anon_map: dict | None = None) -> bool:  # noqa: ANN001
    """seg 의 표시 이름을 정합니다. 바뀌었으면 True.

    anon_map: 목소리 묶음 번호 → 화면 번호. 이름을 알아본 사람의 묶음은 건너뛰고,
    이름 모를 사람만 나오는 순서대로 '화자 1, 2, 3…'이 되게 합니다.
    """
    if seg.speaker_source == "manual":
        return False
    before = (seg.speaker, seg.speaker_id, seg.speaker_source)
    if CLEARED in seg.checked:
        # 사용자가 '이름 지우기'를 한 문장: 어떤 방법으로도 이름을 다시 붙이지 않고 '화자 N'으로 둡니다
        if seg.anon:
            label = seg.anon if anon_map is None else _anon_label(anon_map, seg.anon)
            seg.speaker, seg.speaker_id, seg.speaker_source, seg.speaker_score = label, None, "voice", 0.0
        else:
            seg.speaker, seg.speaker_id, seg.speaker_source, seg.speaker_score = UNKNOWN, None, "none", 0.0
        return (seg.speaker, seg.speaker_id, seg.speaker_source) != before
    cand, source, score, _ = fuse(seg.cands, modes)
    if cand:
        seg.speaker, seg.speaker_id, seg.speaker_source = cand["name"], cand["id"], source
        seg.speaker_score = round(score, 3)
    elif seg.follow and prev is not None:
        seg.speaker, seg.speaker_id, seg.speaker_source = prev.speaker, prev.speaker_id, prev.speaker_source
        seg.speaker_score = 0.0
    elif modes.get("voice") and seg.anon:
        label = seg.anon if anon_map is None else _anon_label(anon_map, seg.anon)
        seg.speaker, seg.speaker_id, seg.speaker_source, seg.speaker_score = label, None, "voice", 0.0
    else:
        seg.speaker, seg.speaker_id, seg.speaker_source, seg.speaker_score = UNKNOWN, None, "none", 0.0
    return (seg.speaker, seg.speaker_id, seg.speaker_source) != before


def smooth(prev, mid, nxt, modes: dict) -> bool:  # noqa: ANN001
    """앞뒤가 같은 사람인데 가운데 짧은 문장만 근거 약하게 다른 사람이면, 앞뒤를 따릅니다.

    (한 사람이 말하는 도중 카메라가 잠깐 다른 사람을 비추는 경우)
    """
    if not (prev and mid and nxt) or mid.speaker_source == "manual" or CLEARED in mid.checked:
        return False
    if not prev.speaker_id or prev.speaker_id != nxt.speaker_id or mid.speaker_id == prev.speaker_id:
        return False
    if mid.end - mid.start > 4.0 or mid.start - prev.end > 3.0 or nxt.start - mid.end > 3.0:
        return False
    _cand, _src, score, scores = fuse(mid.cands, modes)
    if score >= .6:                                   # 가운데 문장의 근거가 충분히 강하면 그대로 둡니다
        return False
    if scores.get(prev.speaker_id, 0.0) + .25 < score:  # 앞뒤 사람의 근거가 거의 없으면 억지로 붙이지 않습니다
        if score > .35:
            return False
    mid.speaker, mid.speaker_id, mid.speaker_source = prev.speaker, prev.speaker_id, prev.speaker_source
    mid.speaker_score = round(scores.get(prev.speaker_id, 0.0), 3)
    return True


def _anon_label(anon_map: dict, key: str) -> str:
    if key not in anon_map:
        nums = [int(m.group(1)) for v in anon_map.values() if (m := re.match(r"^화자\s*(\d+)$", v))]
        anon_map[key] = f"화자 {max(nums, default=0) + 1}"
    return anon_map[key]


def resolve_all(segments: Iterable, modes: dict, start_after: int = 0, anon_map: dict | None = None) -> list:
    """앞에서부터 차례로 다시 정합니다. (이어짐 판단이 앞 문장 결과에 기대므로)

    anon_map 을 넘기면 새로 채워 돌려줍니다. (녹취 중에는 이어서 들어오는 문장에 씁니다)
    """
    anon_map = {} if anon_map is None else anon_map
    anon_map.clear()
    changed, prev = [], None
    for seg in segments:
        migrate(seg)
        if seg.id <= start_after:
            if seg.speaker_source == "voice" and not seg.speaker_id and seg.anon:
                anon_map.setdefault(seg.anon, seg.speaker)      # 이미 붙은 번호는 그대로 둡니다
        elif resolve_one(seg, prev, modes, anon_map):
            changed.append(seg)
        prev = seg
    segs = list(segments)
    for i in range(1, len(segs) - 1):
        if segs[i].id > start_after and smooth(segs[i - 1], segs[i], segs[i + 1], modes):
            if segs[i] not in changed:
                changed.append(segs[i])
    return changed


# ---------------------------------------------------------------------------- 나중에 채우기
class Backfill:
    """스위치를 나중에 켰을 때, 그동안 따져 보지 않은 문장을 채웁니다.

    목소리: 녹음 파일에서 성문을 뽑아 명부와 대조하고 이름 모를 목소리를 묶습니다.
    맥락  : 지명하는 말투의 문장을 AI에게 묻고, 지명된 사람을 뒤따르는 발언에 붙입니다.
    얼굴  : 화면은 그때만 볼 수 있어 나중에 채울 수 없습니다.
    """

    def __init__(self, segments: list, wav_path, registry, cfg: dict,  # noqa: ANN001
                 embs: dict | None = None, context=None, progress: Callable[[str], None] | None = None):  # noqa: ANN001
        self.segments = segments
        self.wav = wav_path
        self.registry = registry
        self.scfg = cfg["speaker"]
        self.embs = embs if embs is not None else {}
        self.context = context
        self.say = progress or (lambda _m: None)
        self._embedder = None
        self._embedder_tried = False
        self.last_active = None          # 끝까지 살아 있는 지명 (녹취 중이면 이어 붙입니다)

    # -- 성문
    def emb_of(self, seg):  # noqa: ANN001
        if seg.id in self.embs:
            return self.embs[seg.id]
        emb = None
        if seg.end - seg.start >= float(self.scfg.get("min_segment_sec", 1.2)) and self.wav and self.wav.exists():
            if not self._embedder_tried:
                from . import voice as voice_mod

                self._embedder = voice_mod.get_embedder()
                self._embedder_tried = True
            if self._embedder:
                from ..audio import recorder as rec

                try:
                    emb = self._embedder.embed(rec.read_samples(self.wav, seg.start, min(seg.end, seg.start + 12)))
                except Exception:  # noqa: BLE001
                    log.debug("성문 추출 실패", exc_info=True)
        self.embs[seg.id] = emb
        return emb

    def voice(self) -> int:
        todo = [s for s in self.segments if "voice" not in s.checked and s.speaker_source != "manual"]
        if not todo:
            return 0
        from . import voice as voice_mod

        if not voice_mod.get_embedder():
            self.say("목소리 인식 모델이 없어 목소리로 채우지 못했습니다.")
            return 0
        pool = voice_mod.UnknownSpeakerPool(threshold=float(self.scfg.get("voice_threshold", 0.58)))
        todo_ids = {s.id for s in todo}
        # 이미 따져 본 문장의 '화자 N' 과 번호가 겹치지 않게 합니다. (새로 따질 문장의 옛 번호는 버립니다)
        used = [int(m.group(1)) for s in self.segments
                if s.id not in todo_ids and (m := re.match(r"^화자\s*(\d+)$", s.anon or ""))]
        offset = max(used, default=0)
        threshold = float(self.scfg.get("voice_threshold", 0.65))
        min_sec = float(self.scfg.get("voice_match_min_sec", 2.5))
        prev = None
        done = 0
        for i, seg in enumerate(self.segments):
            if seg.id in todo_ids:
                if i % 20 == 0:
                    self.say(f"목소리 확인 중… {i + 1}/{len(self.segments)}")
                emb = self.emb_of(seg)
                if emb is not None:
                    if seg.end - seg.start >= min_sec:
                        mid, score = self.registry.match_voice(emb, threshold)
                        if mid:
                            seg.cands["voice"] = {"id": mid, "name": label_of(self.registry, mid), "score": score}
                    n = int(pool.assign(emb).split()[-1])
                    seg.anon = f"화자 {n + offset}"
                seg.follow = follows(seg, emb, prev.end if prev else None,
                                     self.emb_of(prev) if prev else None, prev is not None)
                seg.checked.append("voice")
                done += 1
            prev = seg
        return done

    # -- 맥락
    def context_scan(self) -> int:
        """지나간 문장에서 자기소개·지명을 찾아 채웁니다. 규칙으로 먼저 찾고, 못 찾은 지명 말투만 AI에게 묻습니다."""
        from .context import Designation, looks_like_designation
        from . import intro as intro_mod

        names = self.registry.names()
        found = 0
        for i, seg in enumerate(self.segments):
            if "context" in seg.checked:
                continue
            seg.checked.append("context")
            me = intro_mod.find_self_intro(seg.text, names)
            if me:
                found += 1
                member = intro_mod.resolve_person(*me, self.registry)
                d = Designation(seg.id - 1, member.id, label_of(self.registry, member.id), None, seg.start,
                                learn=self._learn)
                d.started, d.block_emb = True, self.emb_of(seg)
                # 자기소개 바로 앞의 인사말("반갑습니다" 등)도 같은 목소리로 이어져 있으면 같은 사람으로
                nxt, e0 = seg.start, self.emb_of(seg)
                for s in reversed(self.segments[max(0, i - 2):i]):
                    e = self.emb_of(s)
                    if (s.speaker_source == "manual" or nxt - s.end > 3.0
                            or (e is not None and e0 is not None and float(np.dot(e, e0)) < 0.55)
                            or intro_mod.find_call(s.text, names) or intro_mod.find_self_intro(s.text, names)):
                        break
                    s.cands["context"] = {"id": member.id, "name": d.label, "score": 0.0}
                    nxt = s.start
                for s in self.segments[i:]:
                    if s is not seg and (intro_mod.find_call(s.text, names)
                                         or intro_mod.find_self_intro(s.text, names)):
                        break
                    if s.speaker_source != "manual" and not d.apply(s, self.emb_of(s)) and not d.active:
                        break
                self.last_active = d if d.active else self.last_active
                continue
            called = intro_mod.find_call(seg.text, names)
            if not called and i > 0 and seg.start - self.segments[i - 1].end <= 2.0:
                called = intro_mod.find_call_after(self.segments[i - 1].text, seg.text, names)
            if not called and self.context and looks_like_designation(seg.text, names):
                self.say(f"사회자 지명 확인 중… {i + 1}/{len(self.segments)}")
                members = [f"{m['name']} {m.get('title') or ''}".strip() for m in self.registry.list()]
                history = [f"{s.speaker}: {s.text}" for s in self.segments[max(0, i - 4):i]]
                called = self.context.ask(seg.text, history, members)
            if not called:
                continue
            found += 1
            self.last_active = self.run_designation(i, *called)
        if self.context and self.context.error:
            self.say(f"AI 호출 실패: {self.context.error}")
        return found

    def run_designation(self, index: int, name: str, title: str):  # noqa: ANN201
        """segments[index] 의 지명을 뒤 문장에 붙입니다. 끝까지 살아 있으면 Designation 을 돌려줍니다."""
        from .context import Designation, looks_like_designation

        from . import intro as intro_mod

        caller = self.segments[index]
        member = intro_mod.resolve_person(name, title, self.registry)
        d = Designation(caller.id, member.id, label_of(self.registry, member.id),
                        self.emb_of(caller), caller.end, learn=self._learn)
        names = self.registry.names()
        for seg in self.segments[index + 1:]:
            if not d.active or looks_like_designation(seg.text, names):
                d.active = False
                break
            if seg.speaker_source != "manual":
                d.apply(seg, self.emb_of(seg))
        return d if d.active else None

    def _learn(self, member_id: str, emb) -> None:  # noqa: ANN001
        if self.scfg.get("voice_autoenroll", True):
            self.registry.add_voice(member_id, emb)


def label_of(registry, member_id: str) -> str:  # noqa: ANN001
    m = registry.get(member_id)
    if not m:
        return UNKNOWN
    return f"{m.name} {m.title}".strip() if (m.title or "").strip() else m.name
