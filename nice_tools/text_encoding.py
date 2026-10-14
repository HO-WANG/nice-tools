"""Strict, best-effort decoding for locally uploaded text files.

Detection samples are bounded; decoding and text validation always cover the
complete file. Short legacy encodings remain inherently ambiguous, so the
defaults favor Korean intranet text, Japanese kana, and Western Windows text.
"""

import codecs
import re

from charset_normalizer import from_bytes


_ERROR = "인코딩을 감지할 수 없거나 올바른 텍스트 파일로 읽을 수 없습니다."
_BOMS = (
    (codecs.BOM_UTF32_LE, "utf-32-le"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
)
_BAD_TEXT = re.compile(
    "[\x00-\x08\x0b\x0e-\x1f\x7f-\x9f\ud800-\udfff]"
)
_INVALID_UNICODE = re.compile("[\x00\ud800-\udfff]")
_PRIVATE_USE = re.compile(
    "[\ue000-\uf8ff\U000f0000-\U000ffffd\U00100000-\U0010fffd]"
)
_BAD_BYTES = re.compile(b"[\x00-\x08\x0b\x0e-\x1f\x7f]")
_NON_ASCII_BYTES = re.compile(b"[\x80-\xff]")
_NON_ASCII_TEXT = re.compile("[^\x00-\x7f]")
_UNICODE_ENCODINGS = [
    "utf_16", "utf_16_le", "utf_16_be", "utf_32", "utf_32_le", "utf_32_be", "utf_7",
]
_SAMPLE_CHARS = 4096


def _validate(text: str, *, legacy: bool = False) -> str:
    # Keep valid, explicitly identified Unicode logs and text separators.
    # Stronger controls/PUA screening applies only to uncertain legacy guesses.
    if (_INVALID_UNICODE.search(text)
            or (legacy and (_BAD_TEXT.search(text) or _PRIVATE_USE.search(text)))):
        raise ValueError(_ERROR)
    return text


def _strict(content: bytes, encoding: str, *, legacy: bool = False) -> str | None:
    try:
        return _validate(content.decode(encoding, errors="strict"), legacy=legacy)
    except (UnicodeError, ValueError):
        return None


def _bomless_unicode(content: bytes) -> str | None:
    # Alignment is evidence for Unicode, not permission to reinterpret every
    # even-length binary/legacy file as UTF-16.
    if len(content) % 4 == 0 and content:
        lanes = [content[offset::4] for offset in range(4)]
        for encoding, high_lanes in (("utf-32-le", (2, 3)), ("utf-32-be", (0, 1))):
            if all(lanes[lane].count(0) / len(lanes[lane]) >= 0.75 for lane in high_lanes):
                text = _strict(content, encoding)
                if text is not None and text:
                    return text
    if len(content) % 2 == 0 and content:
        even, odd = content[::2], content[1::2]
        even_zero, odd_zero = even.count(0) / len(even), odd.count(0) / len(odd)
        for encoding, high_zero, low_zero in (
            ("utf-16-le", odd_zero, even_zero),
            ("utf-16-be", even_zero, odd_zero),
        ):
            if high_zero >= 0.30 and low_zero < 0.10:
                text = _strict(content, encoding)
                if text is not None and text:
                    return text
    return None


def _text_sample(text: str) -> str:
    if len(text) <= _SAMPLE_CHARS * 4:
        return text
    signal = _NON_ASCII_TEXT.search(text)
    starts = {0, max(0, len(text) - _SAMPLE_CHARS), len(text) // 2}
    if signal:
        starts.add(max(0, signal.start() - _SAMPLE_CHARS // 2))
    return "\n".join(text[start:start + _SAMPLE_CHARS] for start in sorted(starts))


def _script_counts(text: str) -> tuple[int, int]:
    sample = _text_sample(text)
    hangul = sum("\uac00" <= char <= "\ud7a3" for char in sample)
    kana = sum("\u3041" <= char <= "\u30fa" for char in sample)
    return hangul, kana


def _korean_text(text: str, hangul: int) -> bool:
    letters = sum(char.isalpha() and not char.isascii() for char in _text_sample(text))
    return bool(hangul and hangul >= letters * 0.80)


def _western_text(text: str) -> bool:
    sample = _text_sample(text)
    letters = [char for char in sample if char.isalpha()]
    ascii_letters = sum(char.isascii() for char in letters)
    return ascii_letters >= 2 and ascii_letters >= len(letters) / 2


def _western_words(text: str) -> bool:
    # The detector may stop after a multibyte match before testing CP1252.
    # Prefer common accented Latin words even then, but assess the words rather
    # than ASCII config keys that can surround an otherwise Chinese value.
    sample = _text_sample(text)
    if any(not char.isascii() and not char.isspace()
           and not (char.isalpha() and "\u00c0" <= char <= "\u024f")
           for char in sample):
        return False
    accented_word = False
    for word in re.findall(r"[A-Za-z\u00c0-\u00d6\u00d8-\u00f6\u00f8-\u024f]+", sample):
        accents = [index for index, char in enumerate(word) if not char.isascii()]
        if not accents:
            continue
        accented_word = True
        if (len(word) - len(accents) < 2 * len(accents)
                or any(right - left < 3 for left, right in zip(accents, accents[1:]))):
            return False
    return accented_word


def _detection_samples(content: bytes) -> list[bytes]:
    if len(content) <= _SAMPLE_CHARS * 4:
        return [content]
    signal = _NON_ASCII_BYTES.search(content)
    starts = {0, max(0, len(content) - _SAMPLE_CHARS), len(content) // 2}
    if signal:
        starts.add(max(0, signal.start() - _SAMPLE_CHARS // 2))
    samples = []
    for start in sorted(starts):
        end = min(len(content), start + _SAMPLE_CHARS)
        # Starting after a newline avoids splitting multibyte characters in
        # ordinary line-oriented files. Full candidate decoding remains strict.
        if start:
            newline = content.find(b"\n", start, min(end, start + 256))
            if newline >= 0:
                start = newline + 1
        sample = content[start:end]
        if _NON_ASCII_BYTES.search(sample):
            samples.append(sample)
    return samples


def decode_text(content: bytes) -> str:
    """Return complete decoded text, or a sanitized error without lossy fallback."""
    for bom, encoding in _BOMS:
        if content.startswith(bom):
            text = _strict(content[len(bom):], encoding)
            if text is None:
                raise ValueError(_ERROR)
            return text

    if b"\x00" in content:
        text = _bomless_unicode(content)
        if text is None:
            raise ValueError(_ERROR)
        return text

    try:
        return _validate(content.decode("utf-8", errors="strict"))
    except UnicodeDecodeError:
        pass
    if _BAD_BYTES.search(content):
        raise ValueError(_ERROR)

    korean = _strict(content, "cp949", legacy=True)
    japanese = _strict(content, "cp932", legacy=True)
    hangul = _script_counts(korean)[0] if korean is not None else 0
    kana = _script_counts(japanese)[1] if japanese is not None else 0
    if kana and (not hangul or kana >= hangul):
        return japanese
    if korean is not None and _korean_text(korean, hangul):
        return korean

    western = _strict(content, "cp1252", legacy=True)
    if western is not None and _western_text(western) and _western_words(western):
        return western

    ranked = {}
    for sample in _detection_samples(content):
        for match in from_bytes(
            sample, cp_exclusion=_UNICODE_ENCODINGS,
            preemptive_behaviour=False, enable_fallback=False,
        ):
            score = (match.chaos, -match.coherence)
            # Equivalent decodings are merged into one match by the detector;
            # retain those names so the Western preference also covers café.
            for encoding in match.could_be_from_charset:
                if encoding not in ranked or score < ranked[encoding]:
                    ranked[encoding] = score

    if western is not None and _western_text(western) and ranked.get("cp1252", (1,))[0] < 0.10:
        return western
    for encoding in sorted(ranked, key=ranked.get):
        text = _strict(content, encoding, legacy=True)
        if text is not None:
            letters = sum(char.isalpha() for char in _text_sample(text))
            # Two arbitrary high bytes can look like letters in many unrelated
            # codecs. Require more text or measured language evidence outside
            # the explicitly supported Korean/Japanese/Western defaults.
            if letters >= 8 or (letters >= 2 and ranked[encoding][1] <= -0.10):
                return text
    raise ValueError(_ERROR)
