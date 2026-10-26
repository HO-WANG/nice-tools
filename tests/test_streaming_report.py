"""Large one-sided reports must not retain every source line in memory."""
import tracemalloc

import pytest

from nice_tools.local_compare_diff import prepare_diff, read_page


@pytest.mark.parametrize('side', ['left', 'right'])
def test_one_sided_full_report_uses_bounded_input_memory(tmp_path, side):
    source = tmp_path / 'normalized'
    count = 2048
    with source.open('w', encoding='utf-8', newline='\n') as output:
        for index in range(count):
            output.write(f'{index:05d}|' + 'x' * 2048 + '\n')
    tracemalloc.start()
    try:
        metadata = prepare_diff(source if side == 'left' else None,
                                source if side == 'right' else None, tmp_path / 'report',
                                path='large.txt', old_exists=side == 'left', new_exists=side == 'right')
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 3 * 1024 * 1024, f'whole one-sided input retained: {peak} bytes'
    assert metadata['full_total_lines'] == count + 1
    assert metadata['displayed_changes'] == 1000
    assert metadata['additions' if side == 'right' else 'deletions'] == count
    last = read_page(tmp_path / 'report' / 'full', metadata['full_pages'] - 1)
    assert last['lines'][-1]['text'].startswith(f'{count - 1:05d}|')
    assert last['lines'][-1]['new_line' if side == 'right' else 'old_line'] == count
