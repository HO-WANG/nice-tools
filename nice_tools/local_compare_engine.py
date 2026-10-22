"""Streaming text comparison for native, locally selected files.

Every decoder validates the complete source strictly. Detection alone uses
bounded samples. Normalization memory is bounded by the largest decoded line;
external sorting adds a bounded chunk and one line per merge input. There are
no byte or line-size limits, so a single very large line still needs memory
proportional to that line. Sources are never changed.
"""

import codecs
from contextlib import ExitStack, closing
import heapq
import os
from pathlib import Path
import re
import sys
import tempfile

from .comparison import CompareOptions
from . import text_encoding as detection


IO_CHUNK_BYTES = 128 * 1024
SORT_CHUNK_BYTES = 8 * 1024 * 1024
MERGE_FAN_IN = 32
_SAMPLE = detection._SAMPLE_CHARS
_WHITESPACE = re.compile(r"\s+")


class CompareCancelled(ValueError):
    """An owned comparison was cancelled without publishing partial output."""


def _check_cancel(cancelled):
    if cancelled is not None and cancelled():
        raise CompareCancelled("파일 비교가 취소되었습니다. (cancelled)")


def _path(value):
    if not isinstance(value, Path):
        raise ValueError("파일 경로가 올바르지 않습니다.")
    return value


def _options(value):
    if not isinstance(value, CompareOptions):
        raise ValueError("파일 비교 옵션이 올바르지 않습니다.")


class _TextSample:
    """Retain head/middle/tail and the first non-ASCII signal, never full files."""

    def __init__(self, size):
        self.size = size
        self.small = ""
        self.head = ""
        self.middle = ""
        self.tail = ""
        self.signal = ""

    def add(self, text, byte_position):
        if self.small is not None:
            if len(self.small) + len(text) <= _SAMPLE * 4:
                self.small += text
            else:
                self.small = None
        if len(self.head) < _SAMPLE:
            self.head += text[:_SAMPLE - len(self.head)]
        if not self.middle and byte_position >= self.size // 2:
            self.middle = text[:_SAMPLE]
        if not self.signal:
            match = detection._NON_ASCII_TEXT.search(text)
            if match:
                start = max(0, match.start() - _SAMPLE // 2)
                self.signal = (self.tail + text)[max(0, len(self.tail) + start):][: _SAMPLE]
        self.tail = (self.tail + text)[-_SAMPLE:]

    def text(self):
        if self.small is not None:
            return self.small
        return "\n".join((self.head, self.middle, self.signal, self.tail))


def _probe(path, encoding, cancelled, *, legacy=False, collect=False):
    """Return bounded text evidence only after a complete strict decode."""
    try:
        decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
        with path.open("rb") as source:
            sample = _TextSample(os.fstat(source.fileno()).st_size) if collect else None
            position = 0
            while True:
                _check_cancel(cancelled)
                raw = source.read(IO_CHUNK_BYTES)
                text = decoder.decode(raw, final=not raw)
                detection._validate(text, legacy=legacy)
                if sample is not None:
                    sample.add(text, position)
                if not raw:
                    break
                position += len(raw)
        return sample.text() if sample is not None else ""
    except CompareCancelled:
        raise
    except (UnicodeError, ValueError, LookupError):
        return None


def _byte_evidence(path, cancelled):
    """Measure Unicode alignment and locate bounded legacy detection samples."""
    with path.open("rb") as source:
        size = os.fstat(source.fileno()).st_size
        position = 0
        first_signal = None
        zeroes = [0, 0, 0, 0]
        bad_bytes = False
        while True:
            _check_cancel(cancelled)
            raw = source.read(IO_CHUNK_BYTES)
            if not raw:
                break
            for lane in range(4):
                zeroes[(position + lane) % 4] += raw[lane::4].count(0)
            bad_bytes = bad_bytes or bool(detection._BAD_BYTES.search(raw))
            if first_signal is None:
                signal = detection._NON_ASCII_BYTES.search(raw)
                if signal:
                    first_signal = position + signal.start()
            position += len(raw)
        if size <= _SAMPLE * 4:
            source.seek(0)
            samples = [source.read(_SAMPLE * 4)]
        else:
            starts = {0, max(0, size - _SAMPLE), size // 2}
            if first_signal is not None:
                starts.add(max(0, first_signal - _SAMPLE // 2))
            samples = []
            for start in sorted(starts):
                _check_cancel(cancelled)
                source.seek(start)
                sample = source.read(_SAMPLE)
                if start:
                    newline = sample.find(b"\n", 0, 256)
                    if newline >= 0:
                        sample = sample[newline + 1:]
                if detection._NON_ASCII_BYTES.search(sample):
                    samples.append(sample)
    return size, zeroes, bad_bytes, samples


def _bom_encoding(prefix):
    for bom, encoding in detection._BOMS:
        if prefix.startswith(bom):
            return "utf-8-sig" if encoding == "utf-8" else encoding.rsplit("-", 1)[0]
    return None


def validate_file(path: Path, cancelled=None) -> str:
    """Detect an encoding and strictly validate the entire text file."""
    path = _path(path)
    _check_cancel(cancelled)
    with path.open("rb") as source:
        prefix = source.read(4)
    encoding = _bom_encoding(prefix)
    if encoding is not None:
        if _probe(path, encoding, cancelled) is None:
            raise ValueError(detection._ERROR)
        return encoding
    if _probe(path, "utf-8", cancelled) is not None:
        return "utf-8"

    size, zeroes, bad_bytes, samples = _byte_evidence(path, cancelled)
    if any(zeroes):
        candidates = []
        if size and size % 4 == 0:
            for encoding, lanes in (("utf-32-le", (2, 3)), ("utf-32-be", (0, 1))):
                if all(zeroes[lane] / (size // 4) >= 0.75 for lane in lanes):
                    candidates.append(encoding)
        if size and size % 2 == 0:
            even_zero = (zeroes[0] + zeroes[2]) / (size // 2)
            odd_zero = (zeroes[1] + zeroes[3]) / (size // 2)
            for encoding, high, low in (("utf-16-le", odd_zero, even_zero), ("utf-16-be", even_zero, odd_zero)):
                if high >= 0.30 and low < 0.10:
                    candidates.append(encoding)
        for encoding in candidates:
            if _probe(path, encoding, cancelled) is not None:
                return encoding
        raise ValueError(detection._ERROR)
    if bad_bytes:
        raise ValueError(detection._ERROR)

    probed = {}
    def probe(encoding):
        if encoding not in probed:
            probed[encoding] = _probe(path, encoding, cancelled, legacy=True, collect=True)
        return probed[encoding]

    korean, japanese = probe("cp949"), probe("cp932")
    hangul = detection._script_counts(korean)[0] if korean is not None else 0
    kana = detection._script_counts(japanese)[1] if japanese is not None else 0
    if kana and (not hangul or kana >= hangul):
        return "cp932"
    if korean is not None and detection._korean_text(korean, hangul):
        return "cp949"
    western = probe("cp1252")
    if western is not None and detection._western_text(western) and detection._western_words(western):
        return "cp1252"

    ranked = {}
    for sample in samples:
        _check_cancel(cancelled)
        for match in detection.from_bytes(sample, cp_exclusion=detection._UNICODE_ENCODINGS,
                                         preemptive_behaviour=False, enable_fallback=False):
            score = (match.chaos, -match.coherence)
            for encoding in match.could_be_from_charset:
                if encoding not in ranked or score < ranked[encoding]:
                    ranked[encoding] = score
    if western is not None and detection._western_text(western) and ranked.get("cp1252", (1,))[0] < 0.10:
        return "cp1252"
    for encoding in sorted(ranked, key=ranked.get):
        text = probe(encoding)
        if text is not None:
            letters = sum(char.isalpha() for char in detection._text_sample(text))
            if letters >= 8 or (letters >= 2 and ranked[encoding][1] <= -0.10):
                return encoding
    raise ValueError(detection._ERROR)


def files_equal(left: Path, right: Path, cancelled=None) -> bool:
    """Compare complete bytes with bounded reads and no hash collisions."""
    left, right = _path(left), _path(right)
    _check_cancel(cancelled)
    with left.open("rb") as a, right.open("rb") as b:
        if os.fstat(a.fileno()).st_size != os.fstat(b.fileno()).st_size:
            return False
        while True:
            _check_cancel(cancelled)
            chunk = a.read(IO_CHUNK_BYTES)
            if chunk != b.read(IO_CHUNK_BYTES):
                return False
            if not chunk:
                return True


def _decoded_lines(source, encoding, cancelled):
    """Validate decoder chunks once and split only CRLF/CR/LF records."""
    legacy = not codecs.lookup(encoding).name.startswith(("utf-", "utf_"))
    decoder = codecs.getincrementaldecoder(encoding)(errors="strict")
    parts = []
    skip_lf = False
    with source.open("rb") as stream:
        while True:
            _check_cancel(cancelled)
            raw = stream.read(IO_CHUNK_BYTES)
            text = decoder.decode(raw, final=not raw)
            detection._validate(text, legacy=legacy)
            if text:
                if skip_lf and text.startswith("\n"):
                    text = text[1:]
                skip_lf = text.endswith("\r")
                records = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
                if len(records) == 1:
                    if records[0]:
                        parts.append(records[0])
                else:
                    yield "".join(parts) + records[0]
                    parts = []
                    yield from records[1:-1]
                    if records[-1]:
                        parts.append(records[-1])
            if not raw:
                break
    if parts:
        yield "".join(parts)
    _check_cancel(cancelled)


def _normalized_lines(source, options, encoding, cancelled):
    with closing(_decoded_lines(source, encoding, cancelled)) as lines:
        if not options.ignore_start and not options.ignore_end and not options.ignore_whitespace:
            yield from lines
            return
        for line in lines:
            _check_cancel(cancelled)
            stop = max(0, len(line) - options.ignore_end)
            line = line[options.ignore_start:stop]
            if options.ignore_whitespace:
                line = _WHITESPACE.sub("", line)
            yield line


def _write_run(lines, path, cancelled):
    _check_cancel(cancelled)
    lines.sort()
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        for line in lines:
            _check_cancel(cancelled)
            stream.write(line)
            stream.write("\n")


def _merge_runs(runs, destination, cancelled):
    with ExitStack() as stack:
        streams = [stack.enter_context(path.open("r", encoding="utf-8", newline="\n")) for path in runs]
        output = stack.enter_context(destination.open("w", encoding="utf-8", newline="\n"))
        # The record separator must not participate in Python's lexical order:
        # "a" precedes "a\\x01" even though '\\n' sorts after '\\x01'.
        for record in heapq.merge(*streams, key=lambda record: record[:-1]):
            _check_cancel(cancelled)
            output.write(record)


def _sort_to_file(lines, destination, cancelled):
    rows = 0
    with tempfile.TemporaryDirectory(prefix=".compare-sort-", dir=destination.parent) as folder:
        folder = Path(folder)
        runs, chunk, used = [], [], 0
        serial = 0
        for line in lines:
            chunk.append(line)
            rows += 1
            used += sys.getsizeof(line) + 16
            if used >= SORT_CHUNK_BYTES:
                run = folder / str(serial)
                serial += 1
                _write_run(chunk, run, cancelled)
                runs.append(run)
                chunk, used = [], 0
        if chunk or not runs:
            run = folder / str(serial)
            serial += 1
            _write_run(chunk, run, cancelled)
            runs.append(run)
        while len(runs) > MERGE_FAN_IN:
            merged = []
            for index in range(0, len(runs), MERGE_FAN_IN):
                group = runs[index:index + MERGE_FAN_IN]
                run = folder / str(serial)
                serial += 1
                _merge_runs(group, run, cancelled)
                for previous in group:
                    previous.unlink()
                merged.append(run)
            runs = merged
        _merge_runs(runs, destination, cancelled)
    return rows


def _write_normalized(source, stage, options, encoding, cancelled):
    with closing(_normalized_lines(source, options, encoding, cancelled)) as lines:
        if options.sort_lines:
            return _sort_to_file(lines, stage, cancelled)
        rows = 0
        with stage.open("w", encoding="utf-8", newline="\n") as output:
            for line in lines:
                output.write(line)
                output.write("\n")
                rows += 1
        return rows


def normalize_file(source: Path, destination: Path, options: CompareOptions, *, encoding=None, cancelled=None) -> dict:
    """Write UTF-8 LF records, atomically retaining only complete results.

    UTF-8 and BOM-identified Unicode validate while normalizing in one pass.
    Failed UTF-8 attempts are discarded before strict whole-file detection and
    a retry; samples never authorize skipping any source bytes.
    """
    source, destination = _path(source), _path(destination)
    _options(options)
    _check_cancel(cancelled)
    if source.resolve() == destination.resolve() or (destination.exists() and source.samefile(destination)):
        raise ValueError("원본 파일을 정규화 결과로 덮어쓸 수 없습니다.")
    fallback = False
    if encoding is None:
        with source.open("rb") as stream:
            encoding = _bom_encoding(stream.read(4))
        if encoding is None:
            encoding, fallback = "utf-8", True
    if not isinstance(encoding, str):
        raise ValueError(detection._ERROR)
    stage = None
    try:
        descriptor, name = tempfile.mkstemp(prefix=".compare-normalized-", dir=destination.parent)
        os.close(descriptor)
        stage = Path(name)
        try:
            rows = _write_normalized(source, stage, options, encoding, cancelled)
        except CompareCancelled:
            raise
        except (UnicodeError, ValueError, LookupError):
            if not fallback:
                raise
            encoding = validate_file(source, cancelled)
            rows = _write_normalized(source, stage, options, encoding, cancelled)
        _check_cancel(cancelled)
        stage.replace(destination)
        return {"encoding": encoding, "rows": rows}
    except CompareCancelled:
        raise
    except (UnicodeError, LookupError):
        raise ValueError(detection._ERROR) from None
    finally:
        if stage is not None:
            stage.unlink(missing_ok=True)


def compare_files(left: Path, right: Path, options: CompareOptions, cache_dir: Path, *, cancelled=None) -> dict:
    """Compare local text files and keep normalized files for a later diff."""
    left, right, cache_dir = _path(left), _path(right), _path(cache_dir)
    _options(options)
    raw_identical = files_equal(left, right, cancelled)
    if raw_identical:
        encoding = validate_file(left, cancelled)
        return {"identical": True, "raw_identical": True,
                "left_encoding": encoding, "right_encoding": encoding,
                "left_normalized": None, "right_normalized": None}
    cache_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    try:
        encodings = []
        for source in (left, right):
            descriptor, name = tempfile.mkstemp(prefix="normalized-", suffix=".txt", dir=cache_dir)
            os.close(descriptor)
            destination = Path(name).resolve()
            paths.append(destination)
            result = normalize_file(source, destination, options, cancelled=cancelled)
            encodings.append(result["encoding"])
        identical = files_equal(paths[0], paths[1], cancelled)
        return {"identical": identical, "raw_identical": False,
                "left_encoding": encodings[0], "right_encoding": encodings[1],
                "left_normalized": str(paths[0]), "right_normalized": str(paths[1])}
    except BaseException:
        for path in paths:
            path.unlink(missing_ok=True)
        raise
