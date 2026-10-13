"""Apply the reviewed nice-tools fixes to one exact FreeTDS source revision.

The downloaded archive is verified by build_ase_native.ps1. Each edited file
also has an independent digest and each replacement must match exactly once.
No database, compiler, or project dependency is needed to run this patcher.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

FREETDS_VERSION = "1.5.19"
FREETDS_COMMIT = "4db0a7bb97c9c6a476c6d5a9c47283f65685a644"
SOURCE_URL = f"https://codeload.github.com/FreeTDS/freetds/tar.gz/{FREETDS_COMMIT}"
SOURCE_SHA256 = "0a1d0fa066e23c5236c0085d5df3aa3af90c2e68026d50a9e985176790109feb"
VCPKG_COMMIT = "e456309491fd875487bf02b8a0dca76c1b1b00cc"
PATCH_REVISION = 1

ORIGINAL_SHA256 = {
    "src/dblib/bcp.c": "5ec8122e50f2691451f73aa54087804ed1cd687d21c78f92f7027ff9d2d8727b",
    "src/tds/bulk.c": "08cc6f500bae30953b42d940e10370cb04f92a4bcdc3adf0f57c25482858f878",
}

UTF8_FILE_HELPER = r'''/* nice-tools: only BCP host/error filenames are UTF-8, opened as bytes. */
static FILE *
_bcp_fopen_utf8(const char *filename, const char *mode)
{
#ifdef _WIN32
	int length;
	wchar_t *wide_filename;
	const wchar_t *wide_mode;
	FILE *file;

	if (strcmp(mode, "rb") == 0)
		wide_mode = L"rb";
	else if (strcmp(mode, "wb") == 0)
		wide_mode = L"wb";
	else {
		errno = EINVAL;
		return NULL;
	}
	length = MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, filename, -1, NULL, 0);
	if (length <= 0) {
		errno = EINVAL;
		return NULL;
	}
	wide_filename = tds_new(wchar_t, length);
	if (!wide_filename) {
		errno = ENOMEM;
		return NULL;
	}
	if (MultiByteToWideChar(CP_UTF8, MB_ERR_INVALID_CHARS, filename, -1, wide_filename, length) != length) {
		free(wide_filename);
		errno = EINVAL;
		return NULL;
	}
	file = _wfopen(wide_filename, wide_mode);
	free(wide_filename);
	return file;
#else
	return fopen(filename, mode);
#endif
}

'''


def replace_once(text: str, before: str, after: str, description: str) -> str:
    count = text.count(before)
    if count != 1:
        raise ValueError(f"{description}: expected one pinned source match, found {count}")
    return text.replace(before, after, 1)


def patched_bcp(text: str) -> str:
    changes = [
        ("#ifdef _WIN32\n#include <io.h>\n#endif", "#ifdef _WIN32\n#include <io.h>\n#include <freetds/windows.h>\n#endif", "Windows UTF-8 filename APIs with Winsock include order"),
        ("static void _bcp_free_storage(DBPROCESS * dbproc);", UTF8_FILE_HELPER + "static void _bcp_free_storage(DBPROCESS * dbproc);", "private UTF-8 file helper"),
        ('fopen(dbproc->hostfileinfo->hostfile, "w")', '_bcp_fopen_utf8(dbproc->hostfileinfo->hostfile, "wb")', "binary output file"),
        ('fopen(dbproc->hostfileinfo->hostfile, "r")', '_bcp_fopen_utf8(dbproc->hostfileinfo->hostfile, "rb")', "binary input file"),
        ('fopen(dbproc->hostfileinfo->errorfile, "w")', '_bcp_fopen_utf8(dbproc->hostfileinfo->errorfile, "wb")', "binary error file"),
        ("bcpdatefmt, &when, 3);", "bcpdatefmt, &when, (curcol->column_prec > 0 && curcol->column_prec <= 7)\n\t\t\t\t\t ? curcol->column_prec : 3);", "server temporal precision"),
        (
            "\twhile (tds_process_tokens(tds, &result_type, NULL, TDS_STOPAT_ROWFMT|TDS_RETURN_DONE|TDS_RETURN_ROW|TDS_RETURN_COMPUTE)\n\t\t== TDS_SUCCESS)",
            "\twhile ((tdsret = tds_process_tokens(tds, &result_type, NULL, TDS_STOPAT_ROWFMT|TDS_RETURN_DONE|TDS_RETURN_ROW|TDS_RETURN_COMPUTE))\n\t\t== TDS_SUCCESS)",
            "retain export transport result",
        ),
        (
            "\tif (fclose(hostfile) != 0) {\n\t\tdbperror(dbproc, SYBEBCUC, errno);\n\t\tgoto Cleanup;\n\t}\n\thostfile = NULL;",
            "\t/* nice-tools: never publish a partial export after a transport failure. */\n"
            "\tif (TDS_FAILED(tdsret))\n\t\tgoto Cleanup;\n"
            "\t{\n\t\tint close_result = fclose(hostfile);\n\t\thostfile = NULL;\n"
            "\t\tif (close_result != 0) {\n\t\t\tdbperror(dbproc, SYBEBCUC, errno);\n\t\t\tgoto Cleanup;\n\t\t}\n\t}",
            "export transport and flush failure",
        ),
        (
            "\t\t\t\trtrim_bcpcol(bcpcol);",
            "\t\t\t\tif (TDS_SUCCEED(rc)) {\n"
            "\t\t\t\t\trtrim_bcpcol(bcpcol);\n"
            "\t\t\t\t\t/* nice-tools: converted data is in server bytes; never truncate it. */\n"
            "\t\t\t\t\tif (!is_numeric_type(bcpcol->on_server.column_type)\n"
            "\t\t\t\t\t    && !is_blob_type(bcpcol->on_server.column_type)\n"
            "\t\t\t\t\t    && bcpcol->bcp_column_data->datalen > bcpcol->on_server.column_size) {\n"
            "\t\t\t\t\t\thostcol->column_error = HOST_COL_CONV_ERROR;\n"
            "\t\t\t\t\t\t*row_error = true;\n"
            "\t\t\t\t\t\tdbperror(dbproc, SYBEBCOR, 0);\n"
            "\t\t\t\t\t}\n"
            "\t\t\t\t}",
            "file column oversize rejection",
        ),
        (
            "\t\t\t\ttds_bcp_start(tds, dbproc->bcpinfo);",
            "\t\t\t\tif (TDS_FAILED(tds_bcp_start(tds, dbproc->bcpinfo))) {\n"
            "\t\t\t\t\tret = FAIL;\n\t\t\t\t\tbreak;\n\t\t\t\t}",
            "batch restart failure",
        ),
        (
            "\t\t}\n\t}\n\t\n\tif (row_error_count == 0 && row_of_hostfile < dbproc->hostfileinfo->firstrow)",
            "\t\t} else {\n\t\t\t/* nice-tools: a failed record must stop the import. */\n"
            "\t\t\tret = FAIL;\n\t\t\tbreak;\n\t\t}\n\t}\n\t\n"
            "\tif (row_error_count == 0 && row_of_hostfile < dbproc->hostfileinfo->firstrow)",
            "record send failure",
        ),
        (
            "\tif (errfile &&  0 != fclose(errfile) ) {\n\t\tdbperror(dbproc, SYBEBUCE, 0);\n\t}",
            "\tif (errfile &&  0 != fclose(errfile) ) {\n\t\tdbperror(dbproc, SYBEBUCE, 0);\n\t\tret = FAIL;\n\t}",
            "error-file close failure",
        ),
        (
            "\ttds_bcp_done(tds, &rows_written_so_far);\n\t*rows_copied += rows_written_so_far;\n\n"
            "\treturn ret == NO_MORE_ROWS? SUCCEED : FAIL;",
            "\t/* nice-tools: count rows only after the server confirms the final batch. */\n"
            "\tif (TDS_FAILED(tds_bcp_done(tds, &rows_written_so_far)))\n\t\treturn FAIL;\n"
            "\t*rows_copied += rows_written_so_far;\n\n"
            "\treturn ret == NO_MORE_ROWS && row_error_count == 0? SUCCEED : FAIL;",
            "final confirmation and rejected-row failure",
        ),
    ]
    for before, after, description in changes:
        text = replace_once(text, before, after, description)
    # The format-file API intentionally retains its existing fopen semantics.
    if text.count("_bcp_fopen_utf8(") != 4 or 'fopen(filename, "r")' not in text:
        raise ValueError("BCP file wrapper escaped its three host/error call sites")
    return text


def patched_bulk(text: str) -> str:
    fixed = "\t\t\tcpbytes = bcpcol->bcp_column_data->datalen > column_size ?\n\t\t\t\t  column_size : bcpcol->bcp_column_data->datalen;"
    variable = "\t\t\t\tcpbytes = bcpcol->bcp_column_data->datalen > bcpcol->column_size ?\n\t\t\t\tbcpcol->column_size : bcpcol->bcp_column_data->datalen;"
    for before, indent, bound, description in [
        (fixed, "\t\t\t", "column_size", "fixed TDS5 bounds"),
        (variable, "\t\t\t\t", "bcpcol->on_server.column_size", "variable TDS5 bounds"),
    ]:
        guard = (
            f"{indent}/* nice-tools: retain the bounded copy, reject data loss first. */\n"
            f"{indent}if (bcpcol->bcp_column_data->datalen < 0\n"
            f"{indent}    || bcpcol->bcp_column_data->datalen > {bound}) {{\n"
            f"{indent}\ttdsdump_log(TDS_DBG_ERROR, \"BCP column exceeds server byte width\\n\");\n"
            f"{indent}\treturn -1;\n{indent}}}\n"
        )
        copy = before.replace("bcpcol->column_size", "bcpcol->on_server.column_size") if description.startswith("variable") else before
        text = replace_once(text, before, guard + copy, description)
    text = replace_once(
        text,
        "column_bcp_data_size  = bcpcol->column_size;",
        "column_bcp_data_size  = MAX(bcpcol->column_size, bcpcol->on_server.column_size);",
        "row allocation covers both character widths",
    )
    return text


def apply_patches(source: Path) -> dict:
    replacements = {}
    report = {"patch_revision": PATCH_REVISION, "freetds_version": FREETDS_VERSION,
              "freetds_commit": FREETDS_COMMIT, "source_sha256": SOURCE_SHA256, "files": {}}
    for relative, original_digest in ORIGINAL_SHA256.items():
        data = (source / relative).read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != original_digest:
            raise ValueError(f"{relative}: upstream source digest differs; refusing to patch")
        text = data.decode("utf-8")
        patched = (patched_bcp(text) if relative.endswith("bcp.c") else patched_bulk(text)).encode("utf-8")
        replacements[relative] = patched
        report["files"][relative] = {"original_sha256": original_digest,
                                    "patched_sha256": hashlib.sha256(patched).hexdigest()}
    # Validate every file before writing any file.
    for relative, data in replacements.items():
        (source / relative).write_bytes(data)
    (source / "nice-tools-patch-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    report = apply_patches(args.source.resolve())
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
