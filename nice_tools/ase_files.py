"""Streaming validation of FreeTDS character-mode BCP files.

Character BCP has no CSV quoting: each column has its own terminator. The
first columns use the field terminator and only the last uses the row
terminator. Empty bytes represent NULL; one NUL byte represents empty text.
"""

import codecs
import hashlib
import math
import os
import re
import struct
from decimal import Decimal, InvalidOperation
from pathlib import Path


_READ_CHUNK_SIZE = 64 * 1024
_PREVIEW_ROWS = 20
_PREVIEW_CHARS = 400
_TRUNCATED = "… [truncated]"
_ENCODINGS = {"utf-8", "cp949", "euc-kr"}
_INTEGER_RANGES = {
    "tinyint": (0, 255), "smallint": (-32768, 32767),
    "int": (-2147483648, 2147483647),
    "bigint": (-9223372036854775808, 9223372036854775807),
    "unsigned smallint": (0, 65535), "unsigned int": (0, 4294967295),
    "unsigned bigint": (0, 18446744073709551615), "bit": (0, 1),
}
_TYPE_IDS = {
    47: "char", 39: "varchar",
    135: "unichar", 155: "univarchar", 35: "text", 174: "unitext",
    48: "tinyint", 52: "smallint", 56: "int", 191: "bigint", 50: "bit",
    65: "unsigned smallint", 66: "unsigned int", 67: "unsigned bigint",
    106: "decimal", 108: "numeric", 60: "money", 122: "smallmoney",
    62: "float", 59: "real", 61: "datetime", 58: "smalldatetime",
    49: "date", 51: "time", 187: "bigdatetime", 188: "bigtime",
    45: "binary", 37: "varbinary", 34: "image", 80: "timestamp",
}
_TEXT_TYPES = {"char", "varchar", "nchar", "nvarchar", "unichar", "univarchar", "text", "unitext"}
_CHAR_TYPES = _TEXT_TYPES - {"text", "unitext"}
_UNICODE_CHAR_TYPES = {"unichar", "univarchar"}
_DATE_TYPES = {"datetime", "smalldatetime", "date", "time", "bigdatetime", "bigtime"}
_NUMBER_TYPES = set(_INTEGER_RANGES) | {"decimal", "numeric", "money", "smallmoney", "float", "real", "double precision"}
_SUPPORTED_TYPES = _TEXT_TYPES | _NUMBER_TYPES | _DATE_TYPES


def _terminator(value: object, encoding: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError("구분자는 문자열이어야 합니다.")
    escapes = {"t": "\t", "n": "\n", "r": "\r", "\\": "\\"}
    decoded = []
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\":
            index += 1
            if index == len(value) or value[index] not in escapes:
                raise ValueError("구분자에는 \\t, \\n, \\r, \\\\ 이스케이프만 사용할 수 있습니다.")
            char = escapes[value[index]]
        decoded.append(char)
        index += 1
    try:
        return "".join(decoded).encode(encoding, errors="strict")
    except UnicodeEncodeError:
        raise ValueError("구분자를 선택한 인코딩으로 표현할 수 없습니다.") from None


def parse_options(payload: dict) -> dict:
    """Return canonical JSON-safe options; canonical options are accepted too."""
    if not isinstance(payload, dict):
        raise ValueError("BCP 설정은 객체여야 합니다.")
    direction = payload.get("direction", "in")
    if not isinstance(direction, str) or direction.lower() not in {"in", "out"}:
        raise ValueError("BCP 방향은 in 또는 out이어야 합니다.")
    encoding = payload.get("encoding", "utf-8")
    if not isinstance(encoding, str) or encoding not in _ENCODINGS:
        raise ValueError("인코딩은 utf-8, cp949 또는 euc-kr이어야 합니다.")
    terminators = []
    for name, default in (("field", r"\t"), ("row", r"\n")):
        if f"{name}_hex" in payload:
            encoded = payload[f"{name}_hex"]
            if not isinstance(encoded, str) or re.fullmatch(r"(?:[0-9a-fA-F]{2}){1,32}", encoded) is None:
                raise ValueError("구분자 바이트 설정이 올바르지 않습니다.")
            value = bytes.fromhex(encoded)
            if f"{name}_terminator" in payload and value != _terminator(payload[f"{name}_terminator"], encoding):
                raise ValueError("구분자 설정이 서로 다릅니다.")
        else:
            value = _terminator(payload.get(f"{name}_terminator", default), encoding)
        if not value or len(value) > 32 or b"\0" in value:
            raise ValueError("구분자는 NUL을 제외한 1~32바이트여야 합니다.")
        terminators.append(value)
    if terminators[0] == terminators[1]:
        raise ValueError("필드 구분자와 행 구분자는 달라야 합니다.")
    batch_size = payload.get("batch_size", 1000)
    if type(batch_size) is not int or not 1 <= batch_size <= 2147483647:
        raise ValueError("배치 크기는 1~2147483647의 정수여야 합니다.")
    keep_identity = payload.get("keep_identity", False)
    if type(keep_identity) is not bool:
        raise ValueError("IDENTITY 유지 설정은 참/거짓 값이어야 합니다.")
    return {
        "direction": direction.lower(), "encoding": encoding,
        "field_hex": terminators[0].hex(), "row_hex": terminators[1].hex(),
        "batch_size": batch_size, "keep_identity": keep_identity,
    }


def _column_type(column: dict) -> str:
    name = str(column.get("type_name", "")).lower().strip()
    if "encrypt" in name:
        return "unsupported"
    # Built-in IDs also describe alias datatypes returned by the ASE catalog.
    known = _TYPE_IDS.get(column.get("type_id"))
    if known in {"binary", "varbinary", "image", "timestamp"}:
        return known
    return name if name in _SUPPORTED_TYPES else known or "unsupported"


class _NumericValue:
    """Bounded numeric token, preserving decimal exactness without rounding.

    Leading and trailing zeroes may be arbitrarily long. ASE decimals have
    at most 38 significant digits. IEEE double conversion boundaries need
    fewer than 800 decimal digits; the extra sticky digit preserves which
    side of a boundary a longer input occupies.
    """

    def __init__(self, integer: bool, fail):
        self.integer = integer
        self.fail = fail
        self.sign = 0
        self.dot = False
        self.in_exponent = False
        self.exponent_sign = 0
        self.exponent_digits = 0
        self.exponent = 0
        self.digits = 0
        self.fractional = 0
        self.first_nonzero = 0
        self.last_nonzero = 0
        self.coefficient = []

    def feed(self, text: str):
        for char in text:
            if self.in_exponent:
                if char in "+-" and not self.exponent_sign and not self.exponent_digits:
                    self.exponent_sign = -1 if char == "-" else 1
                elif "0" <= char <= "9":
                    self.exponent_digits += 1
                    # Anything larger cannot be cancelled by any real file's
                    # byte count; saturating keeps long exponent tokens small.
                    self.exponent = min(10**18, self.exponent * 10 + ord(char) - 48)
                else:
                    self.fail("숫자 형식이 올바르지 않습니다.")
            elif "0" <= char <= "9":
                self.digits += 1
                if self.dot:
                    self.fractional += 1
                if char != "0":
                    if not self.first_nonzero:
                        self.first_nonzero = self.digits
                    self.last_nonzero = self.digits
                if self.first_nonzero and len(self.coefficient) < 800:
                    self.coefficient.append(char)
            elif char in "+-" and not self.sign and not self.digits and not self.dot:
                self.sign = -1 if char == "-" else 1
            elif char == "." and not self.integer and not self.dot:
                self.dot = True
            elif char in "eE" and not self.integer and self.digits:
                self.in_exponent = True
            else:
                self.fail("정수 형식이 올바르지 않습니다." if self.integer else "숫자 형식이 올바르지 않습니다.")

    def finish(self, kind: str) -> Decimal | int:
        if not self.digits or (self.in_exponent and not self.exponent_digits):
            self.fail("숫자 형식이 올바르지 않습니다.")
        if not self.first_nonzero:
            return 0 if self.integer else Decimal(0)
        sign = self.sign or 1
        significant = self.last_nonzero - self.first_nonzero + 1
        trailing = self.digits - self.last_nonzero
        if self.integer:
            if significant + trailing > 20:
                self.fail("정수가 열의 범위를 초과합니다.")
            return sign * int("".join(self.coefficient[:significant])) * 10**trailing
        if kind in {"numeric", "decimal", "money", "smallmoney"} and significant > 38:
            self.fail("숫자가 열의 정밀도 범위를 초과합니다.")
        exponent = (self.exponent_sign or 1) * self.exponent - self.fractional + trailing
        digits = self.coefficient[:significant]
        if significant > len(digits):
            exponent += significant - len(digits) - 1
            digits = digits + ["1"]
        try:
            return Decimal((int(sign < 0), tuple(ord(d) - 48 for d in digits), exponent))
        except (InvalidOperation, ValueError, OverflowError):
            self.fail("숫자가 열의 범위를 초과합니다.")


def _exact_decimal(number: Decimal, precision: int, scale: int, fail) -> None:
    if not 1 <= precision <= 38 or not 0 <= scale <= precision:
        fail("열의 정밀도/소수 자릿수 정보가 올바르지 않습니다.")
    if number.is_zero():
        return
    _, digits, exponent = number.as_tuple()
    # Ignore zeroes beyond the declared scale, but never round nonzero digits.
    significant = len(digits)
    while significant and digits[significant - 1] == 0:
        significant -= 1
        exponent += 1
    if exponent < -scale:
        fail("소수 자릿수를 초과하여 값이 잘릴 수 있습니다.")
    integer_digits = max(0, significant + exponent)
    if integer_digits > precision - scale:
        fail("숫자가 열의 정밀도 범위를 초과합니다.")


def _validate_number(value: _NumericValue, kind: str, column: dict, fail) -> None:
    number = value.finish(kind)
    if kind in _INTEGER_RANGES:
        low, high = _INTEGER_RANGES[kind]
        if not low <= number <= high:
            fail("정수가 열의 범위를 초과합니다.")
        return
    if kind in {"decimal", "numeric"}:
        _exact_decimal(number, int(column.get("precision", 0)), int(column.get("scale", 0)), fail)
    elif kind in {"money", "smallmoney"}:
        _exact_decimal(number, 19 if kind == "money" else 10, 4, fail)
        low, high = (("-922337203685477.5808", "922337203685477.5807") if kind == "money"
                     else ("-214748.3648", "214748.3647"))
        if not Decimal(low) <= number <= Decimal(high):
            fail("금액이 열의 범위를 초과합니다.")
    else:
        try:
            floating = float(number)
        except (OverflowError, ValueError):
            fail("부동소수점 숫자가 열의 범위를 초과합니다.")
        if not math.isfinite(floating) or (floating == 0 and not number.is_zero()):
            fail("부동소수점 숫자가 열의 범위를 초과합니다.")
        if kind == "real" or (kind == "float" and column.get("length") == 4):
            real_max = Decimal.from_float(struct.unpack("<f", b"\xff\xff\x7f\x7f")[0])
            if abs(number) > real_max:
                fail("부동소수점 숫자가 열의 범위를 초과합니다.")
            try:
                real_value = struct.unpack("<f", struct.pack("<f", floating))[0]
            except (OverflowError, struct.error):
                fail("부동소수점 숫자가 열의 범위를 초과합니다.")
            if real_value == 0 and not number.is_zero():
                fail("부동소수점 숫자가 열의 범위를 초과합니다.")


class _Field:
    def __init__(self, column: dict, options: dict, row: int, index: int, warnings: set[str]):
        self.column = column
        self.kind = _column_type(column)
        self.options = options
        self.row = row
        self.index = index
        self.warnings = warnings
        self.decoder = codecs.getincrementaldecoder(options["encoding"])(errors="strict")
        self.raw_size = 0
        self.utf16_size = 0
        self.nuls = 0
        self.preview = ""
        self.char_count = 0
        self.last_char = ""
        self.numeric = _NumericValue(self.kind in _INTEGER_RANGES, self.fail) if self.kind in _NUMBER_TYPES and self.checked else None

    @property
    def checked(self):
        return self.options["direction"] == "in" and not (
            self.column.get("identity") and not self.options["keep_identity"]
        )

    def fail(self, reason: str):
        raise ValueError(f"{self.row}행 {self.index + 1}열: {reason}")

    def _text(self, value: str):
        self.char_count += len(value)
        if self.kind in _UNICODE_CHAR_TYPES:
            self.utf16_size += len(value.encode("utf-16-le"))
        if len(self.preview) < _PREVIEW_CHARS:
            self.preview += value[:_PREVIEW_CHARS - len(self.preview)]
        if value:
            self.last_char = value[-1]
        if self.numeric is not None and value != "\0":
            self.numeric.feed(value)

    def feed(self, data: bytes):
        self.raw_size += len(data)
        self.nuls += data.count(b"\0")
        try:
            self._text(self.decoder.decode(data, final=False))
        except UnicodeDecodeError:
            self.fail("선택한 인코딩으로 읽을 수 없습니다.")

    def finish(self):
        try:
            self._text(self.decoder.decode(b"", final=True))
        except UnicodeDecodeError:
            self.fail("선택한 인코딩으로 읽을 수 없습니다.")
        if self.raw_size == 0:
            if self.checked and not self.column.get("nullable", False):
                self.fail("NULL을 허용하지 않는 열입니다.")
            return None
        empty_string = self.raw_size == 1 and self.nuls == 1
        if self.nuls and not empty_string:
            self.fail("필드 내부 NUL은 허용하지 않습니다.")
        if empty_string:
            if self.checked and self.kind not in _TEXT_TYPES:
                self.fail("빈 문자열은 문자 열에서만 사용할 수 있습니다.")
            return ""
        if self.checked:
            if self.kind in _CHAR_TYPES:
                length = self.column.get("length")
                if type(length) is not int or length <= 0:
                    self.fail("열의 길이 정보가 올바르지 않습니다.")
                byte_length = self.utf16_size if self.kind in _UNICODE_CHAR_TYPES else self.raw_size
                if byte_length > length:
                    self.fail("문자 값의 바이트 길이가 열의 길이를 초과합니다.")
            if self.kind in _NUMBER_TYPES:
                _validate_number(self.numeric, self.kind, self.column, self.fail)
        if self.kind in _TEXT_TYPES and self.last_char == " ":
            self.warnings.add("FreeTDS는 문자 값의 끝 공백을 제거할 수 있습니다. 원본 공백을 확인하세요.")
        return self.preview + (_TRUNCATED if self.char_count > _PREVIEW_CHARS else "")


def inspect_file(path: Path, columns: list[dict], options: dict, emit=None) -> dict:
    """Validate every byte and return bounded previews plus a reviewed fingerprint.

    Progress callbacks receive only ``{rows, bytes}``; no source values are
    emitted. Date/time conversion and server constraints still need ASE.
    """
    options = parse_options(options)
    if not isinstance(columns, list) or not columns or any(not isinstance(c, dict) for c in columns):
        raise ValueError("테이블의 전체 열 정보가 필요합니다.")
    warnings = set()
    for index, column in enumerate(columns):
        kind = _column_type(column)
        if options["direction"] == "in" and kind not in _SUPPORTED_TYPES:
            raise ValueError(f"{index + 1}열: 이 데이터 형식은 문자 BCP 입력을 지원하지 않습니다.")
        if column.get("identity") and not options["keep_identity"]:
            warnings.add("IDENTITY 열은 파일에 포함해야 하며 입력 시 해당 값은 무시됩니다.")
        if kind in _DATE_TYPES and options["direction"] == "in":
            warnings.add("날짜/시간 값의 형식과 범위는 ASE 서버에서 최종 검증합니다.")
        if kind in _CHAR_TYPES - _UNICODE_CHAR_TYPES and options["direction"] == "in":
            warnings.add("CHAR/VARCHAR/NCHAR/NVARCHAR 길이는 파일 인코딩의 바이트 수로 보수적으로 검사합니다. 서버 문자셋이 다르면 유효한 값도 거부될 수 있으며, 서버 변환과 표현 가능 여부는 ASE에서 최종 검증합니다.")
    field_term = bytes.fromhex(options["field_hex"])
    row_term = bytes.fromhex(options["row_hex"])
    rows = 0
    byte_count = 0
    sample = []
    row_preview = []
    column_index = 0
    field = _Field(columns[0], options, 1, 0, warnings)
    pending = bytearray()
    digest = hashlib.sha256()
    last_emitted_bytes = 0
    try:
        with Path(path).open("rb") as source:
            before = os.fstat(source.fileno())
            while chunk := source.read(_READ_CHUNK_SIZE):
                byte_count += len(chunk)
                digest.update(chunk)
                pending.extend(chunk)
                start = 0
                while True:
                    term = row_term if column_index == len(columns) - 1 else field_term
                    stop = pending.find(term, start)
                    if stop < 0:
                        safe_stop = max(start, len(pending) - len(term) + 1)
                        field.feed(bytes(pending[start:safe_stop]))
                        del pending[:safe_stop]
                        break
                    field.feed(bytes(pending[start:stop]))
                    display = field.finish()
                    if rows < _PREVIEW_ROWS:
                        row_preview.append(display)
                    start = stop + len(term)
                    column_index += 1
                    if column_index == len(columns):
                        rows += 1
                        if len(sample) < _PREVIEW_ROWS:
                            sample.append(row_preview)
                        row_preview = []
                        column_index = 0
                    field = _Field(columns[column_index], options, rows + 1, column_index, warnings)
                    if start == len(pending):
                        pending.clear()
                        break
                if emit and byte_count - last_emitted_bytes >= 1024 * 1024:
                    emit({"rows": rows, "bytes": byte_count})
                    last_emitted_bytes = byte_count
            if pending:
                field.feed(bytes(pending))
            if column_index or field.raw_size:
                field.fail("마지막 필드/행 구분자가 없거나 열이 부족합니다.")
            after = os.fstat(source.fileno())
        current = Path(path).stat()
    except OSError:
        raise ValueError("파일을 읽을 수 없습니다.") from None
    if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            or byte_count != after.st_size):
        raise ValueError("검사 도중 파일이 변경되었습니다. 파일을 다시 검사하세요.")
    if emit:
        emit({"rows": rows, "bytes": byte_count})
    return {
        "rows": rows, "bytes": byte_count, "sample": sample, "warnings": sorted(warnings),
        "fingerprint": {"size": byte_count, "mtime_ns": after.st_mtime_ns, "sha256": digest.hexdigest()},
    }
