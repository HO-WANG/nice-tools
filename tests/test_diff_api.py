import json
import random
import re
import time

import pytest
from starlette.testclient import TestClient

from nice_tools.main import app


def diff_request(client, left=b"one\nold\nend\n", right=b"one\nnew\nend\n", **options):
    data = {
        "left_paths": json.dumps(["sub/a.txt"] if left is not None else []),
        "right_paths": json.dumps(["sub/a.txt"] if right is not None else []),
        **options,
    }
    files = [(side + "_files", ("a.txt", value, "text/plain"))
             for side, value in (("left", left), ("right", right)) if value is not None]
    return client.post("/api/diff", data=data, files=files)


def test_diff_shows_changes_context_and_correct_line_numbers():
    with TestClient(app) as client:
        response = diff_request(client)
    assert response.status_code == 200
    diff = response.json()
    assert diff["path"] == "sub/a.txt"
    assert (diff["additions"], diff["deletions"]) == (1, 1)
    assert diff["identical"] is False
    assert diff["lines"] == [
        {"kind": "hunk", "text": "@@ -1,3 +1,3 @@", "old_line": None, "new_line": None},
        {"kind": "context", "text": "one", "old_line": 1, "new_line": 1},
        {"kind": "delete", "text": "old", "old_line": 2, "new_line": None},
        {"kind": "add", "text": "new", "old_line": None, "new_line": 2},
        {"kind": "context", "text": "end", "old_line": 3, "new_line": 3},
    ]


def test_diff_uses_the_same_four_options_as_comparison():
    with TestClient(app) as client:
        response = diff_request(client, b"Xa bY\nXcdY", b"QcdR\nQa bR",
                                sort_lines="true", ignore_whitespace="true", ignore_start="1", ignore_end="1")
    assert response.status_code == 200
    assert response.json()["identical"] is True
    assert response.json()["lines"] == []


def test_diff_decodes_legacy_and_unicode_files_before_showing_changes():
    with TestClient(app) as client:
        response = diff_request(client, "공통 줄\n이전 내용\n끝".encode("cp949"), "공통 줄\n새로운 내용\n끝".encode("utf-16"))
    assert response.status_code == 200
    changed = [line["text"] for line in response.json()["lines"] if line["kind"] in ("delete", "add")]
    assert changed == ["이전 내용", "새로운 내용"]


@pytest.mark.parametrize("side", ["left", "right"])
def test_one_sided_diff_including_empty_files(side):
    with TestClient(app) as client:
        response = diff_request(client, **{"left": b"first\nsecond" if side == "left" else None,
                                          "right": b"first\nsecond" if side == "right" else None})
        empty = diff_request(client, **{"left": b"" if side == "left" else None,
                                       "right": b"" if side == "right" else None})
    assert response.status_code == 200
    diff = response.json()
    assert diff["old_exists"] is (side == "left")
    assert diff["new_exists"] is (side == "right")
    assert [line["kind"] for line in diff["lines"]] == ["hunk", *( ["delete"] * 2 if side == "left" else ["add"] * 2)]
    assert empty.status_code == 200
    assert empty.json()["identical"] is False
    assert empty.json()["lines"] == []


def test_separate_hunks_keep_distant_line_numbers():
    left = [str(i) for i in range(30)]
    right = left.copy()
    right[1], right[28] = "changed one", "changed two"
    with TestClient(app) as client:
        response = diff_request(client, "\n".join(left).encode(), "\n".join(right).encode())
    assert response.status_code == 200
    lines = response.json()["lines"]
    assert len([line for line in lines if line["kind"] == "hunk"]) == 2
    assert [line["new_line"] for line in lines if line["kind"] == "add"] == [2, 29]


@pytest.mark.parametrize("data", [{"ignore_start": "-1"}, {"left_paths": '["../a"]'},
                                 {"right_paths": '["different.txt"]'}])
def test_diff_reuses_upload_validation(data):
    with TestClient(app) as client:
        assert diff_request(client, **data).status_code == 400


@pytest.mark.parametrize("bad", [b"\xff", b"hello\0there"])
def test_unreadable_diff_is_a_clear_request_error(bad):
    with TestClient(app) as client:
        response = diff_request(client, bad)
        assert response.status_code == 400
        assert "왼쪽" in response.json()["detail"]
        assert client.get("/health").status_code == 200


def test_diff_has_no_old_file_size_cap():
    content = b"a" * (10 * 1024 * 1024 + 1)
    with TestClient(app) as client:
        response = diff_request(client, content + b"\nold", content + b"\nnew")
    assert response.status_code == 200
    assert [line["text"] for line in response.json()["lines"] if line["kind"] in ("delete", "add")] == ["old", "new"]


def test_diff_rejects_multiple_pairs_and_non_multipart():
    with TestClient(app) as client:
        assert client.post("/api/diff", json={}).status_code == 400
        response = client.post("/api/diff", data={"left_paths": '["a", "b"]', "right_paths": '[]'},
                               files=[("left_files", ("a", b"one")), ("left_files", ("b", b"two"))])
    assert response.status_code == 400


@pytest.mark.parametrize("index", [0, 5000, 9999])
def test_one_edit_in_large_repeated_file_is_one_change(index):
    lines = ["common line"] * 10000
    left = "\n".join(lines).encode()
    lines[index] = "changed line"
    with TestClient(app) as client:
        response = diff_request(client, left, "\n".join(lines).encode())
    assert response.status_code == 200
    diff = response.json()
    assert (diff["additions"], diff["deletions"]) == (1, 1)
    assert len(diff["lines"]) <= 9
    assert [line["new_line"] for line in diff["lines"] if line["kind"] == "add"] == [index + 1]


def test_distant_edits_in_repeated_file_do_not_do_quadratic_work():
    old = b"common line\n" * 10000
    new = b"changed first\n" + b"common line\n" * 9998 + b"changed last\n"
    with TestClient(app) as client:
        start = time.perf_counter()
        response = diff_request(client, old, new)
        elapsed = time.perf_counter() - start
    assert response.status_code == 200
    diff = response.json()
    assert (diff["additions"], diff["deletions"]) == (2, 2)
    assert len(diff["lines"]) <= 16
    assert elapsed < 3, f"A 120 KB repeated-line diff took {elapsed:.2f}s"


def test_diff_hunks_can_reconstruct_mixed_and_large_changes():
    rng = random.Random(42)
    pairs = [([rng.choice(["same", "repeated", "", "한글"]) for _ in range(rng.randrange(30))],
              [rng.choice(["same", "repeated", "", "한글"]) for _ in range(rng.randrange(30))])
             for _ in range(60)]
    pairs.append(([f"old {i}" for i in range(500)] + ["shared"],
                  [f"new {i}" for i in range(500)] + ["shared", "last"]))
    with TestClient(app) as client:
        for old, new in pairs:
            def encode(lines):
                return ("\n".join(lines) + "\n").encode() if lines else b""

            response = diff_request(client, encode(old), encode(new))
            assert response.status_code == 200
            rebuilt = []
            cursor = 0
            for line in response.json()["lines"]:
                if line["kind"] == "hunk":
                    hunk = re.fullmatch(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line["text"])
                    assert hunk
                    start = int(hunk[1]) - (0 if hunk[2] == "0" else 1)
                    new_start = int(hunk[3]) - (0 if hunk[4] == "0" else 1)
                    rebuilt.extend(old[cursor:start])
                    cursor = start
                    assert len(rebuilt) == new_start
                else:
                    if line["kind"] in ("context", "delete"):
                        assert line["old_line"] == cursor + 1
                        assert old[cursor] == line["text"]
                        cursor += 1
                    if line["kind"] in ("context", "add"):
                        assert line["new_line"] == len(rebuilt) + 1
                        rebuilt.append(line["text"])
            rebuilt.extend(old[cursor:])
            assert rebuilt == new


def test_many_spaced_edits_preserve_repeated_common_lines():
    lines = ["common line"] * 10000
    old = "\n".join(lines).encode()
    for i in range(129):
        lines[i * 9999 // 128] = f"edit {i}"
    with TestClient(app) as client:
        response = diff_request(client, old, "\n".join(lines).encode())
    assert response.status_code == 200
    diff = response.json()
    assert (diff["additions"], diff["deletions"]) == (129, 129)
    assert len(diff["lines"]) < 1300


@pytest.mark.parametrize('copies', [1, 2])
def test_large_edit_distance_keeps_shifted_identical_records_as_context(copies):
    shared = '202609|1011156045|CT9701|101'
    old = ([f'old {i}' for i in range(300)] + ['before']
           + ['202609|1011150624|CT963|101'] + [shared] * copies
           + ['after'] + [f'old tail {i}' for i in range(300)])
    new = ([f'new {i}' for i in range(300)] + ['before']
           + [shared] * copies + ['202609|1011157616|HW2561|101']
           + ['after'] + [f'new tail {i}' for i in range(300)])
    with TestClient(app) as client:
        response = diff_request(client, ('\n'.join(old) + '\n').encode(),
                                ('\n'.join(new) + '\n').encode())
    assert response.status_code == 200
    diff = response.json()
    assert [line['kind'] for line in diff['lines'] if line['text'] == shared] == ['context'] * copies
    assert (diff['additions'], diff['deletions']) == (601, 601)
