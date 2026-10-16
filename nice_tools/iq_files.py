"""Stream IQ cursor results into strict character-mode BCP text.

The caller owns a temporary binary output and publishes it only after success.
No CSV quoting is available: ambiguous cells are rejected instead of altered.
"""

import math
from datetime import date, datetime, time
from decimal import Decimal

from .ase_files import parse_options as _bcp_options


_FETCH_ROWS = 1000
_ENCODINGS = {'euc-kr', 'cp949', 'utf-8'}
_ERROR_REASONS = {
    'encoding': 'Value cannot be encoded. 선택한 인코딩으로 값을 표현할 수 없습니다.',
    'field-terminator': 'Value contains the field terminator. 값에 필드 구분자가 포함되어 있습니다.',
    'row-terminator': 'Value contains the row terminator. 값에 행 구분자가 포함되어 있습니다.',
    'nul': 'Value contains NUL. 값에 NUL 문자가 포함되어 있습니다.',
    'terminator-overlap': 'Value overlaps a terminator at the cell boundary. 값 끝이 구분자와 겹칩니다.',
    'unsupported-type': 'Value type is unsupported. 지원하지 않는 값 형식입니다.',
    'non-finite-number': 'Number is NaN or infinity. 숫자가 NaN 또는 무한대입니다.',
    'integer-conversion': 'Integer cannot be converted to text. 정수를 문자열로 변환할 수 없습니다.',
    'metadata': 'Result column metadata is invalid. 결과 열 정보가 올바르지 않습니다.',
    'row-shape': 'Result row has an invalid structure or column count. 결과 행 구조 또는 열 수가 올바르지 않습니다.',
    'batch-shape': 'Fetched result batch is invalid. 가져온 결과 묶음이 올바르지 않습니다.',
    'output-write': 'Output did not accept the expected bytes. 출력 파일에 바이트를 기록할 수 없습니다.',
    'output-io': 'Output file write or flush failed. 출력 파일 기록 또는 버퍼 저장에 실패했습니다.',
}
_OUTPUT_IO_MESSAGES = {
    'disk-full': 'Disk full: free disk space. 디스크 공간이 부족합니다.',
    'permission': 'Permission denied: check output folder permissions. 출력 폴더의 쓰기 권한을 확인하세요.',
    'read-only': 'Read-only output: choose a writable disk. 출력 대상이 읽기 전용입니다.',
    'sharing': 'Sharing violation: close the program using the output file. 다른 프로그램이 출력 파일을 사용 중입니다.',
}
_ERRNO_REASONS = {28: 'disk-full', 13: 'permission', 30: 'read-only'}
_WINERROR_REASONS = {112: 'disk-full', 5: 'permission', 19: 'read-only', 32: 'sharing'}


def _safe_number(value, minimum=0, maximum=2 ** 63 - 1):
    return type(value) is int and minimum <= value <= maximum


class ExportError(ValueError):
    """Safe public diagnostic built exclusively from fixed reasons and context.

    Cell/header values, SQL, paths and native exception messages are never input
    to this formatter. Header positions use row 0; data rows/columns start at 1.
    """

    def __init__(self, reason, *, row=None, column=None, encoding=None,
                 os_errno=None, winerror=None):
        if type(reason) is not str or reason not in _ERROR_REASONS:
            raise ValueError('Invalid IQ export error reason.')
        if ((row is not None and not _safe_number(row))
                or (column is not None and not _safe_number(column, minimum=1))
                or (encoding is not None and
                    (type(encoding) is not str or encoding not in _ENCODINGS))
                or (os_errno is not None and not _safe_number(os_errno, maximum=2 ** 32 - 1))
                or (winerror is not None and not _safe_number(winerror, maximum=2 ** 32 - 1))):
            raise ValueError('Invalid IQ export error context.')
        self.reason, self.row, self.column = reason, row, column
        self.encoding, self.os_errno, self.winerror = encoding, os_errno, winerror
        context = [f'reason={reason}']
        if row is not None:
            context.append(f'row {row}')
        if column is not None:
            context.append(f'column {column}')
        if encoding is not None:
            context.append(f'encoding={encoding}')
        if os_errno is not None:
            context.append(f'errno={os_errno}')
        if winerror is not None:
            context.append(f'winerror={winerror}')
        detail = _ERROR_REASONS[reason]
        if reason == 'output-io':
            io_reason = _WINERROR_REASONS.get(winerror) or _ERRNO_REASONS.get(os_errno)
            if io_reason is not None:
                detail += ' ' + _OUTPUT_IO_MESSAGES[io_reason]
        super().__init__(f'IQ export failed: {detail} ({", ".join(context)})')


class FetchError(Exception):
    """Safe fetch failure; the private original exception remains its cause."""

    def __init__(self):
        super().__init__('IQ result fetch failed. IQ 결과를 가져오지 못했습니다.')


class ExportCancelled(ValueError):
    """Export stopped before its temporary output could be published."""


def parse_options(payload: dict) -> dict:
    """Return JSON-safe export options using ASE's encoding and delimiter rules."""
    if not isinstance(payload, dict):
        raise ValueError('Export settings must be an object.')
    selected = {key: value for key, value in payload.items() if key in {
        'encoding', 'field_terminator', 'row_terminator', 'field_hex', 'row_hex',
    }}
    options = _bcp_options(selected)
    include_header = payload.get('include_header', False)
    if type(include_header) is not bool:
        raise ValueError('include_header must be true or false.')
    return {key: options[key] for key in ('encoding', 'field_hex', 'row_hex')} | {
        'include_header': include_header,
    }


def _cell(value, encoding, field, row, terminator, row_index, column_index):
    def fail(reason):
        raise ExportError(reason, row=row_index, column=column_index, encoding=encoding) from None

    if value is None:
        return b''
    if isinstance(value, str):
        text = value
        if not text:
            return b'\0'
    elif isinstance(value, bool):
        text = '1' if value else '0'
    elif isinstance(value, int):
        try:
            text = str(value)
        except ValueError:
            fail('integer-conversion')
    elif isinstance(value, float):
        if not math.isfinite(value):
            fail('non-finite-number')
        text = repr(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            fail('non-finite-number')
        text = str(value)
    elif isinstance(value, (datetime, date, time)):
        text = value.isoformat()
    elif isinstance(value, (bytes, bytearray, memoryview)):
        text = '0x' + bytes(value).hex()
    else:
        fail('unsupported-type')
    try:
        data = text.encode(encoding, errors='strict')
    except UnicodeError:
        fail('encoding')
    if b'\0' in data:
        fail('nul')
    if field in data:
        fail('field-terminator')
    if row in data:
        fail('row-terminator')
    if len(terminator) > 1:
        suffix = data[-(len(terminator) - 1):]
        if (suffix + terminator).find(terminator) < len(suffix):
            fail('terminator-overlap')
    return data


def write_result(cursor, output, options, cancelled=None, emit=None) -> dict:
    """Write all rows using bounded fetchmany calls; errors never include values.

    Headers use row index 0. Row and column positions in errors are one-based.
    A failed/cancelled call can leave temporary output; callers must discard it.
    """
    options = parse_options(options)
    encoding = options['encoding']
    field, row = bytes.fromhex(options['field_hex']), bytes.fromhex(options['row_hex'])
    description = cursor.description
    if not isinstance(description, (list, tuple)) or not description:
        raise ExportError('metadata')
    columns = []
    for index, column in enumerate(description, 1):
        if not isinstance(column, (list, tuple)) or not column or not isinstance(column[0], str):
            raise ExportError('metadata', column=index)
        columns.append(column[0])
    rows = 0
    bytes_written = 0

    def check_cancelled():
        if cancelled is not None and cancelled():
            raise ExportCancelled('Export cancelled.')

    def output_error(error, row_index=None):
        os_errno, winerror = getattr(error, 'errno', None), getattr(error, 'winerror', None)
        raise ExportError('output-io', row=row_index,
                          os_errno=os_errno if _safe_number(os_errno, maximum=2 ** 32 - 1) else None,
                          winerror=winerror if _safe_number(winerror, maximum=2 ** 32 - 1) else None) from None

    def write_row(values, row_index):
        nonlocal bytes_written
        check_cancelled()
        if isinstance(values, (str, bytes, bytearray, memoryview, dict, set)):
            raise ExportError('row-shape', row=row_index)
        try:
            count = len(values)
            values = tuple(values)
        except Exception:
            raise ExportError('row-shape', row=row_index) from None
        if count != len(columns) or len(values) != len(columns):
            raise ExportError('row-shape', row=row_index)
        data = field.join(_cell(value, encoding, field, row,
                                row if index == len(columns) else field, row_index, index)
                          for index, value in enumerate(values, 1)) + row
        check_cancelled()
        remaining = memoryview(data)
        while remaining:
            try:
                written = output.write(remaining)
            except OSError as error:
                output_error(error, row_index)
            if type(written) is not int or written <= 0 or written > len(remaining):
                raise ExportError('output-write', row=row_index)
            bytes_written += written
            remaining = remaining[written:]

    def progress():
        if emit is not None:
            emit({'rows': rows, 'bytes_written': bytes_written})

    check_cancelled()
    if options['include_header']:
        write_row(columns, 0)
    while True:
        check_cancelled()
        try:
            batch = cursor.fetchmany(_FETCH_ROWS)
        except Exception as error:
            raise FetchError() from error
        check_cancelled()
        if not isinstance(batch, (list, tuple)):
            raise ExportError('batch-shape')
        if not batch:
            break
        for values in batch:
            write_row(values, rows + 1)
            rows += 1
        progress()
    check_cancelled()
    try:
        output.flush()
    except OSError as error:
        output_error(error)
    check_cancelled()
    progress()
    return {'rows': rows, 'bytes_written': bytes_written, 'columns': columns}
