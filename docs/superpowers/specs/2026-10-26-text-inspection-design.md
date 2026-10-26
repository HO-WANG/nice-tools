# Hidden character inspection

## Intent and constraints

Add a fifth tool to the existing local Windows EXE. The user wants to identify unusual text characters such as spaces that look invisible, then requested a strict-mode checkbox with a hover tooltip, automatic or directly entered encoding names, and very fast processing of large files. This is an inspection tool: source files are not rewritten. Results are review candidates, since joiners/variation selectors and non-ASCII scripts may be intentional.

## Approaches

The implementation uses the established native file picker, session grants and owned-process lifecycle, plus a streamed Unicode scanner and disk-backed result pages. This preserves local direct reads and cancellation without transferring native-mode files through the browser. A browser-only decoder would duplicate legacy-encoding and file-handling rules; loading every file/finding in memory would conflict with the large-file requirement. These alternatives were discarded as implementation choices. Native Windows is the normal EXE flow; the non-Windows development flow explicitly supports a streamed browser upload.

## Detection policy

Normal mode detects unusual Unicode whitespace, zero-width/format/bidi controls, C0/C1 controls including NUL, Hangul/Khmer/Braille blank fillers, variation/grapheme controls, replacement characters and Unicode noncharacters. Ordinary space, TAB and CR/LF have separate counts and can optionally appear as findings. Leading encoding BOM is metadata; an embedded BOM is a candidate. Normal combining marks and ordinary letters are not blanket errors.

Strict mode is off by default. It permits printable ASCII (letters, numbers, punctuation and normal space), TAB, CR/LF, and assigned Hangul syllables and Jamo. Blank fillers remain findings even within Hangul blocks. All other characters are reported; existing specific categories take precedence over the generic strict category. The tooltip states that CJK ideographs, emoji, fullwidth letters and accented Latin letters are included by this policy. It does not assert that such text is invalid.

Positions are one-based Unicode characters, with TAB one character and CRLF one line ending. Each result contains position, Unicode code point/name, kind, a visible marker, and bounded before/after context. All invisible/bidi/control characters in context are replaced by visible notation; rendering uses text nodes and isolated directionality.

## Encoding and performance

Automatic detection uses BOM/Unicode lane evidence and existing Korean/Japanese/Western heuristics with full strict decoding. Manual input accepts standard registered incremental byte-to-text codec names and aliases; transforms such as escape interpretation, base64, compression and domain codecs are rejected. NUL/control code points are scan findings rather than reasons to reject otherwise decoded text. Bad bytes are never replaced or dropped. Ambiguous short/BOMless input can be inspected with an explicit codec.

The scanner processes bounded chunks and uses compiled candidate regexes so allowed ASCII/Hangul runs stay in C regex scanning. Manual codecs validate during the sole full read. BOMless UTF-8 uses a bounded Unicode-alignment header check then an optimistic strict scan; if decoding fails, provisional pages are discarded before full candidate detection/retry. BOM/manual and cache failures never trigger fallback. Metadata publishes only after EOF, so a malformed tail cannot create a false successful scan. There is no input-byte or finding-count cap. Result storage is bounded to 200 rows in memory and pages on temporary disk; files with many findings necessarily incur more output I/O.

## API, ownership and cache

The loopback/origin/custom-header/no-store API uses opaque sessions, selected-file tokens and job handles. HTTP cannot supply source filesystem paths. Jobs reuse atomic cancellation/process registration, per-job Windows Job/POSIX process ownership and bounded worker slots. Source sharing guards and identity/size/time fingerprints span decoding and are checked again on page reads. Pages read directly in the parent after validation without another EXE bootstrap.

Upload readers keep their multipart input open until their thread settles, including repeated cancellation. Session close revokes grants, terminates owned work, waits for selected-file operations and cleans upload/result folders. Job registration is atomic with session close, avoiding caches created after the closing session took its work snapshot. Eviction retains at most two job results and removes unreferenced upload copies. No content, contexts or tokens enter logs or browser storage.

## Verification

Cover detection families, strict whitelist boundaries, codec aliases/transform rejection, BOM/NUL, exact positions at chunk and CRLF boundaries, context safety, page completeness, huge one-line files, late findings/invalid bytes, retry cleanup and unchanged originals. Real source HTTP-to-worker tests complement mocked native browser tests. Include mobile layouts, tooltip/accessibility, manual text entry, stale replies, cancellation and session cleanup. The actual frozen EXE must inspect real UTF-8/UTF-16/manual legacy files, strict/non-strict inputs, a late finding past 10MiB, multiple pages and changed-source rejection. Existing comparison, ASE, IQ and Java Jasypt checks remain required.
