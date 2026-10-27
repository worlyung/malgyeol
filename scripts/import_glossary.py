"""용어사전 CSV를 명령줄에서 가져오기.

    python -m scripts.import_glossary "C:/경로/충남도의회_STT_오인식_전체사전.csv"
    python -m scripts.import_glossary 사전.csv --replace      # 기존 사전을 지우고 새로 넣기

CSV 머리글은 "오인식표현, 표준용어, 분류, 세부분류" 형식을 기본으로 읽습니다.
(from/to, wrong/correct 같은 영문 머리글도 인식합니다)
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.glossary import Glossary  # noqa: E402
from app.glossary_import import import_csv  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="용어사전 CSV 가져오기")
    ap.add_argument("csv", help="CSV 파일 경로")
    ap.add_argument("--replace", action="store_true", help="기존 용어집을 지우고 새로 넣기")
    args = ap.parse_args()

    path = Path(args.csv)
    if not path.exists():
        print(f"파일이 없습니다: {path}")
        return 1

    glossary = Glossary()
    before_terms, before_rules = len(glossary.terms), len(glossary.replacements)
    result = import_csv(glossary, path.read_bytes(), replace=args.replace)

    print(f"용어    {before_terms:>5} -> {result['total_terms']:>5}  (+{result['added_terms']})")
    print(f"바꾸기  {before_rules:>5} -> {result['total_rules']:>5}  (+{result['added_rules']})")
    if result["skipped"]:
        print(f"건너뜀  {result['skipped']}줄 (표준용어 칸이 비어 있음)")
    print("서버가 켜져 있다면 용어집 탭에서 새로고침하면 바로 반영됩니다.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
