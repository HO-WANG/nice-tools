"""Stream IQ cursor results into strict character-mode BCP text.

The caller owns a temporary binary output and publishes it only after success.
No CSV quoting is available: ambiguous cells are rejected instead of altered.
"""

import math
from datetime import date, datetime, time
from decimal import Decimal

from .ase_files import parse_options as _bcp_options


_FETCH_ROWS = 1000


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
    def fail():
        raise ValueError(f'Cannot export row {row_index}, column {column_index}.')

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
            fail()
    elif isinstance(value, float):
        if not math.isfinite(value):
            fail()
        text = repr(value)
    elif isinstance(value, Decimal):
        if not value.is_finite():
            fail()
        text = str(value)
    elif isinstance(value, (datetime, date, time)):
        text = value.isoformat()
    elif isinstance(value, (bytes, bytearray, memoryview)):
        text = '0x' + bytes(value).hex()
    else:
        fail()
    try:
        data = text.encode(encoding, errors='strict')
    except UnicodeError:
        fail()
    if b'\0' in data or field in data or row in data:
        fail()
    if len(terminator) > 1:
        suffix = data[-(len(terminator) - 1):]
        if (suffix + terminator).find(terminator) < len(suffix):
            fail()
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
        raise ValueError('The query did not return a result set.')
    columns = []
    for column in description:
        if not isinstance(column, (list, tuple)) or not column or not isinstance(column[0], str):
            raise ValueError('The result column metadata is invalid.')
        columns.append(column[0])
    rows = 0
    bytes_written = 0

    def check_cancelled():
        if cancelled is not None and cancelled():
            raise ExportCancelled('Export cancelled.')

    def write_row(values, row_index):
        nonlocal bytes_written
        check_cancelled()
        if isinstance(values, (str, bytes, bytearray, memoryview, dict, set)):
            raise ValueError(f'Invalid result row {row_index}.')
        try:
            count = len(values)
        except (TypeError, AttributeError):
            raise ValueError(f'Invalid result row {row_index}.') from None
        if count != len(columns):
            raise ValueError(f'Invalid result row {row_index}.')
        data = field.join(_cell(value, encoding, field, row,
                                row if index == len(columns) else field, row_index, index)
                          for index, value in enumerate(values, 1)) + row
        check_cancelled()
        remaining = memoryview(data)
        while remaining:
            written = output.write(remaining)
            if type(written) is not int or written <= 0 or written > len(remaining):
                raise ValueError('The export output could not be written.')
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
        batch = cursor.fetchmany(_FETCH_ROWS)
        check_cancelled()
        if not isinstance(batch, (list, tuple)):
            raise ValueError('The result batch is invalid.')
        if not batch:
            break
        for values in batch:
            write_row(values, rows + 1)
            rows += 1
        progress()
    check_cancelled()
    progress()
    return {'rows': rows, 'bytes_written': bytes_written, 'columns': columns}
