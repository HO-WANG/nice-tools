"""Read-only, strict streaming inspection of Unicode review candidates.

Candidates may be intentional (emoji variation selectors and joiners, for
example). No content is rewritten. Memory is bounded by decoding chunks, short
contexts and one 200-result page, even for a file consisting of one huge line.
Auto encoding detection is best effort; ambiguous input needs a manual codec.
"""

import codecs
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import unicodedata

from . import text_encoding as detection
from .local_compare_engine import _TextSample, _byte_evidence

IO_CHUNK_BYTES = 128 * 1024
PAGE_SIZE = 200
CONTEXT_RADIUS = 24
_SURROGATES = re.compile('[\ud800-\udfff]')
_NON_FILE_CODECS = frozenset(('punycode', 'idna', 'unicode-escape', 'raw-unicode-escape'))
# Assigned syllables/Jamo, including compatibility and halfwidth Jamo.
# Explicit fillers and unassigned gaps are excluded from these ranges.
_STRICT_HANGUL = ('\u1100-\u115e\u1161-\u11ff\u3131-\u3163\u3165-\u318e'
                  '\ua960-\ua97c\uac00-\ud7a3\ud7b0-\ud7c6\ud7cb-\ud7fb'
                  '\uffa1-\uffbe\uffc2-\uffc7\uffca-\uffcf\uffd2-\uffd7\uffda-\uffdc')
_MESSAGES = {
    'options': '텍스트 검사 옵션이 올바르지 않습니다.',
    'encoding': '선택한 인코딩으로 전체 파일을 올바르게 읽을 수 없습니다.',
    'cache': '텍스트 검사 결과를 저장하거나 읽을 수 없습니다.',
}
_LABELS = {'space': '특수 공백', 'zero_width': '너비 없는 문자', 'control': '제어 문자',
           'bidi': '글자 방향 제어', 'format': '서식 제어', 'filler': '빈칸 채움 문자',
           'variation': '변형 선택·결합 제어', 'replacement': '대체 문자',
           'noncharacter': '유니코드 비문자', 'common': '일반 공백·줄바꿈',
           'strict': '엄격모드 허용 외 문자'}
_FILLERS = frozenset('\u115f\u1160\u3164\uffa0\u2800\u17b4\u17b5')
_ZERO_WIDTH = frozenset('\u180e\u200b\u200c\u200d\u2060\ufeff')
_BIDI = frozenset('\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069')
_COMMON = {' ': 'SPACE', '\t': 'TAB', '\r\n': 'CRLF', '\n': 'LF', '\r': 'CR'}
_CONTROL_NAMES = {0: 'NULL', 7: 'ALERT', 8: 'BACKSPACE', 9: 'CHARACTER TABULATION',
                  10: 'LINE FEED', 11: 'LINE TABULATION', 12: 'FORM FEED',
                  13: 'CARRIAGE RETURN', 27: 'ESCAPE', 127: 'DELETE', 133: 'NEXT LINE'}


class InspectionError(ValueError):
    """Sanitized typed error suitable for the worker JSON boundary."""
    def __init__(self, code):
        self.code = code
        super().__init__(_MESSAGES[code])


class _WideEncodingEvidence(ValueError):
    """Late NUL alignment warrants full detection before publishing a cache."""


class InspectionCancelled(ValueError):
    def __init__(self):
        super().__init__('텍스트 검사가 취소되었습니다. (cancelled)')


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise InspectionCancelled()


def _canonical_encoding(value):
    if (not isinstance(value, str) or not 1 <= len(value) <= 128
            or value != value.strip()
            or any(unicodedata.category(char) in ('Cc', 'Cf') for char in value)):
        raise InspectionError('options')
    if value.lower() == 'auto':
        return 'auto'
    try:
        codec = codecs.lookup(value)
        canonical = codec.name.replace('_', '-')
        if not codec._is_text_encoding or canonical in _NON_FILE_CODECS:
            raise InspectionError('options')
        decoder = codec.incrementaldecoder
        if decoder is None or not isinstance(decoder(errors='strict').decode(b'', final=False), str):
            raise InspectionError('options')
        return canonical
    except (LookupError, UnicodeError, TypeError, ValueError):
        raise InspectionError('options') from None


def validate_options(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - {'encoding', 'include_common_whitespace', 'strict_mode'}:
        raise InspectionError('options')
    common = raw.get('include_common_whitespace', False)
    strict = raw.get('strict_mode', False)
    if type(common) is not bool or type(strict) is not bool:
        raise InspectionError('options')
    return {'encoding': _canonical_encoding(raw.get('encoding', 'auto')),
            'include_common_whitespace': common, 'strict_mode': strict}


def _probe(path, encoding, cancelled):
    """Full strict scalar decode, permitting reportable NUL and controls."""
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
        with path.open('rb') as stream:
            sample = _TextSample(os.fstat(stream.fileno()).st_size)
            position = 0
            while True:
                _check_cancel(cancelled)
                raw = stream.read(IO_CHUNK_BYTES)
                text = decoder.decode(raw, final=not raw)
                if not text.isascii() and _SURROGATES.search(text):
                    raise UnicodeError('Invalid Unicode scalar.')
                sample.add(text, position)
                position += len(raw)
                if not raw:
                    return sample.text()
    except UnicodeError:
        return None


def _wide_candidates(size, zeroes):
    candidates = []
    if size and size % 4 == 0:
        for codec, lanes in (('utf-32-le', (2, 3)), ('utf-32-be', (0, 1))):
            if all(zeroes[lane] / (size // 4) >= .75 for lane in lanes):
                candidates.append(codec)
    if size and size % 2 == 0:
        even = (zeroes[0] + zeroes[2]) / (size // 2)
        odd = (zeroes[1] + zeroes[3]) / (size // 2)
        for codec, high, low in (('utf-16-le', odd, even), ('utf-16-be', even, odd)):
            if high >= .30 and low < .10:
                candidates.append(codec)
    return candidates


def _encoding(path, requested, cancelled, *, optimistic=True):
    with path.open('rb') as stream:
        prefix = stream.read(128 * 1024 if requested == 'auto' and optimistic else 4)
    bom_encoding = None
    bom_size = 0
    for bom, codec in detection._BOMS:
        if prefix.startswith(bom):
            bom_encoding, bom_size = codec, len(bom)
            break
    if requested != 'auto':
        if bom_encoding and (requested == bom_encoding or requested == 'utf-8-sig' and bom_encoding == 'utf-8' or requested in ('utf-16', 'utf-32') and bom_encoding.startswith(requested + '-')):
            return bom_encoding, bom_encoding, bom_size
        return requested, None, 0
    if bom_encoding:
        return bom_encoding, bom_encoding, bom_size
    # UTF-8 usually needs just one full read. Only the bounded header guides
    # this optimization: the actual scan still validates every byte strictly.
    # Strong UTF-16/32 lane evidence keeps the existing full detection path.
    if optimistic and not _wide_candidates(len(prefix), [prefix[lane::4].count(0) for lane in range(4)]):
        return 'utf-8', None, 0
    size, zeroes, _, _ = _byte_evidence(path, lambda: _check_cancel(cancelled))
    candidates = _wide_candidates(size, zeroes)
    for codec in candidates + ['utf-8']:
        if _probe(path, codec, cancelled) is not None:
            return codec, None, 0
    korean = _probe(path, 'cp949', cancelled)
    japanese = _probe(path, 'cp932', cancelled)
    hangul = detection._script_counts(korean)[0] if korean is not None else 0
    kana = detection._script_counts(japanese)[1] if japanese is not None else 0
    if kana and (not hangul or kana >= hangul):
        return 'cp932', None, 0
    if korean is not None and detection._korean_text(korean, hangul):
        return 'cp949', None, 0
    western = _probe(path, 'cp1252', cancelled)
    if western is not None and detection._western_text(western) and detection._western_words(western):
        return 'cp1252', None, 0
    raise InspectionError('encoding')


def _kind(char):
    if char in _COMMON:
        return 'common'
    number = ord(char)
    if char in _FILLERS:
        return 'filler'
    if char in _BIDI:
        return 'bidi'
    if char in _ZERO_WIDTH:
        return 'zero_width'
    if number == 0x034f or 0x180b <= number <= 0x180f or 0xfe00 <= number <= 0xfe0f or 0xe0100 <= number <= 0xe01ef:
        return 'variation'
    if number == 0xfffd:
        return 'replacement'
    if 0xfdd0 <= number <= 0xfdef or number & 0xffff in (0xfffe, 0xffff):
        return 'noncharacter'
    category = unicodedata.category(char)
    if category in ('Zs', 'Zl', 'Zp'):
        return 'space'
    if category == 'Cc':
        return 'control'
    if category == 'Cf':
        return 'format'
    return None


@lru_cache(maxsize=4)
def _candidates(common, strict=False):
    # Build once per option. A C regex skips normal letters/entire huge lines;
    # Python only visits review candidates and CR/LF positioning boundaries.
    if strict:
        allowed = ('\x21-\x7e' if common else '\t\x20-\x7e') + _STRICT_HANGUL
        return re.compile('\r\n|[^' + allowed + ']')
    values = []
    for value in range(0x110000):
        char = chr(value)
        category = unicodedata.category(char)
        if category in ('Cc', 'Cf', 'Zs', 'Zl', 'Zp') or char in _FILLERS or value == 0x034f or 0x180b <= value <= 0x180f or 0xfe00 <= value <= 0xfe0f or 0xe0100 <= value <= 0xe01ef or value == 0xfffd or 0xfdd0 <= value <= 0xfdef or value & 0xffff in (0xfffe, 0xffff):
            if common or char not in (' ', '\t'):
                values.append(value)
    ranges = []
    left = right = values[0]
    for value in values[1:]:
        if value == right + 1:
            right = value
            continue
        ranges.append(re.escape(chr(left)) + ('-' + re.escape(chr(right)) if right > left else ''))
        left = right = value
    ranges.append(re.escape(chr(left)) + ('-' + re.escape(chr(right)) if right > left else ''))
    return re.compile('\r\n|[' + ''.join(ranges) + ']')


def _codepoint(text):
    return ' '.join(f'U+{ord(char):04X}' for char in text)


def _safe(text):
    pieces = []
    position = 0
    while position < len(text):
        char = text[position]
        if char == '\r' and text[position:position + 2] == '\r\n':
            pieces.append('⟦CRLF⟧')
            position += 2
            continue
        if char == ' ':
            pieces.append('·')
        elif char in _COMMON:
            pieces.append('⟦' + _COMMON[char] + '⟧')
        elif _kind(char) is not None or unicodedata.category(char) in ('Mn', 'Me', 'Cs'):
            # Combining marks remain legitimate content, but standalone marks
            # in preview can alter adjacent labels; show their code points.
            pieces.append('⟦' + _codepoint(char) + '⟧')
        else:
            pieces.append(char)
        position += 1
    return ''.join(pieces)


def _write_json(path, value):
    try:
        temporary = path.with_suffix('.tmp')
        with temporary.open('x', encoding='utf-8', newline='\n') as output:
            json.dump(value, output, ensure_ascii=True, separators=(',', ':'))
        temporary.replace(path)
    except OSError:
        raise InspectionError('cache') from None


def _page_path(cache_dir, page):
    return cache_dir / f'page-{page:08d}.json'


def _stamp(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _assert_unchanged(source, initial):
    if _stamp(initial) != _stamp(source.stat()):
        raise InspectionError('encoding')


def _retry_scan(source, cache_dir, options, initial, emit, cancelled):
    """Retry a failed provisional UTF-8 scan, never reusing its disk pages."""
    try:
        _check_cancel(cancelled)
        _assert_unchanged(source, initial)
        encoding, _, _ = _encoding(source, 'auto', cancelled, optimistic=False)
        _assert_unchanged(source, initial)
        result = scan_file(source, cache_dir, {**options, 'encoding': encoding}, emit, cancelled)
        _assert_unchanged(source, initial)
        return result
    except BaseException as error:
        shutil.rmtree(cache_dir, ignore_errors=True)
        if isinstance(error, (UnicodeError, LookupError, OSError)):
            raise InspectionError('encoding') from None
        raise


def scan_file(source: Path, cache_dir: Path, options: dict, emit=None, cancelled=None) -> dict:
    options = validate_options(options)
    source, cache_dir = Path(source), Path(cache_dir)
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        if any(cache_dir.iterdir()):
            raise InspectionError('cache')
        (cache_dir / '.building').touch(exist_ok=False)
    except OSError:
        raise InspectionError('cache') from None
    encoding = bom = None
    try:
        _check_cancel(cancelled)
        initial = source.stat()
        encoding, bom, skip = _encoding(source, options['encoding'], cancelled)
        summary = {'encoding': codecs.lookup(encoding).name.replace('_', '-'), 'bom': bom,
                   'file_size': initial.st_size, 'characters': 0, 'lines': 0,
                   'findings': 0, 'kinds': {},
                   'common_whitespace': {'space': 0, 'tab': 0, 'crlf': 0, 'lf': 0, 'cr': 0}}
        pattern = _candidates(options['include_common_whitespace'], options['strict_mode'])
        decoder = codecs.getincrementaldecoder(encoding)(errors='strict')
        optimistic_utf8 = options['encoding'] == 'auto' and encoding == 'utf-8' and bom is None
        zeroes = [0, 0, 0, 0]
        rows = []
        pages = 0
        line = column = 1
        buffer = ''
        start = 0
        with source.open('rb') as stream:
            stream.seek(skip)
            completed = skip
            while True:
                _check_cancel(cancelled)
                raw = stream.read(IO_CHUNK_BYTES)
                if optimistic_utf8 and b'\x00' in raw:
                    for lane in range(4):
                        zeroes[(completed + lane) % 4] += raw[lane::4].count(0)
                    if _wide_candidates(initial.st_size, zeroes):
                        raise _WideEncodingEvidence()
                text = decoder.decode(raw, final=not raw)
                if not text.isascii() and _SURROGATES.search(text):
                    raise UnicodeError('Invalid Unicode scalar.')
                completed += len(raw)
                summary['characters'] += len(text)
                summary['common_whitespace']['space'] += text.count(' ')
                summary['common_whitespace']['tab'] += text.count('\t')
                buffer += text
                end = len(buffer) if not raw else max(start, len(buffer) - CONTEXT_RADIUS - 1)
                if raw and end > start and buffer[end - 1] == '\r':
                    end -= 1
                cursor = start
                for match in pattern.finditer(buffer, start, end):
                    _check_cancel(cancelled)
                    column += match.start() - cursor
                    token = match.group()
                    kind = _kind(token) or 'strict'
                    if kind != 'common' or options['include_common_whitespace']:
                        name = ('CARRIAGE RETURN + LINE FEED' if token == '\r\n' else unicodedata.name(token, _CONTROL_NAMES.get(ord(token), 'CONTROL CHARACTER' if kind == 'control' else 'UNASSIGNED CODE POINT' if kind == 'strict' else 'NONCHARACTER')))
                        rows.append({'line': line, 'column': column, 'codepoint': _codepoint(token),
                                     'name': name, 'kind': kind, 'label': _LABELS[kind],
                                     'before': _safe(buffer[max(0, match.start() - CONTEXT_RADIUS):match.start()]),
                                     'display': '⟦' + (_COMMON.get(token) or _codepoint(token)) + '⟧',
                                     'after': _safe(buffer[match.end():match.end() + CONTEXT_RADIUS])})
                        summary['findings'] += 1
                        summary['kinds'][kind] = summary['kinds'].get(kind, 0) + 1
                        if len(rows) == PAGE_SIZE:
                            _write_json(_page_path(cache_dir, pages), rows)
                            pages += 1
                            rows = []
                    if token in ('\r\n', '\r', '\n'):
                        summary['common_whitespace'][_COMMON[token].lower()] += 1
                        line += 1
                        column = 1
                    else:
                        column += len(token)
                    cursor = match.end()
                column += end - cursor
                # Keep preceding context without processing it twice.
                keep = max(0, end - CONTEXT_RADIUS)
                buffer, start = buffer[keep:], end - keep
                if emit is not None:
                    emit({'completed': completed, 'total': initial.st_size, 'phase': 'scanning'})
                if not raw:
                    break
        summary['lines'] = line - 1 + int(column > 1) if summary['characters'] else 0
        _assert_unchanged(source, initial)
        if rows or not pages:
            _write_json(_page_path(cache_dir, pages), rows)
            pages += 1
        _check_cancel(cancelled)
        try:
            (cache_dir / '.building').unlink()
        except OSError:
            raise InspectionError('cache') from None
        _write_json(cache_dir / 'metadata.json', {'summary': summary, 'page_size': PAGE_SIZE, 'pages': pages, 'total': summary['findings']})
        return summary
    except BaseException as error:
        shutil.rmtree(cache_dir)
        if (isinstance(error, (UnicodeDecodeError, _WideEncodingEvidence)) and options['encoding'] == 'auto'
                and encoding == 'utf-8' and bom is None):
            return _retry_scan(source, cache_dir, options, initial, emit, cancelled)
        if isinstance(error, (UnicodeError, LookupError, OSError)):
            raise InspectionError('encoding') from None
        raise


def read_page(cache_dir: Path, page: int) -> dict:
    if type(page) is not int or page < 0:
        raise InspectionError('cache')
    try:
        cache_dir = Path(cache_dir)
        with (cache_dir / 'metadata.json').open(encoding='utf-8') as stream:
            metadata = json.load(stream)
        if (not isinstance(metadata, dict) or type(metadata.get('total')) is not int
                or metadata['total'] < 0 or type(metadata.get('pages')) is not int
                or metadata.get('page_size') != PAGE_SIZE
                or metadata['pages'] != max(1, (metadata['total'] + PAGE_SIZE - 1) // PAGE_SIZE)
                or not isinstance(metadata.get('summary'), dict)
                or metadata['summary'].get('findings') != metadata['total']
                or page >= metadata['pages']):
            raise InspectionError('cache')
        with _page_path(cache_dir, page).open(encoding='utf-8') as stream:
            rows = json.load(stream)
        expected = min(PAGE_SIZE, max(0, metadata['total'] - page * PAGE_SIZE))
        if not isinstance(rows, list) or len(rows) != expected:
            raise InspectionError('cache')
        return metadata | {'page': page, 'results': rows}
    except (OSError, ValueError, TypeError, KeyError):
        raise InspectionError('cache') from None
