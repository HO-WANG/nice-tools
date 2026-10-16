import importlib
import io
from datetime import date, datetime, time
from decimal import Decimal

import pytest


def test_export_module_is_available():
    try:
        importlib.import_module('nice_tools.iq_files')
    except ModuleNotFoundError:
        pytest.fail('IQ character-file export is not implemented.')


@pytest.fixture
def iq_files():
    try:
        return importlib.import_module('nice_tools.iq_files')
    except ModuleNotFoundError:
        pytest.fail('IQ character-file export is not implemented.')


class Cursor:
    def __init__(self, rows=(), columns=('value',)):
        self.description = [(name, None, None, None, None, None, True) for name in columns]
        self.rows = iter(rows)
        self.fetch_sizes = []

    def fetchmany(self, size):
        self.fetch_sizes.append(size)
        result = []
        for _ in range(size):
            try:
                result.append(next(self.rows))
            except StopIteration:
                break
        return result

    def fetchall(self):
        pytest.fail('Export must stream through fetchmany.')


def export(iq_files, rows=(), columns=('value',), **options):
    output = io.BytesIO()
    result = iq_files.write_result(Cursor(rows, columns), output, iq_files.parse_options(options))
    return output.getvalue(), result


def test_options_default_and_canonical_roundtrip(iq_files):
    expected = {'encoding': 'euc-kr', 'field_hex': '7c', 'row_hex': '0a', 'include_header': False}
    assert iq_files.parse_options({}) == expected
    assert iq_files.parse_options(expected) == expected
    assert iq_files.parse_options({'encoding': 'cp949', 'field_terminator': r'\t',
                                   'row_terminator': r'\r\n', 'include_header': True}) == {
        'encoding': 'cp949', 'field_hex': '09', 'row_hex': '0d0a', 'include_header': True,
    }


@pytest.mark.parametrize('options', [
    None, [], {'encoding': []}, {'encoding': 'utf-16'}, {'include_header': 1},
    {'include_header': 'false'}, {'field_terminator': ''}, {'row_terminator': ''},
    {'field_terminator': '\0'}, {'field_terminator': r'\x09'}, {'row_hex': 'zz'},
    {'row_hex': '00'}, {'row_hex': '7c'}, {'field_hex': '09', 'field_terminator': '|'},
    {'field_terminator': '😀'}, {'field_terminator': 'x' * 33},
])
def test_invalid_options_fail(iq_files, options):
    with pytest.raises(ValueError):
        iq_files.parse_options(options)


def test_defaults_preserve_korean_trailing_spaces_null_and_empty(iq_files):
    data, result = export(iq_files, [(None, '', '한국어  '), ('서울', '부산', '끝')], ('a', 'b', 'c'))
    assert data == '|\0|한국어  \n서울|부산|끝\n'.encode('euc-kr')
    assert result == {'rows': 2, 'bytes_written': len(data), 'columns': ['a', 'b', 'c']}


def test_values_preserve_exact_numeric_binary_and_iso_dates(iq_files):
    values = (True, False, 12345678901234567890, 1.25, Decimal('12345678901234567890.1234500'),
              date(2026, 10, 16), time(1, 2, 3, 456789), datetime(2026, 10, 16, 1, 2, 3, 456789),
              b'\x00\xff', bytearray(b'ab'), memoryview(b'cd'))
    data, _ = export(iq_files, [values], tuple('c' + str(i) for i in range(len(values))))
    assert data == (b'1|0|12345678901234567890|1.25|12345678901234567890.1234500|'
                    b'2026-10-16|01:02:03.456789|2026-10-16T01:02:03.456789|0x00ff|0x6162|0x6364\n')


@pytest.mark.parametrize('encoding', ['euc-kr', 'cp949', 'utf-8'])
def test_selected_encoding_headers_and_custom_terminators(iq_files, encoding):
    data, result = export(iq_files, [('한국', '값')], ('첫째', '둘째'), encoding=encoding,
                          field_terminator=r'\t', row_terminator=r'\r\n', include_header=True)
    assert data == '첫째\t둘째\r\n한국\t값\r\n'.encode(encoding)
    assert result['rows'] == 1 and result['bytes_written'] == len(data)


def test_empty_result_with_and_without_header(iq_files):
    assert export(iq_files)[0] == b''
    data, result = export(iq_files, columns=('a', 'b'), include_header=True)
    assert data == b'a|b\n' and result['rows'] == 0


@pytest.mark.parametrize('value', [
    'private|value', 'private\nvalue', 'private\0value', '😀private',
    float('nan'), float('inf'), float('-inf'), Decimal('NaN'), Decimal('Infinity'),
    object(), {'private': 1}, ['private'],
])
def test_invalid_cell_fails_without_value_or_partial_row(iq_files, value):
    output = io.BytesIO()
    with pytest.raises(ValueError) as error:
        iq_files.write_result(Cursor([('safe', value)], ('a', 'b')), output, iq_files.parse_options({}))
    assert 'private' not in str(error.value)
    assert '1' in str(error.value) and '2' in str(error.value)
    assert output.getvalue() == b''


@pytest.mark.parametrize('name', ['private|column', 'private\ncolumn', 'private\0column', '😀private'])
def test_invalid_header_is_validated_before_output(iq_files, name):
    output = io.BytesIO()
    with pytest.raises(ValueError) as error:
        iq_files.write_result(Cursor([], ('good', name)), output,
                              iq_files.parse_options({'include_header': True}))
    assert 'private' not in str(error.value) and output.getvalue() == b''


def test_rejects_embedded_whole_multibyte_terminators(iq_files):
    with pytest.raises(ValueError):
        export(iq_files, [('secret<>value',)], field_terminator='<>')
    assert export(iq_files, [('left<right',)], field_terminator='<>')[0] == b'left<right\n'


def test_rejects_terminator_overlap_at_cell_boundary(iq_files):
    with pytest.raises(ValueError):
        export(iq_files, [('secret|', 'next')], ('a', 'b'), field_terminator='||')
    with pytest.raises(ValueError):
        export(iq_files, [('secretab',)], row_terminator='abab')


def test_large_multibatch_result_has_last_row_and_numeric_only_progress(iq_files):
    cursor = Cursor(((i, '가' * 1100) for i in range(5101)), ('id', 'text'))
    output, progress = io.BytesIO(), []
    result = iq_files.write_result(cursor, output, iq_files.parse_options({}), emit=progress.append)
    assert result['rows'] == 5101 and result['bytes_written'] == len(output.getvalue())
    assert output.getvalue().endswith(b'5100|' + ('가' * 1100).encode('euc-kr') + b'\n')
    assert len(output.getvalue()) > 10 * 1024 * 1024
    assert len(cursor.fetch_sizes) > 2 and all(1 <= size <= 1000 for size in cursor.fetch_sizes)
    assert progress[-1] == {'rows': 5101, 'bytes_written': len(output.getvalue())}
    assert all(type(value) is int for event in progress for value in event.values())


def test_invalid_final_row_is_not_skipped(iq_files):
    with pytest.raises(ValueError, match='2502'):
        export(iq_files, [(str(i),) for i in range(2501)] + [('private|bad',)])


def test_cancellation_before_fetch_and_during_rows(iq_files):
    cursor = Cursor([('value',)])
    with pytest.raises(iq_files.ExportCancelled):
        iq_files.write_result(cursor, io.BytesIO(), {}, cancelled=lambda: True)
    assert cursor.fetch_sizes == []
    output = io.BytesIO()
    with pytest.raises(iq_files.ExportCancelled):
        iq_files.write_result(Cursor(((i,) for i in range(2000))), output, {},
                              cancelled=lambda: len(output.getvalue()) > 10)
    assert 10 < len(output.getvalue()) < 100


@pytest.mark.parametrize('rows,description', [
    ([], None), ([], []), ([], [('a',), (None,)]),
    ([('a', 'b')], [('a',)]), (['abc'], [('a',)]), ([None], [('a',)]),
])
def test_malformed_result_sets_fail(iq_files, rows, description):
    cursor = Cursor(rows)
    cursor.description = description
    with pytest.raises(ValueError):
        iq_files.write_result(cursor, io.BytesIO(), {})


@pytest.mark.parametrize('value,options,reason', [
    ('😀private-value', {}, 'encoding'),
    ('private-value|', {}, 'field-terminator'),
    ('private-value\n', {}, 'row-terminator'),
    ('private-value\0', {}, 'nul'),
    ('private-valueab', {'row_terminator': 'abab'}, 'terminator-overlap'),
    (object(), {}, 'unsupported-type'),
    (float('nan'), {}, 'non-finite-number'),
    (Decimal('Infinity'), {}, 'non-finite-number'),
])
def test_typed_cell_reason_and_safe_position(iq_files, value, options, reason):
    assert hasattr(iq_files, 'ExportError'), 'Missing typed, safe IQ export diagnostics'
    output = io.BytesIO()
    with pytest.raises(iq_files.ExportError) as caught:
        iq_files.write_result(Cursor([('ok', 'ok'), ('ok', value)],
                                     ('private-first-column', 'private-second-column')),
                              output, options)
    error = caught.value
    assert (error.reason, error.row, error.column, error.encoding) == (reason, 2, 2, 'euc-kr')
    assert reason in str(error) and 'row 2' in str(error) and 'column 2' in str(error)
    assert 'private' not in str(error)
    assert output.getvalue() == b'ok|ok' + options.get('row_terminator', '\n').encode('euc-kr')


def test_integer_conversion_has_typed_safe_reason(iq_files):
    class Integer(int):
        def __str__(self):
            raise ValueError('private-conversion-detail')

    with pytest.raises(iq_files.ExportError) as caught:
        export(iq_files, [(Integer(1),)])
    assert caught.value.reason == 'integer-conversion'
    assert (caught.value.row, caught.value.column) == (1, 1)
    assert 'private' not in str(caught.value)


def test_header_error_has_row_zero_without_column_name(iq_files):
    with pytest.raises(iq_files.ExportError) as caught:
        export(iq_files, columns=('private😀header',), include_header=True)
    assert (caught.value.reason, caught.value.row, caught.value.column) == ('encoding', 0, 1)
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('description,reason,column', [
    (None, 'metadata', None), ([], 'metadata', None),
    ([('valid',), (None,)], 'metadata', 2),
])
def test_metadata_error_is_typed_and_safe(iq_files, description, reason, column):
    cursor = Cursor()
    cursor.description = description
    with pytest.raises(iq_files.ExportError) as caught:
        iq_files.write_result(cursor, io.BytesIO(), {})
    assert (caught.value.reason, caught.value.column) == (reason, column)
    assert caught.value.row is None


@pytest.mark.parametrize('value', [None, 'private-row', ('a', 'b'), {'private-key': 'private-value'}])
def test_row_shape_error_has_safe_row_position(iq_files, value):
    with pytest.raises(iq_files.ExportError) as caught:
        export(iq_files, [value])
    assert (caught.value.reason, caught.value.row, caught.value.column) == ('row-shape', 1, None)
    assert 'private' not in str(caught.value)


def test_batch_shape_error_is_typed(iq_files):
    cursor = Cursor()
    cursor.fetchmany = lambda size: 'private-batch'
    with pytest.raises(iq_files.ExportError) as caught:
        iq_files.write_result(cursor, io.BytesIO(), {})
    assert caught.value.reason == 'batch-shape'
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('written', [None, True, 0, -1, 1000, 'private-write-result'])
def test_invalid_output_count_is_typed(iq_files, written):
    class Output:
        def write(self, data):
            return written

    with pytest.raises(iq_files.ExportError) as caught:
        iq_files.write_result(Cursor([('private-value',)]), Output(), {})
    assert caught.value.reason == 'output-write'
    assert caught.value.row == 1
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('os_errno,winerror', [(28, None), (13, None), (None, 112), (None, 5)])
@pytest.mark.parametrize('operation', ['write', 'flush'])
def test_disk_and_permission_errors_keep_only_safe_numeric_context(iq_files, os_errno, winerror, operation):
    original = OSError(os_errno, 'private-native-detail', '/private/output-path')
    if winerror is not None:
        original.winerror = winerror

    class Output(io.BytesIO):
        def write(self, data):
            if operation == 'write':
                raise original
            return super().write(data)

        def flush(self):
            if operation == 'flush':
                raise original
            return super().flush()

    with pytest.raises(iq_files.ExportError) as caught:
        iq_files.write_result(Cursor([('private-value',)]), Output(), {})
    assert caught.value.reason == 'output-io'
    assert (caught.value.os_errno, caught.value.winerror) == (os_errno, winerror)
    assert 'private' not in str(caught.value)
    for name, value in [('errno', os_errno), ('winerror', winerror)]:
        if value is not None:
            assert f'{name}={value}' in str(caught.value)


def test_output_oserror_untrusted_attributes_are_not_formatted(iq_files):
    original = OSError('private-native-detail')
    original.errno, original.winerror = 'private-errno', True

    class Output:
        def write(self, data):
            raise original

    with pytest.raises(iq_files.ExportError) as caught:
        iq_files.write_result(Cursor([('private-value',)]), Output(), {})
    assert caught.value.os_errno is None and caught.value.winerror is None
    assert 'private' not in str(caught.value)


def test_fetch_failure_has_fixed_safe_message_and_original_cause(iq_files):
    class NativeFailure(Exception):
        def __str__(self):
            pytest.fail('Native fetch error must never be converted to text')

    original = NativeFailure('HY001', 'private-SQL-password-or-value')
    cursor = Cursor()

    def fetchmany(size):
        raise original

    cursor.fetchmany = fetchmany
    with pytest.raises(iq_files.FetchError) as caught:
        iq_files.write_result(cursor, io.BytesIO(), {})
    assert caught.value.__cause__ is original
    assert 'private' not in str(caught.value)
    assert 'fetch' in str(caught.value).lower()


@pytest.mark.parametrize('kwargs', [
    {'reason': 'private-unknown-reason'}, {'reason': []},
    {'row': True}, {'row': -1}, {'row': 'private-row'}, {'row': 1.5},
    {'column': 0}, {'column': 'private-column'}, {'column': True},
    {'encoding': 'private-encoding'}, {'encoding': []},
    {'os_errno': 'private-errno'}, {'os_errno': -1}, {'os_errno': True},
    {'winerror': 'private-winerror'}, {'winerror': -1}, {'winerror': True},
])
def test_export_error_rejects_untrusted_context_without_echo(iq_files, kwargs):
    with pytest.raises(ValueError) as caught:
        iq_files.ExportError(**({'reason': 'encoding'} | kwargs))
    assert 'private' not in str(caught.value)


def test_partial_output_writes_and_flush_still_report_complete_result(iq_files):
    class Output(io.BytesIO):
        flushed = False

        def write(self, data):
            return super().write(data[:2])

        def flush(self):
            self.flushed = True
            return super().flush()

    output = Output()
    result = iq_files.write_result(Cursor([('한국',), ('value',)]), output, {})
    assert output.flushed
    assert output.getvalue() == '한국\nvalue\n'.encode('euc-kr')
    assert result['rows'] == 2 and result['bytes_written'] == len(output.getvalue())


def test_row_shape_checks_actual_items_before_writing(iq_files):
    class InvalidRow:
        def __len__(self):
            return 1

        def __iter__(self):
            return iter(('private-first-value', 'private-extra-value'))

    output = io.BytesIO()
    with pytest.raises(iq_files.ExportError) as caught:
        iq_files.write_result(Cursor([InvalidRow()]), output, {})
    assert (caught.value.reason, caught.value.row) == ('row-shape', 1)
    assert output.getvalue() == b''
    assert 'private' not in str(caught.value)


@pytest.mark.parametrize('os_errno,winerror,english,korean', [
    (28, None, 'Disk full', '디스크 공간'),
    (None, 112, 'Disk full', '디스크 공간'),
    (13, None, 'Permission denied', '쓰기 권한'),
    (None, 5, 'Permission denied', '쓰기 권한'),
    (30, None, 'Read-only', '읽기 전용'),
    (None, 19, 'Read-only', '읽기 전용'),
    (None, 32, 'Sharing violation', '다른 프로그램'),
])
def test_known_output_io_codes_have_safe_human_meaning(iq_files, os_errno, winerror, english, korean):
    error = iq_files.ExportError('output-io', os_errno=os_errno, winerror=winerror)
    message = str(error)
    assert english in message and korean in message
    assert 'reason=output-io' in message
    if os_errno is not None:
        assert f'errno={os_errno}' in message
    if winerror is not None:
        assert f'winerror={winerror}' in message


def test_unknown_output_io_codes_keep_generic_message_and_codes(iq_files):
    error = iq_files.ExportError('output-io', os_errno=12345, winerror=67890)
    message = str(error)
    assert 'Output file write or flush failed' in message
    assert 'errno=12345' in message and 'winerror=67890' in message
    assert all(text not in message for text in ('Disk full', 'Permission denied', 'Read-only', 'Sharing violation'))


def test_io_human_meaning_does_not_override_other_export_reasons(iq_files):
    error = iq_files.ExportError('encoding', encoding='euc-kr', os_errno=28)
    assert 'Value cannot be encoded' in str(error)
    assert 'Disk full' not in str(error)
