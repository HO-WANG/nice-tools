"""Real-file coverage for the bounded-memory native comparison engine."""

import codecs
import importlib
import itertools
import random
from pathlib import Path

import pytest

from nice_tools.comparison import CompareOptions, normalize_lines


@pytest.fixture
def engine():
    try:
        return importlib.import_module("nice_tools.local_compare_engine")
    except ModuleNotFoundError:
        pytest.fail("Streaming local comparison engine is missing")


def put(tmp_path, name, content):
    path = tmp_path / name
    path.write_bytes(content)
    return path


@pytest.mark.parametrize(("text", "encoding"), [
    ("", "utf-8"),
    ("hello\tworld\r\n\f 안녕 😀", "utf-8"),
    ("안녕", "cp949"),
    ("뷁쀍똠휗", "cp949"),
    ("우리 회사의 설정 파일입니다.\r\n", "euc-kr"),
    ("こんにちは世界。設定ファイルを比較してください。\r\n名前=山田太郎", "cp932"),
    ("café", "cp1252"),
    ("Café — “Price €12.50”\r\nRésumé déjà vu.\r\n", "cp1252"),
    ("Привет мир. Это текстовый файл настроек.\r\n", "cp1251"),
    ("你好世界，这是一个中文配置文件。请比较文件内容。\n", "gb18030"),
    ("你好世界，這是一個中文設定檔案。請比較檔案內容。\n", "big5"),
    ("name=hello\r\nvalue=world\n", "utf-16-le"),
    ("name=hello\r\nvalue=world\n", "utf-16-be"),
    ("name=hello\r\nvalue=world\n", "utf-32-le"),
    ("name=hello\r\nvalue=world\n", "utf-32-be"),
    ("hello\r\n안녕 😀\n", "utf-8-sig"),
    ("hello\r\n안녕 😀\n", "utf-16"),
    ("hello\r\n안녕 😀\n", "utf-32"),
])
def test_detects_encoding_and_normalizes_complete_text(engine, tmp_path, text, encoding):
    source = put(tmp_path, "source", text.encode(encoding))
    detected = engine.validate_file(source)
    assert source.read_bytes().decode(detected) == text
    destination = tmp_path / "normalized"
    metadata = engine.normalize_file(source, destination, CompareOptions())
    assert destination.read_text() == "".join(line + "\n" for line in normalize_lines(source.read_bytes(), CompareOptions()))
    assert metadata == {"encoding": detected, "rows": len(normalize_lines(source.read_bytes(), CompareOptions()))}


@pytest.mark.parametrize("content", [
    b"\xff", b"a\x00b", b"\xff\xfe\x00", b"\xfe\xff\xd8\x00",
    b"\xff\xfe\x00\xd8", b"\xef\xbb\xbf\xff", b"\xef\xbb", b"\x80\x80",
    random.Random(123).randbytes(1024),
    codecs.BOM_UTF8 + b"A\n" * (6 * 1024 * 1024) + b"\xff",
    "안녕하세요\n".encode("cp949") + b"A\n" * (6 * 1024 * 1024) + b"\x01",
], ids=["ff", "nul", "utf16-truncated", "utf16-be-surrogate", "utf16-le-surrogate", "utf8-bom-invalid", "utf8-bom-truncated", "ambiguous", "random", "late-utf8", "late-control"])
def test_rejects_invalid_complete_file_including_late_tail(engine, tmp_path, content):
    source = put(tmp_path, "bad", content)
    with pytest.raises(ValueError):
        engine.validate_file(source)
    with pytest.raises(ValueError):
        engine.compare_files(source, source, CompareOptions(), tmp_path / "cache")


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_preserves_valid_unicode_controls_and_private_glyphs(engine, tmp_path, encoding):
    text = "item\x1fvalue\x1erecord\x07\x1b[31mred\x1b[0m\u0085next\ue123\U000f0123\u2028line"
    source = put(tmp_path, "unicode", text.encode(encoding))
    destination = tmp_path / "normalized"
    engine.normalize_file(source, destination, CompareOptions())
    assert destination.read_bytes() == (text + "\n").encode()


@pytest.mark.parametrize("sort_lines,ignore_whitespace,ignore_start,ignore_end", itertools.product(
    (False, True), (False, True), (0, 1, 30), (0, 1, 30),
))
def test_options_match_existing_engine(engine, tmp_path, sort_lines, ignore_whitespace, ignore_start, ignore_end):
    content = "\r\nXa bY\rX😀가\u00a0나Y\n\nX\t다\x1f라Y\r\nXa bY\n".encode()
    options = CompareOptions(sort_lines, ignore_whitespace, ignore_start, ignore_end)
    source = put(tmp_path, "source", content)
    destination = tmp_path / "normalized"
    result = engine.normalize_file(source, destination, options)
    expected = normalize_lines(content, options)
    assert destination.read_bytes() == "".join(line + "\n" for line in expected).encode()
    assert result["rows"] == len(expected)


@pytest.mark.parametrize("content", [b"", b"\n", b"\r", b"\r\n", b"a\n\n", b"a\r\rb", b"a"])
def test_empty_and_trailing_newline_records(engine, tmp_path, content):
    source = put(tmp_path, "source", content)
    destination = tmp_path / "normalized"
    result = engine.normalize_file(source, destination, CompareOptions())
    expected = normalize_lines(content, CompareOptions())
    assert destination.read_bytes() == "".join(line + "\n" for line in expected).encode()
    assert result["rows"] == len(expected)


def test_external_sort_retains_duplicates_and_python_unicode_order(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SORT_CHUNK_BYTES", 32)
    monkeypatch.setattr(engine, "MERGE_FAN_IN", 3)
    lines = ["a", "a\x01", "😀", "z", "가", "", "a", "a\x01"] * 40
    source = put(tmp_path, "source", "\n".join(lines).encode())
    destination = tmp_path / "normalized"
    result = engine.normalize_file(source, destination, CompareOptions(sort_lines=True))
    assert destination.read_bytes() == "".join(line + "\n" for line in sorted(lines)).encode()
    assert result["rows"] == len(lines)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["normalized", "source"]


def test_exact_byte_fast_path_validates_and_skips_normalization(engine, tmp_path, monkeypatch):
    left = put(tmp_path, "left", "안녕하세요\n".encode("cp949"))
    right = put(tmp_path, "right", left.read_bytes())
    def unexpected(*args, **kwargs):
        pytest.fail("Byte-identical text must skip normalization and sorting")
    monkeypatch.setattr(engine, "normalize_file", unexpected)
    result = engine.compare_files(left, right, CompareOptions(sort_lines=True), tmp_path / "cache")
    assert result == {"identical": True, "raw_identical": True, "left_encoding": "cp949", "right_encoding": "cp949", "left_normalized": None, "right_normalized": None}


def test_mixed_encodings_keep_normalized_cache_for_diff(engine, tmp_path):
    text = "내부망 파일 비교\r\n설정 값 확인\n"
    left = put(tmp_path, "left", text.encode("cp949"))
    right = put(tmp_path, "right", text.encode("utf-16"))
    result = engine.compare_files(left, right, CompareOptions(), tmp_path / "cache")
    assert result["identical"] is True and result["raw_identical"] is False
    assert result["left_encoding"] == "cp949"
    assert result["right_encoding"] == "utf-16"
    assert Path(result["left_normalized"]).is_absolute()
    assert Path(result["left_normalized"]).read_bytes() == Path(result["right_normalized"]).read_bytes()


def test_late_differences_and_large_single_line_have_no_cap(engine, tmp_path):
    prefix = b"A" * (11 * 1024 * 1024)
    left = put(tmp_path, "left", prefix + "끝:원본".encode("cp949"))
    right = put(tmp_path, "right", prefix + "끝:수정".encode("cp949"))
    assert engine.files_equal(left, right) is False
    result = engine.compare_files(left, right, CompareOptions(ignore_start=1), tmp_path / "cache")
    assert result["identical"] is False
    assert Path(result["left_normalized"]).read_bytes().endswith("끝:원본\n".encode())


def test_cancelled_operations_clean_partial_cache_and_sort_runs(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SORT_CHUNK_BYTES", 16)
    source = put(tmp_path, "source", b"hello world\n" * 1000)
    cache = tmp_path / "cache"
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 30
    with pytest.raises(ValueError, match="취소|cancel"):
        engine.normalize_file(source, tmp_path / "normalized", CompareOptions(sort_lines=True), encoding="utf-8", cancelled=cancelled)
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source"]
    with pytest.raises(ValueError, match="취소|cancel"):
        engine.compare_files(source, source, CompareOptions(), cache, cancelled=lambda: True)
    assert not cache.exists() or list(cache.iterdir()) == []


def test_invalid_explicit_encoding_cleans_partial_destination(engine, tmp_path):
    source = put(tmp_path, "source", b"hello\nworld\n\xff")
    destination = tmp_path / "normalized"
    with pytest.raises(ValueError):
        engine.normalize_file(source, destination, CompareOptions(), encoding="utf-8")
    assert sorted(path.name for path in tmp_path.iterdir()) == ["source"]


def test_source_cannot_be_its_normalized_destination(engine, tmp_path):
    source = put(tmp_path, "source", b"source\n")
    with pytest.raises(ValueError):
        engine.normalize_file(source, source, CompareOptions())
    assert source.read_bytes() == b"source\n"


def test_public_argument_types_are_validated(engine, tmp_path):
    source = put(tmp_path, "source", b"hello")
    with pytest.raises(ValueError):
        engine.validate_file(str(source))
    with pytest.raises(ValueError):
        engine.normalize_file(source, tmp_path / "out", {})
    with pytest.raises(ValueError):
        engine.compare_files(source, source, {}, tmp_path / "cache")


@pytest.mark.parametrize(("text", "encoding"), [
    ("name=你好世界，这是一个中文配置文件。请比较文件内容。\nother=configuration\n", "gb18030"),
    ("name=你好世界，這是一個中文設定檔案。請比較檔案內容。\nother=configuration\n", "big5"),
    ("こんにちは", "shift_jis"),
    ("Überprüfung der Dateien ist erfolgreich.", "cp1252"),
    ("São Paulo", "cp1252"),
])
def test_legacy_detection_matches_existing_default_preferences(engine, tmp_path, text, encoding):
    source = put(tmp_path, "source", text.encode(encoding))
    destination = tmp_path / "normalized"
    engine.normalize_file(source, destination, CompareOptions())
    expected = normalize_lines(source.read_bytes(), CompareOptions())
    assert destination.read_bytes() == "".join(line + "\n" for line in expected).encode()


def test_right_validation_failure_cleans_both_comparison_stages(engine, tmp_path):
    left = put(tmp_path, "left", b"valid text\n")
    right = put(tmp_path, "right", codecs.BOM_UTF8 + b"\xff")
    cache = tmp_path / "cache"
    with pytest.raises(ValueError):
        engine.compare_files(left, right, CompareOptions(), cache)
    assert list(cache.iterdir()) == []
    assert left.read_bytes() == b"valid text\n"
    assert right.read_bytes() == codecs.BOM_UTF8 + b"\xff"


def test_cancellation_during_merge_preserves_existing_destination(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SORT_CHUNK_BYTES", 16)
    monkeypatch.setattr(engine, "MERGE_FAN_IN", 3)
    source = put(tmp_path, "source", b"b\na\n" * 1000)
    destination = put(tmp_path, "destination", b"previous complete result\n")
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 6100
    with pytest.raises(ValueError, match="취소|cancel"):
        engine.normalize_file(source, destination, CompareOptions(sort_lines=True), encoding="utf-8", cancelled=cancelled)
    assert destination.read_bytes() == b"previous complete result\n"
    assert sorted(path.name for path in tmp_path.iterdir()) == ["destination", "source"]


def test_explicit_encoding_validation_precedes_ignored_characters(engine, tmp_path):
    source = put(tmp_path, "source", b"\x00hello\n")
    destination = tmp_path / "normalized"
    with pytest.raises(ValueError):
        engine.normalize_file(source, destination, CompareOptions(ignore_start=1), encoding="utf-8")
    assert not destination.exists()


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 7, 128 * 1024])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16", "cp949"])
def test_chunk_boundaries_preserve_crlf_and_multibyte_characters(engine, tmp_path, monkeypatch, chunk_size, encoding):
    monkeypatch.setattr(engine, "IO_CHUNK_BYTES", chunk_size)
    text = "가\r\n나\r다\n\r\n\r\r마\n바"
    content = text.encode(encoding)
    source = put(tmp_path, "source", content)
    destination = tmp_path / "normalized"
    result = engine.normalize_file(source, destination, CompareOptions(ignore_start=1), encoding=encoding)
    expected = normalize_lines(content, CompareOptions(ignore_start=1))
    assert destination.read_bytes() == "".join(line + "\n" for line in expected).encode()
    assert result["rows"] == len(expected)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_unicode_normalization_validates_in_one_streaming_pass(engine, tmp_path, monkeypatch, encoding):
    content = "한글 text\ue123\u0085\nsecond\n".encode(encoding)
    source = put(tmp_path, "source", content)
    destination = tmp_path / "normalized"
    def unexpected(*args, **kwargs):
        pytest.fail("Identified Unicode must be normalized and validated in the same pass")
    monkeypatch.setattr(engine, "validate_file", unexpected)
    metadata = engine.normalize_file(source, destination, CompareOptions())
    assert destination.read_bytes() == "한글 text\ue123\u0085\nsecond\n".encode()
    assert metadata == {"encoding": encoding, "rows": 2}


def test_legacy_fallback_discards_runs_from_failed_utf8_attempt(engine, tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "SORT_CHUNK_BYTES", 64 * 1024)
    content = b"z\n" * (128 * 1024) + "끝:원본\n".encode("cp949")
    source = put(tmp_path, "source", content)
    destination = tmp_path / "normalized"
    result = engine.normalize_file(source, destination, CompareOptions(sort_lines=True))
    expected = normalize_lines(content, CompareOptions(sort_lines=True))
    assert result == {"encoding": "cp949", "rows": len(expected)}
    assert destination.read_bytes() == "".join(line + "\n" for line in expected).encode()
    assert sorted(path.name for path in tmp_path.iterdir()) == ["normalized", "source"]
