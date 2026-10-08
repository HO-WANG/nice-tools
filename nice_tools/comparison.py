"""HTTP와 독립적인 텍스트 파일 비교 규칙."""

from dataclasses import dataclass


@dataclass(frozen=True)
class CompareOptions:
    sort_lines: bool = False
    ignore_whitespace: bool = False
    ignore_start: int = 0
    ignore_end: int = 0

    def __post_init__(self):
        for value in (self.ignore_start, self.ignore_end):
            if type(value) is not int or value < 0:
                raise ValueError("무시할 글자 수는 0 이상의 정수여야 합니다.")
        if type(self.sort_lines) is not bool or type(self.ignore_whitespace) is not bool:
            raise ValueError("줄 정렬과 공백 무시는 참/거짓 값이어야 합니다.")


def normalize_lines(content: bytes, options: CompareOptions) -> list[str]:
    text = content.decode("utf-8-sig")
    if "\0" in text:
        raise ValueError("NUL 문자를 포함하여 텍스트 파일로 읽을 수 없습니다.")
    if not text:
        return []
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines[-1] == "":
        lines.pop()
    normalized = []
    for line in lines:
        stop = max(0, len(line) - options.ignore_end)
        line = line[options.ignore_start:stop]
        if options.ignore_whitespace:
            line = "".join(char for char in line if not char.isspace())
        normalized.append(line)
    return sorted(normalized) if options.sort_lines else normalized


def compare_directories(
    left: dict[str, bytes], right: dict[str, bytes], options: CompareOptions
) -> dict:
    summary = {key: 0 for key in ("total", "same", "different", "left_only", "right_only", "error")}
    results = []
    for path in sorted(left.keys() | right.keys()):
        entry = {"path": path}
        if path not in right:
            entry["status"] = "left_only"
        elif path not in left:
            entry["status"] = "right_only"
        else:
            normalized = []
            errors = []
            for label, content in (("왼쪽", left[path]), ("오른쪽", right[path])):
                try:
                    normalized.append(normalize_lines(content, options))
                except UnicodeDecodeError:
                    errors.append(f"{label}: UTF-8로 읽을 수 없습니다.")
                except ValueError as exc:
                    errors.append(f"{label}: {exc}")
            if errors:
                entry.update(status="error", message=" / ".join(errors))
            else:
                entry["status"] = "same" if normalized[0] == normalized[1] else "different"
        summary[entry["status"]] += 1
        summary["total"] += 1
        results.append(entry)
    return {"summary": summary, "results": results}
