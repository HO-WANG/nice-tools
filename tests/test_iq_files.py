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
