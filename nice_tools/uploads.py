"""폼 데이터 검증과 업로드 파일 읽기."""

import json
import re

from starlette.datastructures import FormData, UploadFile
from starlette.exceptions import HTTPException

from .comparison import CompareOptions
from .limits import MAX_FILE_BYTES, MAX_FILES


def single_field(form: FormData, name: str, default: str | None = None) -> str:
    values = form.getlist(name)
    if not values and default is not None:
        return default
    if len(values) != 1 or not isinstance(values[0], str):
        raise HTTPException(400, f"{name} 값이 없거나 중복되었습니다.")
    return values[0]


def parse_boolean(form: FormData, name: str) -> bool:
    value = single_field(form, name, "false")
    if value not in ("true", "false"):
        raise HTTPException(400, "비교 체크박스 값이 잘못되었습니다.")
    return value == "true"


def parse_count(form: FormData, name: str) -> int:
    value = single_field(form, name, "0")
    if not re.fullmatch(r"[0-9]+", value):
        raise HTTPException(400, "무시할 글자 수는 0 이상의 정수여야 합니다.")
    try:
        return int(value)
    except ValueError:
        raise HTTPException(400, "무시할 글자 수가 너무 큽니다.") from None


def parse_paths(form: FormData, name: str, count: int) -> list[str]:
    try:
        paths = json.loads(single_field(form, name))
    except (json.JSONDecodeError, RecursionError):
        raise HTTPException(400, "폴더의 파일 경로 목록이 잘못되었습니다.") from None
    if not isinstance(paths, list) or len(paths) != count:
        raise HTTPException(400, "파일 목록과 상대 경로의 개수가 일치하지 않습니다.")
    seen = set()
    for path in paths:
        if (
            not isinstance(path, str) or not path or "\\" in path
            or re.match(r"^[A-Za-z]:", path)
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in path)
            or any(part in ("", ".", "..") for part in path.split("/"))
        ):
            raise HTTPException(400, "유효한 폴더 내 상대 경로만 사용할 수 있습니다.")
        if path in seen:
            raise HTTPException(400, f"같은 상대 경로가 중복되었습니다: {path}")
        seen.add(path)
    return paths


async def read_uploads(form: FormData) -> tuple[dict[str, bytes], dict[str, bytes], CompareOptions]:
    expected = {"left_files", "right_files", "left_paths", "right_paths", "sort_lines", "ignore_whitespace", "ignore_start", "ignore_end"}
    if any(key not in expected for key in form):
        raise HTTPException(400, "알 수 없는 업로드 항목이 있습니다.")
    left = form.getlist("left_files")
    right = form.getlist("right_files")
    if not left or not right:
        raise HTTPException(400, "파일이 있는 폴더 두 개를 선택해 주세요.")
    if len(left) + len(right) > MAX_FILES:
        raise HTTPException(400, "파일은 두 폴더 합계 1,000개까지 비교할 수 있습니다.")
    if any(not isinstance(item, UploadFile) for item in left + right):
        raise HTTPException(400, "업로드 파일 정보가 잘못되었습니다.")
    left_paths = parse_paths(form, "left_paths", len(left))
    right_paths = parse_paths(form, "right_paths", len(right))
    options = CompareOptions(
        sort_lines=parse_boolean(form, "sort_lines"),
        ignore_whitespace=parse_boolean(form, "ignore_whitespace"),
        ignore_start=parse_count(form, "ignore_start"),
        ignore_end=parse_count(form, "ignore_end"),
    )
    collections = []
    for paths, files in ((left_paths, left), (right_paths, right)):
        contents = {}
        for path, file in zip(paths, files, strict=True):
            if file.size is not None and file.size > MAX_FILE_BYTES:
                raise HTTPException(413, f"개별 파일은 10 MiB 이내여야 합니다: {path}")
            content = await file.read(MAX_FILE_BYTES + 1)
            if len(content) > MAX_FILE_BYTES:
                raise HTTPException(413, f"개별 파일은 10 MiB 이내여야 합니다: {path}")
            contents[path] = content
        collections.append(contents)
    return collections[0], collections[1], options
