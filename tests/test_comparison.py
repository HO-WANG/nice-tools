import importlib

import pytest


@pytest.fixture
def core():
    try:
        return importlib.import_module("nice_tools.comparison")
    except ModuleNotFoundError:
        pytest.fail("디렉토리 비교 모듈이 아직 구현되지 않았습니다.")


def test_summary_and_path_pairing(core):
    result = core.compare_directories(
        {"sub/same.txt": b"one", "changed.txt": b"old", "left.txt": b"L"},
        {"sub/same.txt": b"one", "changed.txt": b"new", "right.txt": b"R"},
        core.CompareOptions(),
    )
    assert result["summary"] == {
        "total": 4, "same": 1, "different": 1, "left_only": 1, "right_only": 1, "error": 0,
    }
    assert [(r["path"], r["status"]) for r in result["results"]] == [
        ("changed.txt", "different"), ("left.txt", "left_only"),
        ("right.txt", "right_only"), ("sub/same.txt", "same"),
    ]


@pytest.mark.parametrize(("content", "options", "expected"), [
    (b"", {}, []),
    (b"\xef\xbb\xbfhello\r\nworld\r", {}, ["hello", "world"]),
    (b"a\n\nb\n", {}, ["a", "", "b"]),
    (b"\n", {}, [""]),
    (b"b\na\nb", {"sort_lines": True}, ["a", "b", "b"]),
    (" 가\t나\u00a0다 ".encode(), {"ignore_whitespace": True}, ["가나다"]),
    (b"a\n\nb", {"ignore_whitespace": True}, ["a", "", "b"]),
    (b"xxabcYY", {"ignore_start": 2, "ignore_end": 2}, ["abc"]),
    (b"xxabc", {"ignore_start": 2}, ["abc"]),
    (b"abcYY", {"ignore_end": 2}, ["abc"]),
    ("😀가나다".encode(), {"ignore_start": 1, "ignore_end": 1}, ["가나"]),
    ("😀가나다".encode(), {"ignore_start": 3, "ignore_end": 3}, [""]),
    (b"abc", {"ignore_start": 999}, [""]),
    (b"Xa bY\nX b aY", {"ignore_start": 1, "ignore_end": 1, "ignore_whitespace": True, "sort_lines": True}, ["ab", "ba"]),
])
def test_normalization(core, content, options, expected):
    assert core.normalize_lines(content, core.CompareOptions(**options)) == expected


@pytest.mark.parametrize(("left", "right", "options", "status"), [
    (b"a\nb", b"b\na", {}, "different"),
    (b"a\nb", b"b\na", {"sort_lines": True}, "same"),
    (b"a\na\nb", b"a\nb", {"sort_lines": True}, "different"),
    (b"a b", b"ab", {"ignore_whitespace": True}, "same"),
    (b"a\n\nb", b"a\nb", {"ignore_whitespace": True}, "different"),
    (b"a\r\nb\r", b"a\nb", {}, "same"),
    (b"Xa bY\nX c dY", b"Qc dR\nQabR", {"ignore_start": 1, "ignore_end": 1, "ignore_whitespace": True, "sort_lines": True}, "same"),
])
def test_comparison_options(core, left, right, options, status):
    result = core.compare_directories({"a.txt": left}, {"a.txt": right}, core.CompareOptions(**options))
    assert result["results"][0]["status"] == status


@pytest.mark.parametrize("bad", [b"\xff", b"a\0b"])
def test_invalid_text_reports_per_file_error(core, bad):
    result = core.compare_directories({"bad": bad, "good": b"ok"}, {"bad": b"ok", "good": b"ok"}, core.CompareOptions())
    assert result["summary"]["error"] == 1
    assert result["summary"]["same"] == 1
    assert "message" in result["results"][0]


@pytest.mark.parametrize("options", [{"ignore_start": -1}, {"ignore_end": -1}, {"ignore_start": 0.5}, {"ignore_end": True}])
def test_reject_invalid_options(core, options):
    with pytest.raises(ValueError):
        core.CompareOptions(**options)


def test_one_sided_files_are_reported_without_decoding(core):
    result = core.compare_directories({"a.bin": b"\xff"}, {}, core.CompareOptions())
    assert result["summary"]["left_only"] == 1
    assert result["summary"]["error"] == 0


@pytest.mark.parametrize("encoding", ["cp949", "euc-kr", "utf-16", "utf-32"])
def test_identical_text_in_different_encodings_compares_equal(core, encoding):
    text = "내부망 파일 비교\n설정 값 확인\n"
    result = core.compare_directories({"a.txt": text.encode(encoding)}, {"a.txt": text.encode()}, core.CompareOptions())
    assert result["summary"]["same"] == 1
    assert result["summary"]["error"] == 0


def test_normalization_options_apply_after_legacy_decoding(core):
    left = "X가 나Y\nX다 라Y".encode("cp949")
    right = "Q다라R\nQ가나R".encode("utf-16")
    options = core.CompareOptions(sort_lines=True, ignore_whitespace=True, ignore_start=1, ignore_end=1)
    result = core.compare_directories({"a.txt": left}, {"a.txt": right}, options)
    assert result["summary"]["same"] == 1


def test_whitespace_option_matches_all_unicode_isspace_characters(core):
    whitespace = "".join(chr(value) for value in range(0x110000) if chr(value).isspace() and chr(value) not in "\r\n")
    text = "X" + whitespace + "😀가" + whitespace + "Y"
    assert core.normalize_lines(text.encode(), core.CompareOptions(ignore_whitespace=True)) == ["X😀가Y"]
