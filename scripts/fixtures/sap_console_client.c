/* Developer-only external PE fixtures. Not SAP clients or a database emulator.
 * Read the password from the console without echo to verify Windows ConPTY.
 * Deterministic file responses exercise BCP process integration; no database.
 */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <conio.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

static const wchar_t expected_password[] = L"fixture-db-password-never-log!42";

static const wchar_t *option(int argc, wchar_t **argv, const wchar_t *name) {
    int i;
    for (i = 1; i + 1 < argc; ++i) if (wcscmp(argv[i], name) == 0) return argv[i + 1];
    return NULL;
}

static void audit(int argc, wchar_t **argv, const wchar_t *program) {
    wchar_t path[32768], *slash;
    FILE *output;
    int i;
    if (!GetModuleFileNameW(NULL, path, 32768)) return;
    slash = wcsrchr(path, L'\\');
    if (!slash) return;
    wcscpy(slash + 1, L"fixture-commands.log");
    output = _wfopen(path, L"ab");
    if (!output) return;
    fprintf(output, "%ls\tPID=%lu", program, (unsigned long)GetCurrentProcessId());
    for (i = 1; i < argc; ++i) {
        int bytes = WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, NULL, 0, NULL, NULL);
        char *text = (char *)malloc((size_t)bytes);
        if (!text) { fclose(output); exit(9); }
        WideCharToMultiByte(CP_UTF8, 0, argv[i], -1, text, bytes, NULL, NULL);
        fprintf(output, "\t%s", text);
        free(text);
    }
    fputs("\n", output);
    fclose(output);
}

static int authenticate(int argc, wchar_t **argv) {
    wchar_t password[256];
    DWORD mode;
    int i, length = 0, character;
    for (i = 1; i < argc; ++i) if (wcscmp(argv[i], L"-P") == 0 || wcsncmp(argv[i], L"-P", 2) == 0) return 8;
    if (!GetConsoleMode(GetStdHandle(STD_INPUT_HANDLE), &mode)) return 7;
    fputs("Password:", stdout);
    fflush(stdout);
    while ((character = _getwch()) != '\r' && character != '\n') {
        if (character == WEOF || length >= 255) return 6;
        password[length++] = (wchar_t)character;
    }
    password[length] = 0;
    fputs("\n", stdout);
    fflush(stdout);
    i = wcscmp(password, expected_password) == 0;
    SecureZeroMemory(password, sizeof(password));
    if (!i) { fputs("Login failed.\n", stdout); return 5; }
    return 0;
}

static int bcp(int argc, wchar_t **argv) {
    FILE *data;
    int rows = 0, character;
    if (argc < 4 || !option(argc, argv, L"-J") || !option(argc, argv, L"-t") || !option(argc, argv, L"-r")) return 4;
    if (wcscmp(option(argc, argv, L"-J"), L"utf8") || wcscmp(option(argc, argv, L"-t"), L"\t") || wcscmp(option(argc, argv, L"-r"), L"\n")) return 4;
    if (wcscmp(argv[2], L"out") == 0) {
        if (!option(argc, argv, L"-T") || wcscmp(option(argc, argv, L"-T"), L"2147483647")) return 4;
        data = _wfopen(argv[3], L"wb");
        if (!data) return 4;
        fputs("1\n2\n3\n", data);
        fclose(data);
        if (wcsstr(argv[1], L"cancel_me")) { fputs("fixture transfer started\n", stdout); fflush(stdout); Sleep(30000); }
        if (wcsstr(argv[1], L"fail_me")) { fputs("Error: fixture transfer failure\n", stdout); return 3; }
        rows = 3;
    } else if (wcscmp(argv[2], L"in") == 0) {
        data = _wfopen(argv[3], L"rb");
        if (!data) return 4;
        while ((character = fgetc(data)) != EOF) if (character == '\n') ++rows;
        fclose(data);
        fprintf(stdout, "%d rows sent to the server. %d rows total sent.\n", rows, rows);
    } else return 4;
    /* Match SAP's no-newline startup banner and final row-count output. */
    fprintf(stdout, "Starting copy..%d rows copied.\nClock Time (ms.): total = 25 Avg = 8 (120 rows per sec.)\n", rows);
    fflush(stdout);
    return 0;
}

int wmain(int argc, wchar_t **argv) {
    wchar_t module[32768], *name;
    int status;
    SetConsoleOutputCP(CP_UTF8);
    SetConsoleCP(CP_UTF8);
    if (!GetModuleFileNameW(NULL, module, 32768)) return 9;
    name = wcsrchr(module, L'\\');
    name = name ? name + 1 : module;
    audit(argc, argv, name);
    status = authenticate(argc, argv);
    if (status) return status;
    return _wcsicmp(name, L"bcp.exe") == 0 ? bcp(argc, argv) : 4;
}
