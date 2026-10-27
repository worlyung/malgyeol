"""음성인식 모델을 가볍게(int8) 다시 저장합니다. (배포 용량 절반)

프로그램은 CPU에서 모델을 어차피 int8 로 바꿔 쓰므로, 처음부터 int8 로 저장해 두면
인식 결과는 같고 파일만 절반이 됩니다. (1.5GB → 약 0.85GB)

변환 규칙은 CTranslate2 의 int8 규칙을 따릅니다:
  - Linear 행렬(…/weight): 행마다 최댓값으로 나눠 int8 + 배율(…/weight_scale, float32)
  - 나머지 값: float16 그대로

    python -m scripts.slim_whisper <원본 폴더> <새 폴더>
"""
from __future__ import annotations

import shutil
import struct
import sys
from pathlib import Path

import numpy as np

DTYPES = ("float32", "int8", "int16", "int32", "float16", "bfloat16")   # CTranslate2 types.h 순서
OTHER_FILES = ("config.json", "tokenizer.json", "vocabulary.json", "vocabulary.txt", "preprocessor_config.json")


def _read_str(f) -> str:
    n = struct.unpack("H", f.read(2))[0]
    return f.read(n)[:-1].decode("utf-8")


def _write_str(f, s: str) -> None:
    b = s.encode("utf-8")
    f.write(struct.pack("H", len(b) + 1)); f.write(b); f.write(b"\0")


def read_model(path: Path):
    with open(path, "rb") as f:
        version = struct.unpack("I", f.read(4))[0]
        if version != 6:
            raise ValueError(f"모르는 모델 형식입니다 (version {version})")
        name = _read_str(f)
        revision = struct.unpack("I", f.read(4))[0]
        variables = []
        for _ in range(struct.unpack("I", f.read(4))[0]):
            vname = _read_str(f)
            rank = struct.unpack("B", f.read(1))[0]
            shape = struct.unpack(f"{rank}I", f.read(4 * rank)) if rank else ()
            dtype = DTYPES[struct.unpack("B", f.read(1))[0]]
            nbytes = struct.unpack("I", f.read(4))[0]
            if dtype == "bfloat16":
                raise ValueError("bfloat16 모델은 지원하지 않습니다")
            # 통째로 읽지 않고 파일에서 필요한 만큼만 꺼내 씁니다 (메모리 적은 PC 배려)
            if shape:
                variables.append((vname, np.memmap(path, dtype=dtype, mode="r", offset=f.tell(), shape=shape)))
            else:
                variables.append((vname, np.frombuffer(f.read(nbytes), dtype=dtype).reshape(())))
                continue
            f.seek(nbytes, 1)
        aliases = [(_read_str(f), _read_str(f)) for _ in range(struct.unpack("I", f.read(4))[0])]
    return name, revision, variables, aliases


def _int8(v):
    w = np.asarray(v, dtype=np.float32)
    amax = np.abs(w).max(axis=1)
    amax[amax == 0] = 127.0
    scale = (127.0 / amax).astype(np.float32)
    return np.rint(w * scale[:, None]).astype(np.int8), scale


def quantize(variables):
    """(이름, 값을 만드는 함수) 목록. 쓸 때 하나씩 만들어 메모리를 적게 씁니다."""
    names = {n for n, _ in variables}
    out = []
    for vname, v in variables:
        key = vname.rsplit("/", 1)[-1]
        float_v = v.dtype in (np.float16, np.float32)
        # 실행할 때 int8 로 바뀌는 행렬(Linear)만 미리 바꿉니다. 단어표(embeddings)·합성곱(conv)은 그대로 둬야
        # 인식 결과와 속도가 원본과 똑같습니다. (이미 배율이 있으면 이미 양자화된 모델)
        if (key == "weight" and float_v and v.ndim == 2 and "embeddings" not in vname
                and vname + "_scale" not in names):
            out.append((vname, lambda v=v: _int8(v)[0]))
            out.append((vname + "_scale", lambda v=v: _int8(v)[1]))
        elif float_v and v.ndim > 0 and not key.endswith("_scale"):     # 배율은 float32 그대로
            out.append((vname, lambda v=v: np.asarray(v, dtype=np.float16)))
        else:
            out.append((vname, lambda v=v: v))
    out.sort(key=lambda x: x[0])
    return out


def write_model(path: Path, name: str, revision: int, variables, aliases) -> None:
    with open(path, "wb") as f:
        f.write(struct.pack("I", 6)); _write_str(f, name); f.write(struct.pack("I", revision))
        f.write(struct.pack("I", len(variables)))
        for vname, make in variables:
            v = make()
            v = np.ascontiguousarray(v) if v.ndim else np.asarray(v)   # (ascontiguousarray 는 스칼라를 1차원으로 바꿔 버립니다)
            _write_str(f, vname)
            f.write(struct.pack("B", v.ndim))
            for d in v.shape:
                f.write(struct.pack("I", d))
            f.write(struct.pack("B", DTYPES.index(v.dtype.name)))
            f.write(struct.pack("I", v.nbytes))
            f.write(v.tobytes())
        f.write(struct.pack("I", len(aliases)))
        for a, t in aliases:
            _write_str(f, a); _write_str(f, t)


def slim(src: Path, dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    name, rev, variables, aliases = read_model(src / "model.bin")
    tmp = dest / "model.bin.part"
    write_model(tmp, name, rev, quantize(variables), aliases)
    tmp.replace(dest / "model.bin")
    for fname in OTHER_FILES:
        if (src / fname).exists():
            shutil.copy2(src / fname, dest / fname)


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 1
    src, dest = Path(sys.argv[1]), Path(sys.argv[2])
    slim(src, dest)
    before = (src / "model.bin").stat().st_size >> 20
    after = (dest / "model.bin").stat().st_size >> 20
    print(f"완료: {before}MB → {after}MB  ({dest})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
