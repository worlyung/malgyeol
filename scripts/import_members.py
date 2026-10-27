"""의원 명부·얼굴 가져오기.

세 가지 방법이 있습니다.

1) 충청남도의회 누리집에서 통째로 가져오기 (이름 + 공식 사진 + 얼굴 특징)
       python -m scripts.import_members --council chungnam

2) 다른 의회 누리집에서 가져오기 (의원 목록이 있는 주소를 직접 지정)
       python -m scripts.import_members --list-url https://council.xxx.go.kr/

3) 사진 폴더에서 가져오기 (파일명이 곧 이름: 김철수.jpg)
       python -m scripts.import_members --photos "C:/사진폴더"

공식 사진은 정면 스튜디오 사진이라 중계 화면과 각도가 달라, 처음에는 인식률이
낮을 수 있습니다. 실제 중계 화면에서 한두 장 더 등록하면 훨씬 좋아집니다.
(웹 화면 → 의원 명부 → "화면에서 얼굴 등록")
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402

from app import paths  # noqa: E402
from app.glossary import Glossary  # noqa: E402
from app.speakers.registry import registry  # noqa: E402

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36")

COUNCILS = {
    "chungnam": {
        "list_url": "https://council.chungnam.go.kr/",
        "link_re": r'href="(/member/(\d+)/main\.do)"[^>]*>\s*([가-힣]{2,4})\s*<',
        "photo_re": r'(/attach/member/PR/[^"\']+\.(?:jpg|jpeg|png|JPG))',
    },
}


def fetch(url: str, referer: str = "") -> bytes:
    headers = {"User-Agent": UA, "Accept-Language": "ko-KR,ko;q=0.9"}
    if referer:
        headers["Referer"] = referer
    resp = httpx.get(url, headers=headers, timeout=30, follow_redirects=True)
    resp.raise_for_status()
    return resp.content


def enroll_photo(name: str, title: str, image_bytes: bytes, glossary: Glossary) -> str:
    """사진 한 장으로 의원을 등록하고 결과 메시지를 돌려줍니다."""
    import cv2

    from app.speakers.face import FaceRecognizer

    img = cv2.imdecode(np.frombuffer(image_bytes, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return f"{name}: 이미지를 읽지 못함"

    member = registry.add(name, title)
    glossary.add(name, "의원")

    try:
        recognizer = enroll_photo._recognizer  # type: ignore[attr-defined]
    except AttributeError:
        recognizer = enroll_photo._recognizer = FaceRecognizer()  # type: ignore[attr-defined]

    faces = recognizer.detect(img)
    if not len(faces):
        return f"{name}: 명부에 넣었지만 사진에서 얼굴을 못 찾음"
    row = max(faces, key=lambda r: r[2] * r[3])
    registry.add_face(member.id, recognizer.feature(img, row), recognizer.crop(img, row))
    return f"{name}: 등록 완료 (얼굴 {registry.get(member.id).face_count}장)"


def from_council(profile: dict, title: str, limit: int | None, delay: float) -> None:
    glossary = Glossary()
    base = profile["list_url"].rstrip("/")
    print(f"목록 읽는 중: {base}")
    html = fetch(profile["list_url"]).decode("utf-8", "replace")
    found = re.findall(profile["link_re"], html)

    # 링크 글자가 사람 이름처럼 보여도 메뉴일 수 있어 걸러냅니다.
    not_names = {"누리집", "홈페이지", "바로가기", "의원소개", "전체보기", "더보기", "회의록"}
    seen: set[str] = set()
    people: list[tuple[str, str]] = []
    for path, _mid, name in found:
        if name in seen or name in not_names:
            continue
        seen.add(name)
        people.append((name, base + path))
    if limit:
        people = people[:limit]
    print(f"의원 {len(people)}명을 찾았습니다.")

    for i, (name, url) in enumerate(people, 1):
        try:
            page = fetch(url, referer=profile["list_url"]).decode("utf-8", "replace")
            m = re.search(profile["photo_re"], page)
            if not m:
                registry.add(name, title)
                glossary.add(name, "의원")
                print(f"[{i}/{len(people)}] {name}: 사진을 못 찾아 이름만 등록")
                continue
            photo = fetch(base + m.group(1), referer=url)
            print(f"[{i}/{len(people)}] " + enroll_photo(name, title, photo, glossary))
        except Exception as exc:  # noqa: BLE001
            print(f"[{i}/{len(people)}] {name}: 실패 ({exc})")
        time.sleep(delay)      # 상대 서버에 부담 주지 않도록 쉬어 갑니다


def from_photos(folder: str, title: str) -> None:
    glossary = Glossary()
    files = [p for p in Path(folder).iterdir()
             if p.suffix.lower() in {".jpg", ".jpeg", ".png", ".bmp", ".webp"}]
    print(f"사진 {len(files)}장")
    for p in files:
        name = re.sub(r"[_\-\s]*(의원|의장|부의장)?\s*$", "", p.stem).strip()
        print(" " + enroll_photo(name, title, p.read_bytes(), glossary))


def main() -> int:
    ap = argparse.ArgumentParser(description="의원 명부·얼굴 가져오기")
    ap.add_argument("--council", choices=sorted(COUNCILS), help="미리 등록된 의회")
    ap.add_argument("--list-url", help="의원 목록이 있는 주소 (직접 지정)")
    ap.add_argument("--photos", help="사진 폴더 (파일명이 이름)")
    ap.add_argument("--title", default="의원", help="직함 (기본: 의원)")
    ap.add_argument("--limit", type=int, default=None, help="앞에서 N명만")
    ap.add_argument("--delay", type=float, default=0.6, help="요청 간격(초)")
    args = ap.parse_args()

    paths.ensure_dirs()
    if not paths.FACE_DETECT_MODEL.exists():
        print("얼굴 모델이 없습니다. 먼저: python -m scripts.download_models --aux")
        return 1

    if args.photos:
        from_photos(args.photos, args.title)
    elif args.council or args.list_url:
        profile = dict(COUNCILS.get(args.council or "chungnam"))
        if args.list_url:
            profile["list_url"] = args.list_url
        from_council(profile, args.title, args.limit, args.delay)
    else:
        ap.print_help()
        return 1

    print(f"\n명부에 {len(registry.list())}명이 있습니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
