import codecs
import importlib
import random

import pytest


@pytest.fixture
def decode():
    def call(content):
        try:
            module = importlib.import_module("nice_tools.text_encoding")
        except ModuleNotFoundError:
            pytest.fail("자동 텍스트 인코딩 감지 모듈이 아직 구현되지 않았습니다.")
        return module.decode_text(content)
    return call


@pytest.mark.parametrize(("text", "encoding"), [
    ("", "utf-8"),
    ("hello\tworld\r\n\f 안녕 😀", "utf-8"),
    ("안녕", "cp949"),
    ("테스트", "cp949"),
    ("뷁쀍똠휗", "cp949"),
    ("name=홍길동\r\nvalue=안녕하세요\r\n", "cp949"),
    ("우리 회사의 설정 파일입니다.\r\n", "euc-kr"),
    ("こんにちは", "shift_jis"),
    ("こんにちは世界。設定ファイルを比較してください。\r\n名前=山田太郎", "shift_jis"),
    ("café", "cp1252"),
    ("Café — “Price €12.50”\r\nRésumé déjà vu.\r\n", "cp1252"),
    ("Überprüfung der Dateien ist erfolgreich.", "cp1252"),
    ("São Paulo", "cp1252"),
    ("Привет мир. Это текстовый файл настроек.\r\n", "cp1251"),
    ("你好世界，这是一个中文配置文件。请比较文件内容。\n", "gb18030"),
    ("你好世界，這是一個中文設定檔案。請比較檔案內容。\n", "big5"),
    ("name=你好世界，这是一个中文配置文件。请比较文件内容。\nother=configuration\n", "gb18030"),
    ("name=你好世界，這是一個中文設定檔案。請比較檔案內容。\nother=configuration\n", "big5"),
])
def test_decodes_complete_text_without_changing_characters(decode, text, encoding):
    assert decode(text.encode(encoding)) == text


@pytest.mark.parametrize(("bom", "encoding"), [
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
])
def test_bom_identifies_unicode_and_is_removed(decode, bom, encoding):
    text = "hello\r\n안녕 😀\n"
    assert decode(bom + text.encode(encoding)) == text


@pytest.mark.parametrize("encoding", ["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"])
def test_detects_aligned_bomless_unicode(decode, encoding):
    text = "name=hello\r\nvalue=world\n"
    assert decode(text.encode(encoding)) == text


@pytest.mark.parametrize("content", [
    b"\xff", b"a\x00b", b"\xff\xfe\x00",
    b"\xfe\xff\xd8\x00", b"\xff\xfe\x00\xd8",
    b"\xff\xfe\x00\x00\x00\xd8\x00\x00",
    b"\x00\x00\xfe\xff\x00\x00\xd8\x00",
    b"\xef\xbb\xbf\xff", b"\xef\xbb", b"\x00\x00\xfe",
    random.Random(123).randbytes(1024),
])
def test_rejects_binary_ambiguous_and_malformed_unicode(decode, content):
    with pytest.raises(ValueError):
        decode(content)


def test_decodes_korean_tail_beyond_twelve_mib(decode):
    prefix = b"A\n" * (6 * 1024 * 1024)
    left = decode(prefix + "끝:원본\n".encode("cp949"))
    right = decode(prefix + "끝:수정\n".encode("cp949"))
    assert len(left) == len(prefix) + len("끝:원본\n")
    assert left.endswith("끝:원본\n")
    assert right.endswith("끝:수정\n")
    assert left != right


def test_strict_bom_decode_checks_invalid_byte_beyond_twelve_mib(decode):
    content = codecs.BOM_UTF8 + b"A\n" * (6 * 1024 * 1024) + b"\xff"
    with pytest.raises(ValueError):
        decode(content)


def test_candidate_validation_checks_controls_beyond_twelve_mib(decode):
    content = "안녕하세요\n".encode("cp949") + b"A\n" * (6 * 1024 * 1024) + b"\x01"
    with pytest.raises(ValueError):
        decode(content)


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_preserves_private_glyphs_in_identified_unicode(decode, encoding):
    text = "사용자 글꼴: \ue123 \U000f0123"
    assert decode(text.encode(encoding)) == text


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "utf-16", "utf-32"])
def test_preserves_text_separators_and_terminal_controls_in_identified_unicode(decode, encoding):
    # Existing comparison supported these valid Unicode characters; detection
    # must not turn logs and separator-delimited text into unreadable files.
    text = "item\x1fvalue\x1erecord\x07\x1b[31mred\x1b[0m\u0085next"
    assert decode(text.encode(encoding)) == text


def test_rejects_short_ambiguous_legacy_bytes_without_text_evidence(decode):
    with pytest.raises(ValueError):
        decode(b"\x80\x80")
