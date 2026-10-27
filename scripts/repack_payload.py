"""기존 ZIP의 압축 데이터를 보존하면서 지정한 소스 파일만 갱신합니다."""
import copy
import shutil
import sys
import zipfile
from pathlib import Path


def repack(archive: Path, root: Path, names: list[str]) -> None:
    temp = archive.with_suffix('.repack.zip')
    replacements = set(names)
    with zipfile.ZipFile(archive) as source, zipfile.ZipFile(temp, 'w', compression=zipfile.ZIP_LZMA) as target:
        entries = sorted(source.infolist(), key=lambda item: item.header_offset)
        if not replacements.issubset({entry.filename for entry in entries}):
            raise ValueError('교체 파일이 원본 ZIP에 없습니다')
        for index, info in enumerate(entries):
            if info.filename in replacements:
                target.writestr(info.filename, (root / info.filename).read_bytes())
                continue
            end = entries[index + 1].header_offset if index + 1 < len(entries) else source.start_dir
            source.fp.seek(info.header_offset)
            cloned = copy.copy(info)
            cloned.header_offset = target.fp.tell()
            left = end - info.header_offset
            while left:
                data = source.fp.read(min(left, 1024 * 1024))
                if not data:
                    raise EOFError(info.filename)
                target.fp.write(data)
                left -= len(data)
            target.filelist.append(cloned)
            target.NameToInfo[cloned.filename] = cloned
            target.start_dir = target.fp.tell()
            target._didModify = True
        target.comment = source.comment
    # 교체한 내용과 중앙 디렉터리를 확인합니다. 기존 항목의 압축 바이트는 그대로입니다.
    with zipfile.ZipFile(temp) as result:
        assert len(result.infolist()) == len(entries)
        for name in names:
            assert result.read(name) == (root / name).read_bytes()
    temp.replace(archive)


if __name__ == '__main__':
    repack(Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3:])
