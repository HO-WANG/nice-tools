import hashlib
import importlib
import os

import pytest


@pytest.fixture
def ase_files():
    try:
        return importlib.import_module("nice_tools.ase_files")
    except ModuleNotFoundError:
        pytest.fail("The ASE character-file inspector is not implemented.")


def column(name="value", type_name="varchar", type_id=39, **changes):
    result = {
        "position": 1, "name": name, "type_id": type_id, "type_name": type_name,
        "length": 100, "precision": 0, "scale": 0, "nullable": True,
        "identity": False,
    }
    result.update(changes)
    return result


def inspect(ase_files, tmp_path, data, columns=None, **options):
    path = tmp_path / "data.bcp"
    path.write_bytes(data)
    return ase_files.inspect_file(path, columns or [column()], ase_files.parse_options(options))


def test_options_normalize_terminators_and_defaults(ase_files):
    assert ase_files.parse_options({}) == {
        "direction": "in", "encoding": "utf-8", "field_hex": "09", "row_hex": "0a",
        "batch_size": 1000, "keep_identity": False,
    }
    assert ase_files.parse_options({
        "direction": "out", "encoding": "cp949", "field_terminator": "가\\t\\\\",
        "row_terminator": "\\r\\n", "batch_size": 7, "keep_identity": True,
    }) == {
        "direction": "out", "encoding": "cp949", "field_hex": "b0a1095c",
        "row_hex": "0d0a", "batch_size": 7, "keep_identity": True,
    }


@pytest.mark.parametrize("options", [
    {"direction": "other"}, {"encoding": "utf-16"}, {"field_terminator": ""},
    {"row_terminator": ""}, {"field_terminator": "\0"}, {"row_terminator": "\\0"},
    {"field_terminator": "\\x09"}, {"field_terminator": "\\"},
    {"row_terminator": "\\t"}, {"row_terminator": "a" * 33},
    {"batch_size": 0}, {"batch_size": True}, {"batch_size": 1.5},
    {"keep_identity": "false"}, {"encoding": "euc-kr", "field_terminator": "😀"},
])
def test_options_reject_invalid_or_ambiguous_settings(ase_files, options):
    with pytest.raises(ValueError):
        ase_files.parse_options(options)


def test_real_file_fingerprint_rows_and_bcp_last_field_rule(ase_files, tmp_path):
    data = b"line1\nline2\tlast\tfield\nsecond\tend\n"
    path = tmp_path / "input.bcp"
    path.write_bytes(data)
    result = ase_files.inspect_file(path, [column(), column(position=2)], ase_files.parse_options({}))
    assert result["rows"] == 2
    assert result["bytes"] == len(data)
    assert result["sample"] == [["line1\nline2", "last\tfield"], ["second", "end"]]
    assert result["fingerprint"] == {
        "size": len(data), "mtime_ns": path.stat().st_mtime_ns, "sha256": hashlib.sha256(data).hexdigest(),
    }


def test_delimiters_and_multibyte_characters_across_tiny_reads(ase_files, tmp_path, monkeypatch):
    monkeypatch.setattr(ase_files, "_READ_CHUNK_SIZE", 1)
    result = inspect(ase_files, tmp_path, "가<>나\r\n다<>라\r\n".encode("cp949"),
                     [column(), column(position=2)], encoding="cp949",
                     field_terminator="<>", row_terminator="\\r\\n")
    assert result["sample"] == [["가", "나"], ["다", "라"]]
    assert result["rows"] == 2


@pytest.mark.parametrize("encoding", ["utf-8", "cp949", "euc-kr"])
def test_selected_korean_encoding_is_decoded_strictly(ase_files, tmp_path, encoding):
    result = inspect(ase_files, tmp_path, "한국어\n".encode(encoding), encoding=encoding)
    assert result["sample"] == [["한국어"]]


def test_empty_bytes_are_null_and_single_nul_is_empty_text(ase_files, tmp_path):
    result = inspect(ase_files, tmp_path, b"\t\0\n", [column(), column(position=2, nullable=False)])
    assert result["sample"] == [[None, ""]]
    with pytest.raises(ValueError, match="NULL"):
        inspect(ase_files, tmp_path, b"\n", [column(nullable=False)])


@pytest.mark.parametrize("data", [b"no final delimiter", b"a\tb", b"a\0b\n", b"\xff\n"])
def test_malformed_files_fail_without_exposing_values(ase_files, tmp_path, data):
    with pytest.raises(ValueError) as exc:
        inspect(ase_files, tmp_path, data)
    assert "no final delimiter" not in str(exc.value)
    assert "a\\x00b" not in str(exc.value)


def test_crlf_requires_explicit_row_delimiter(ase_files, tmp_path):
    assert inspect(ase_files, tmp_path, b"text\r\n")["sample"] == [["text\r"]]
    assert inspect(ase_files, tmp_path, b"text\r\n", row_terminator="\\r\\n")["sample"] == [["text"]]


def test_large_file_is_fully_validated_after_preview_limit(ase_files, tmp_path):
    path = tmp_path / "large.bcp"
    row = b"x" * 1024 + b"\n"
    with path.open("wb") as stream:
        for _ in range(11000):
            stream.write(row)
    options = ase_files.parse_options({})
    columns = [column(type_name="text", type_id=35)]
    result = ase_files.inspect_file(path, columns, options)
    assert result["bytes"] > 10 * 1024 * 1024
    assert result["rows"] == 11000
    assert len(result["sample"]) == 20
    assert result["sample"][0][0].startswith("x" * 400)
    assert len(result["sample"][0][0]) < 430
    with path.open("ab") as stream:
        stream.write(b"\xff\n")
    with pytest.raises(ValueError, match="11001"):
        ase_files.inspect_file(path, columns, options)


def test_oversized_single_text_field_does_not_limit_file_size(ase_files, tmp_path):
    path = tmp_path / "long-text.bcp"
    with path.open("wb") as stream:
        for _ in range(11000):
            stream.write(b"x" * 1024)
        stream.write(b"\n")
    result = ase_files.inspect_file(path, [column(type_name="text", type_id=35)], ase_files.parse_options({}))
    assert result["rows"] == 1
    assert len(result["sample"][0][0]) < 430


@pytest.mark.parametrize("value,valid", [
    (b"123.45", True), (b"123.4500", True), (b"0.00", True), (b"1e-2", True),
    (b"1234.5", False), (b"1.234", False), (b"NaN", False), (b"Infinity", False),
    (b"1e100000", False), (b"-0.00001", False),
])
def test_decimal_exact_precision_and_scale(ase_files, tmp_path, value, valid):
    columns = [column(type_name="numeric", type_id=108, precision=5, scale=2)]
    if valid:
        assert inspect(ase_files, tmp_path, value + b"\n", columns)["rows"] == 1
    else:
        with pytest.raises(ValueError):
            inspect(ase_files, tmp_path, value + b"\n", columns)


@pytest.mark.parametrize("type_name,type_id,valid,invalid", [
    ("tinyint", 48, b"255", b"256"), ("smallint", 52, b"-32768", b"32768"),
    ("int", 56, b"2147483647", b"2147483648"),
    ("bigint", 191, b"-9223372036854775808", b"9223372036854775808"),
    ("bit", 50, b"1", b"2"), ("money", 60, b"922337203685477.5807", b"922337203685477.5808"),
    ("smallmoney", 122, b"-214748.3648", b"-214748.3649"),
    ("float", 62, b"1.5", b"NaN"), ("real", 59, b"1.5", b"1e100"),
])
def test_native_numeric_limits(ase_files, tmp_path, type_name, type_id, valid, invalid):
    columns = [column(type_name=type_name, type_id=type_id)]
    assert inspect(ase_files, tmp_path, valid + b"\n", columns)["rows"] == 1
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, invalid + b"\n", columns)


@pytest.mark.parametrize("value", [b"1.0", b"1e1", b" 1", b"1 ", b"+", b"NaN"])
def test_integer_syntax_is_strict(ase_files, tmp_path, value):
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, value + b"\n", [column(type_name="int", type_id=56)])


def test_character_capacity_uses_selected_encoding_bytes(ase_files, tmp_path):
    columns = [column(length=2)]
    assert inspect(ase_files, tmp_path, "가\n".encode("cp949"), columns, encoding="cp949")["rows"] == 1
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, "가\n".encode("utf-8"), columns)


def test_trailing_spaces_are_preserved_in_preview_and_fingerprint(ase_files, tmp_path):
    data = b"value \n"
    result = inspect(ase_files, tmp_path, data)
    assert result["sample"] == [["value "]]
    assert result["fingerprint"]["sha256"] == hashlib.sha256(data).hexdigest()


def test_identity_is_present_but_not_validated_unless_retained(ase_files, tmp_path):
    columns = [column(type_name="int", type_id=56, nullable=False, identity=True), column(position=2)]
    result = inspect(ase_files, tmp_path, b"\tvalue\n", columns)
    assert result["sample"] == [[None, "value"]]
    assert any("IDENTITY" in warning for warning in result["warnings"])
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, b"\tvalue\n", columns, keep_identity=True)
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, b"value\n", columns)


@pytest.mark.parametrize("type_name,type_id", [
    ("binary", 45), ("varbinary", 37), ("image", 34), ("timestamp", 80),
    ("encrypted varchar", 39), ("mystery", 999),
])
def test_unsupported_import_types_are_rejected(ase_files, tmp_path, type_name, type_id):
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, b"value\n", [column(type_name=type_name, type_id=type_id)])


def test_empty_file_and_progress_are_safe(ase_files, tmp_path):
    path = tmp_path / "empty.bcp"
    path.write_bytes(b"")
    progress = []
    result = ase_files.inspect_file(path, [column()], ase_files.parse_options({}), progress.append)
    assert result["rows"] == 0 and result["sample"] == []
    assert result["fingerprint"]["sha256"] == hashlib.sha256(b"").hexdigest()
    assert progress[-1] == {"rows": 0, "bytes": 0}


def test_long_numeric_spelling_has_no_arbitrary_field_cap(ase_files, tmp_path, monkeypatch):
    monkeypatch.setattr(ase_files, "_READ_CHUNK_SIZE", 17)
    columns = [column(type_name="numeric", type_id=108, precision=5, scale=2)]
    assert inspect(ase_files, tmp_path, b"0" * 10000 + b"123.450" + b"0" * 10000 + b"\n", columns)["rows"] == 1
    assert inspect(ase_files, tmp_path, b"12345" + b"0" * 10000 + b"e-10002\n", columns)["rows"] == 1
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, b"123.45" + b"0" * 10000 + b"1\n", columns)


@pytest.mark.parametrize("payload", [{"encoding": []}, {"encoding": {}}])
def test_options_invalid_encoding_types_raise_validation_error(ase_files, payload):
    with pytest.raises(ValueError):
        ase_files.parse_options(payload)


def test_normalized_options_are_idempotent(ase_files):
    options = ase_files.parse_options({"direction": "OUT", "row_terminator": "\\r\\n"})
    assert ase_files.parse_options(options) == options


@pytest.mark.parametrize("value", [b"1e-100", b"3.4028234663852887e38"])
def test_real_float32_range_is_not_only_double_range(ase_files, tmp_path, value):
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, value + b"\n", [column(type_name="real", type_id=59)])


@pytest.mark.parametrize("value", [b"1e-9999999999999999999999999999", b"1e9999999999999999999999999999"])
def test_unbounded_exponents_are_validated_without_integer_conversion(ase_files, tmp_path, value):
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, value + b"\n", [column(type_name="float", type_id=62)])


def test_zero_with_huge_exponent_is_exact(ase_files, tmp_path):
    assert inspect(ase_files, tmp_path, b"0e999999999999999999999999\n", [
        column(type_name="numeric", type_id=108, precision=5, scale=2),
    ])["rows"] == 1


def test_fingerprint_rejects_file_metadata_change_during_scan(ase_files, tmp_path):
    path = tmp_path / "changing.bcp"
    path.write_bytes((b"x" * 1024 + b"\n") * 2000)
    changed = False

    def emit(progress):
        nonlocal changed
        assert set(progress) == {"rows", "bytes"}
        if not changed:
            metadata = path.stat()
            os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))
            changed = True

    with pytest.raises(ValueError, match="변경"):
        ase_files.inspect_file(path, [column(type_name="text", type_id=35)], ase_files.parse_options({}), emit)


@pytest.mark.parametrize("type_name,type_id", [("unichar", 135), ("univarchar", 155)])
def test_unicode_capacity_counts_utf16_storage_not_file_encoding(ase_files, tmp_path, type_name, type_id):
    columns = [column(type_name=type_name, type_id=type_id, length=2)]
    assert inspect(ase_files, tmp_path, "가\n".encode(), columns)["sample"] == [["가"]]
    with pytest.raises(ValueError):
        inspect(ase_files, tmp_path, "😀\n".encode(), columns)
    columns[0]["length"] = 4
    assert inspect(ase_files, tmp_path, "😀\n".encode(), columns)["sample"] == [["😀"]]


def test_nonunicode_character_capacity_explains_server_charset_assumption(ase_files, tmp_path):
    result = inspect(ase_files, tmp_path, b"value\n")
    assert any("서버 문자셋" in message for message in result["warnings"])
