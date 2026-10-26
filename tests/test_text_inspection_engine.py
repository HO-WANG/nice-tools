"""Complete-file, read-only text inspection regressions."""
import codecs
from pathlib import Path
import unicodedata

import pytest


@pytest.mark.parametrize('strict_mode',[False,True])
def test_mongolian_free_variation_selectors_are_review_candidates(tmp_path,strict_mode):
    subject=engine()
    source=tmp_path/'mongolian.txt'
    source.write_text('A\u180b\u180c\u180d\u180fB',encoding='utf-8')
    cache=tmp_path/'cache'
    summary=subject.scan_file(source,cache,{'strict_mode':strict_mode})
    rows=subject.read_page(cache,0)['results']
    assert summary['findings']==4 and summary['kinds']['variation']==4
    assert [row['codepoint'] for row in rows]==['U+180B','U+180C','U+180D','U+180F']


def test_strict_decoder_must_not_publish_lone_unicode_surrogates(tmp_path):
    subject=engine()
    source=tmp_path/'surrogate.txt';source.write_bytes(b'+2AA-\0')
    cache=tmp_path/'cache'
    with pytest.raises(subject.InspectionError) as error:
        subject.scan_file(source,cache,{'encoding':'utf-7'})
    assert error.value.code=='encoding'
    assert not (cache/'metadata.json').exists()


def engine():
    from nice_tools import text_inspection_engine
    return text_inspection_engine


def scan(tmp_path, text, options=None):
    source = tmp_path / 'source.txt'
    source.write_text(text, encoding='utf-8', newline='')
    cache = tmp_path / 'cache'
    result = engine().scan_file(source, cache, options or {})
    return result, engine().read_page(cache, 0)


def test_options_are_strict_and_default_to_auto():
    subject = engine()
    assert subject.validate_options({}) == {'encoding': 'auto', 'include_common_whitespace': False, 'strict_mode': False}
    assert subject.validate_options({'encoding': 'UTF-16-LE', 'include_common_whitespace': True}) == {'encoding': 'utf-16-le', 'include_common_whitespace': True, 'strict_mode': False}
    for options in (None, [], {'encoding': 'not-a-real-codec'}, {'include_common_whitespace': 1}, {'unknown': True}):
        with pytest.raises(subject.InspectionError) as error:
            subject.validate_options(options)
        assert error.value.code == 'options'


def test_hidden_candidates_exclude_ordinary_letters_emoji_and_combining(tmp_path):
    text = '가e\u0301😀\u00a0\u200b\u200d\u202e\u3164\x00\x1b\ufeff\ufe0f\ufffd\ufdd0\u034f\u2800\u17b4\u2060'
    summary, page = scan(tmp_path, text)
    assert summary['characters'] == len(text)
    assert summary['lines'] == 1
    assert [row['kind'] for row in page['results']] == ['space', 'zero_width', 'zero_width', 'bidi', 'filler', 'control', 'control', 'zero_width', 'variation', 'replacement', 'noncharacter', 'variation', 'filler', 'filler', 'zero_width']
    assert [row['column'] for row in page['results']][:3] == [5, 6, 7]
    assert summary['findings'] == 15
    assert sum(summary['kinds'].values()) == 15
    for row in page['results']:
        assert set(row) == {'line', 'column', 'codepoint', 'name', 'kind', 'label', 'before', 'display', 'after'}
        assert row['display'].startswith('⟦')
        assert row['label']
        for field in ('before', 'display', 'after'):
            assert not any(unicodedata.category(c) in ('Cc', 'Cf', 'Zs', 'Zl', 'Zp') for c in row[field])


@pytest.mark.parametrize('common', [False, True])
def test_common_counts_and_crlf_positions_cross_tiny_chunks(tmp_path, monkeypatch, common):
    subject = engine()
    monkeypatch.setattr(subject, 'IO_CHUNK_BYTES', 1)
    summary, page = scan(tmp_path, ' A\t\r\nB\u200b\rC\n', {'include_common_whitespace': common})
    assert summary['characters'] == 10
    assert summary['lines'] == 3
    assert summary['common_whitespace'] == {'space': 1, 'tab': 1, 'crlf': 1, 'lf': 1, 'cr': 1}
    rows = page['results']
    hidden = [row for row in rows if row['kind'] != 'common']
    assert [(row['line'], row['column'], row['codepoint']) for row in hidden] == [(2, 2, 'U+200B')]
    assert len(rows) == (6 if common else 1)
    if common:
        assert [(row['line'], row['column']) for row in rows] == [(1, 1), (1, 3), (1, 4), (2, 2), (2, 3), (3, 2)]
        assert rows[2]['codepoint'] == 'U+000D U+000A'
    assert hidden[0]['before'] == '·A⟦TAB⟧⟦CRLF⟧B'
    assert hidden[0]['after'] == '⟦CR⟧C⟦LF⟧'


@pytest.mark.parametrize('text,lines', [('', 0), ('\n', 1), ('\r\n\n', 2), ('a\n', 1), ('a\nb', 2)])
def test_empty_and_trailing_newline_counts(tmp_path, text, lines):
    summary, page = scan(tmp_path, text)
    assert summary['lines'] == lines
    assert page['pages'] == 1
    assert page['results'] == []


@pytest.mark.parametrize('encoding,bom', [('utf-8', codecs.BOM_UTF8), ('utf-16-le', codecs.BOM_UTF16_LE), ('utf-16-be', codecs.BOM_UTF16_BE), ('utf-32-le', codecs.BOM_UTF32_LE), ('utf-32-be', codecs.BOM_UTF32_BE)])
def test_bom_is_metadata_embedded_bom_is_finding(tmp_path, encoding, bom):
    source = tmp_path / 'source.txt'
    raw = bom + '한글\ufeff'.encode(encoding)
    source.write_bytes(raw)
    cache = tmp_path / 'cache'
    summary = engine().scan_file(source, cache, {})
    assert summary['encoding'] == encoding
    assert summary['bom'] == encoding
    assert summary['file_size'] == len(raw)
    assert summary['characters'] == 3
    assert summary['findings'] == 1
    assert engine().read_page(cache, 0)['results'][0]['column'] == 3


@pytest.mark.parametrize('encoding,text', [('cp949', '안녕하세요 한글 텍스트'), ('cp932', 'こんにちは カタカナです'), ('cp1252', 'Hello café and résumé text'), ('utf-16-le', 'Hello world\n') , ('utf-32-be', 'Hello world\n')])
def test_automatic_legacy_and_bomless_unicode(tmp_path, encoding, text):
    source = tmp_path / 'source.txt'
    source.write_bytes(text.encode(encoding))
    summary = engine().scan_file(source, tmp_path / 'cache', {})
    assert summary['encoding'] == encoding
    assert summary['characters'] == len(text)
    assert summary['findings'] == 0


def test_override_and_raw_utf8_nul(tmp_path):
    source = tmp_path / 'source.txt'
    source.write_bytes(b'a\0b')
    summary = engine().scan_file(source, tmp_path / 'cache', {})
    assert summary['encoding'] == 'utf-8'
    assert summary['findings'] == 1
    source.write_bytes('한글\u200b'.encode('utf-16-le'))
    summary = engine().scan_file(source, tmp_path / 'manual', {'encoding': 'utf-16-le'})
    assert summary['characters'] == 3
    assert summary['findings'] == 1


def test_pages_are_complete_and_cache_cannot_be_clobbered(tmp_path):
    summary, first = scan(tmp_path, '\u200b' * 405)
    subject = engine()
    cache = tmp_path / 'cache'
    assert (first['total'], first['pages'], first['page_size']) == (405, 3, 200)
    rows = first['results'] + subject.read_page(cache, 1)['results'] + subject.read_page(cache, 2)['results']
    assert [row['column'] for row in rows] == list(range(1, 406))
    with pytest.raises(subject.InspectionError) as error:
        subject.scan_file(tmp_path / 'source.txt', cache, {})
    assert error.value.code == 'cache'
    assert subject.read_page(cache, 0) == first
    for bad in (-1, True, '0', 3):
        with pytest.raises(subject.InspectionError):
            subject.read_page(cache, bad)


def test_complete_large_file_late_finding_and_bad_tail(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    source.write_bytes(b'a' * (10 * 1024 * 1024 + 9) + '\u200b'.encode())
    original = source.read_bytes()
    progress = []
    summary = subject.scan_file(source, tmp_path / 'cache', {}, emit=progress.append)
    assert summary['characters'] == 10 * 1024 * 1024 + 10
    assert subject.read_page(tmp_path / 'cache', 0)['results'][0]['column'] == 10 * 1024 * 1024 + 10
    assert progress[-1] == {'completed': len(original), 'total': len(original), 'phase': 'scanning'}
    assert source.read_bytes() == original
    source.write_bytes(original + b'\xf0\x9f')
    with pytest.raises(subject.InspectionError) as error:
        subject.scan_file(source, tmp_path / 'bad', {'encoding': 'utf-8'})
    assert error.value.code == 'encoding'
    assert not (tmp_path / 'bad' / 'metadata.json').exists()


def test_cancellation_removes_partial_cache_and_preserves_source(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    original = b'a' * 500_000 + '\u200b'.encode()
    source.write_bytes(original)
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls > 5
    with pytest.raises(subject.InspectionCancelled):
        subject.scan_file(source, tmp_path / 'cache', {}, cancelled=cancelled)
    assert not (tmp_path / 'cache' / 'metadata.json').exists()
    assert source.read_bytes() == original


@pytest.mark.parametrize('text,counts', [('a\r', {'space': 0, 'tab': 0, 'crlf': 0, 'lf': 0, 'cr': 1}), ('\r', {'space': 0, 'tab': 0, 'crlf': 0, 'lf': 0, 'cr': 1})])
def test_final_standalone_cr_is_counted(tmp_path, text, counts):
    summary, page = scan(tmp_path, text, {'include_common_whitespace': True})
    assert summary['lines'] == 1
    assert summary['common_whitespace'] == counts
    assert page['results'][-1]['codepoint'] == 'U+000D'


def test_context_survives_many_decode_chunks(tmp_path, monkeypatch):
    subject = engine()
    monkeypatch.setattr(subject, 'IO_CHUNK_BYTES', 7)
    summary, page = scan(tmp_path, 'x' * 40 + '\u200b' + 'y' * 40)
    assert summary['findings'] == 1
    row = page['results'][0]
    assert row['before'] == 'x' * 24
    assert row['after'] == 'y' * 24
    assert row['column'] == 41


def test_cancellation_after_pages_does_not_publish_or_leave_rows(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    source.write_text('\u200b' * 600, encoding='utf-8')
    cache = tmp_path / 'cache'
    def cancelled():
        return (cache / 'page-00000000.json').exists()
    with pytest.raises(subject.InspectionCancelled):
        subject.scan_file(source, cache, {'encoding': 'utf-8'}, cancelled=cancelled)
    assert not cache.exists()


def test_incomplete_corrupt_pages_fail_safely(tmp_path):
    scan(tmp_path, '\u200b')
    cache = tmp_path / 'cache'
    (cache / 'page-00000000.json').write_text('[]', encoding='utf-8')
    with pytest.raises(engine().InspectionError) as error:
        engine().read_page(cache, 0)
    assert error.value.code == 'cache'


def test_cache_path_file_and_missing_source_have_sanitized_errors(tmp_path):
    subject = engine()
    occupied = tmp_path / 'occupied'
    occupied.write_text('keep', encoding='utf-8')
    source = tmp_path / 'secret-source-name.txt'
    source.write_text('text', encoding='utf-8')
    with pytest.raises(subject.InspectionError) as error:
        subject.scan_file(source, occupied, {})
    assert error.value.code == 'cache'
    assert occupied.read_text() == 'keep'
    source.unlink()
    with pytest.raises(subject.InspectionError) as error:
        subject.scan_file(source, tmp_path / 'empty-cache', {})
    assert error.value.code == 'encoding'
    assert 'secret-source-name' not in str(error.value)


def test_supplementary_selectors_noncharacters_and_other_format(tmp_path):
    summary, page = scan(tmp_path, 'A\u2028\u2029\u00ad\U000e0100\U0010ffff\u115f\u1160\uffa0\u17b5')
    assert summary['lines'] == 1
    assert [row['kind'] for row in page['results']] == ['space', 'space', 'format', 'variation', 'noncharacter', 'filler', 'filler', 'filler', 'filler']
    assert page['results'][3]['codepoint'] == 'U+E0100'
    assert page['results'][4]['column'] == 6


def test_auto_bom_unicode_malformed_tail_never_publishes(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    source.write_bytes(codecs.BOM_UTF16_LE + b'A\0' * 200_000 + b'\x00\xd8')
    cache = tmp_path / 'cache'
    with pytest.raises(subject.InspectionError) as error:
        subject.scan_file(source, cache, {})
    assert error.value.code == 'encoding'
    assert not cache.exists()


def test_failed_cache_publication_is_cache_error_and_cleans_pages(tmp_path, monkeypatch):
    subject = engine()
    source = tmp_path / 'source.txt'
    source.write_text('\u200b', encoding='utf-8')
    cache = tmp_path / 'cache'
    original_unlink = Path.unlink
    def denied_marker(path, *args, **kwargs):
        if path.name == '.building':
            raise PermissionError('sensitive temporary path')
        return original_unlink(path, *args, **kwargs)
    monkeypatch.setattr(Path, 'unlink', denied_marker)
    with pytest.raises(subject.InspectionError) as error:
        subject.scan_file(source, cache, {})
    assert error.value.code == 'cache'
    assert 'sensitive' not in str(error.value)
    assert not cache.exists()


def test_strict_allows_ascii_punctuation_and_assigned_hangul_jamo(tmp_path):
    text = ''.join(chr(value) for value in range(32, 127)) + '\t\r\n한글한ㄱㄴ\ua960\ua97c\ud7b0\ud7c6\ud7cb\ud7fb\uffa1\uffbe\uffc2\uffdc'
    summary, page = scan(tmp_path, text, {'strict_mode': True})
    assert summary['findings'] == 0
    assert page['results'] == []
    assert summary['lines'] == 2
    assert summary['common_whitespace'] == {'space': 1, 'tab': 1, 'crlf': 1, 'lf': 0, 'cr': 0}


def test_strict_reports_other_scripts_and_preserves_known_kinds(tmp_path):
    summary, page = scan(tmp_path, '한|12中文😀Ａé\u0301\u115f\u1160\u3164\uffa0\u200b\ua97d\ud7a4', {'strict_mode': True})
    assert summary['findings'] == 13
    assert [row['kind'] for row in page['results']] == ['strict'] * 6 + ['filler'] * 4 + ['zero_width'] + ['strict'] * 2
    assert page['results'][0]['column'] == 5
    assert page['results'][0]['label'] == '엄격모드 허용 외 문자'
    assert page['results'][0]['name'] == 'CJK UNIFIED IDEOGRAPH-4E2D'
    assert page['results'][2]['codepoint'] == 'U+1F600'


@pytest.mark.parametrize('chunk', [1, 3, 31])
def test_strict_crlf_and_common_findings_across_chunks(tmp_path, monkeypatch, chunk):
    monkeypatch.setattr(engine(), 'IO_CHUNK_BYTES', chunk)
    summary, page = scan(tmp_path, '한' * 30 + '|123\r\n中\t A\r끝\n', {'strict_mode': True, 'include_common_whitespace': True})
    assert summary['lines'] == 3
    assert [(row['line'], row['column'], row['kind']) for row in page['results']] == [(1, 35, 'common'), (2, 1, 'strict'), (2, 2, 'common'), (2, 3, 'common'), (2, 5, 'common'), (3, 2, 'common')]
    assert page['results'][0]['codepoint'] == 'U+000D U+000A'


def test_strict_large_file_keeps_late_forbidden_result(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    with source.open('wb') as output:
        for _ in range(11):
            output.write(b'|a123!?' * 150_000)
        output.write('한글中'.encode('utf-8'))
    summary = subject.scan_file(source, tmp_path / 'cache', {'strict_mode': True})
    assert summary['file_size'] > 10 * 1024 * 1024
    assert summary['findings'] == 1
    result = subject.read_page(tmp_path / 'cache', 0)['results'][0]
    assert result['column'] == 11_550_003
    assert result['codepoint'] == 'U+4E2D'


@pytest.mark.parametrize('alias,canonical', [('MS949', 'cp949'), ('euckr', 'euc-kr'), ('UTF8', 'utf-8'), ('utf-8-sig', 'utf-8-sig'), ('windows-1251', 'cp1251'), ('GB18030', 'gb18030'), ('iso2022_jp', 'iso2022-jp'), ('latin1', 'iso8859-1')])
def test_manual_codec_aliases_are_canonical_text_decoders(alias, canonical):
    assert engine().validate_options({'encoding': alias})['encoding'] == canonical


@pytest.mark.parametrize('codec,text', [('cp1251', 'Привет'), ('gb18030', '中文😀'), ('iso2022_jp', '日本語'), ('latin1', 'café')])
def test_manual_extra_text_codecs_scan_complete_file(tmp_path, codec, text):
    subject = engine()
    source = tmp_path / 'source.txt'
    source.write_bytes(text.encode(codec))
    summary = subject.scan_file(source, tmp_path / 'cache', {'encoding': codec, 'strict_mode': True})
    assert summary['characters'] == len(text)
    assert summary['findings'] == (1 if codec == 'latin1' else len(text))


@pytest.mark.parametrize('codec', ['base64', 'hex', 'zlib', 'bz2', 'rot13', 'punycode', 'idna', 'unicode_escape', 'raw_unicode_escape', 'unknown-secret-codec', 'a' * 129, 'utf-8\n', ' utf-8', '', None])
def test_transform_or_unknown_manual_codec_is_rejected_safely(codec):
    with pytest.raises(engine().InspectionError) as error:
        engine().validate_options({'encoding': codec})
    assert error.value.code == 'options'
    assert 'unknown-secret-codec' not in str(error.value)


def test_utf8_sig_metadata_does_not_consume_embedded_bom(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    source.write_bytes(codecs.BOM_UTF8 + '\ufeffA'.encode())
    summary = subject.scan_file(source, tmp_path / 'cache', {'encoding': 'utf-8-sig'})
    assert summary['bom'] == 'utf-8'
    assert summary['characters'] == 2
    assert summary['findings'] == 1


def test_strict_flag_requires_real_boolean():
    for value in (None, 0, 1, 'true'):
        with pytest.raises(engine().InspectionError) as error:
            engine().validate_options({'strict_mode': value})
        assert error.value.code == 'options'


@pytest.mark.parametrize('encoding', ['UTF8', 'auto'])
def test_fast_scan_reads_source_once_plus_bounded_header(tmp_path, monkeypatch, encoding):
    subject = engine()
    source = tmp_path / 'source.txt'
    raw = ('한글|123ABC' * 50_000 + '中').encode('utf-8')
    source.write_bytes(raw)
    original_open = Path.open
    observed = 0
    class CountedReader:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            self.stream.__enter__()
            return self
        def __exit__(self, *args):
            return self.stream.__exit__(*args)
        def __getattr__(self, name):
            return getattr(self.stream, name)
        def read(self, *args):
            nonlocal observed
            data = self.stream.read(*args)
            observed += len(data)
            return data
    def counted_open(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return CountedReader(stream) if path == source and mode == 'rb' else stream
    monkeypatch.setattr(Path, 'open', counted_open)
    summary = subject.scan_file(source, tmp_path / 'cache', {'encoding': encoding, 'strict_mode': True})
    assert summary['findings'] == 1
    if encoding == 'UTF8':
        assert observed == len(raw) + 4
    else:
        assert observed <= len(raw) + 128 * 1024 + 4


def test_manual_decode_failure_after_completed_pages_removes_cache(tmp_path, monkeypatch):
    subject = engine()
    monkeypatch.setattr(subject, 'IO_CHUNK_BYTES', 64)
    source = tmp_path / 'source.txt'
    source.write_bytes('中'.encode('utf-8') * 600 + b'\xf0\x9f')
    cache = tmp_path / 'cache'
    with pytest.raises(subject.InspectionError) as error:
        subject.scan_file(source, cache, {'encoding': 'utf-8', 'strict_mode': True})
    assert error.value.code == 'encoding'
    assert not cache.exists()


def test_auto_late_cp949_strict_decode_retries_complete_file(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    raw = b'a' * (10 * 1024 * 1024 + 7) + '한글\x00'.encode('cp949')
    source.write_bytes(raw)
    summary = subject.scan_file(source, tmp_path / 'cache', {'strict_mode': True})
    assert summary['encoding'] == 'cp949'
    assert summary['characters'] == 10 * 1024 * 1024 + 10
    assert summary['findings'] == 1
    assert subject.read_page(tmp_path / 'cache', 0)['results'][0]['codepoint'] == 'U+0000'
    assert source.read_bytes() == raw


def test_auto_retry_discards_prior_utf8_pages_and_keeps_source(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    raw = 'é'.encode() * 500 + b'A' * (128 * 1024) + '한글'.encode('cp949')
    source.write_bytes(raw)
    cache = tmp_path / 'cache'
    provisional = []
    def progress(event):
        provisional.append(len(list(cache.glob('page-*.json'))))
    summary = subject.scan_file(source, cache, {'strict_mode': True}, emit=progress)
    assert max(provisional) >= 2
    assert summary['encoding'] == 'cp949'
    assert summary['characters'] == 500 + 128 * 1024 + 2
    assert summary['findings'] == 0
    assert subject.read_page(cache, 0)['results'] == []
    assert len(list(cache.glob('page-*.json'))) == 1
    assert source.read_bytes() == raw


def test_auto_preserves_late_bomless_wide_alignment_evidence(tmp_path):
    subject = engine()
    source = tmp_path / 'source.txt'
    # UTF-16 中 is two ordinary ASCII bytes; its head has no NUL lane hint.
    # Later ASCII supplies the same strong whole-file evidence as before.
    text = '中' * 65_536 + 'A' * 65_536
    source.write_bytes(text.encode('utf-16-le'))
    summary = subject.scan_file(source, tmp_path / 'cache', {})
    assert summary['encoding'] == 'utf-16-le'
    assert summary['characters'] == 131_072
    assert summary['findings'] == 0
