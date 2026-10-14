# nice-tools

사용자 PC에서 사용하는 작은 웹 도구 모음입니다.
**배포물은 Windows 10/11 x64용 `nice-tools.exe` 한 개입니다.**
Python, 필요한 라이브러리와 HTML·CSS·JavaScript 화면을 EXE에 포함합니다.
접을 수 있는 왼쪽 메뉴에서 도구를 선택하며 실행 중 외부 CDN을 사용하지 않습니다.

첫 도구는 **디렉토리 텍스트 비교**입니다. 사용자 PC의 폴더 두 개를
브라우저에서 선택하고, 같은 하위 상대 경로의 파일을 비교합니다.
두 번째 도구는 **Jasypt 암호화**입니다. 왼쪽 메뉴에서 선택해 문자열을
암호화·복호화하며, Java Jasypt와 호환되는 설정을 지원합니다.
세 번째 도구는 **ASE / BCP**입니다. Sybase ASE의 오브젝트를 탐색하고,
PC의 문자형 파일로 BCP IN / OUT을 실행합니다. 필요한 DB 클라이언트도 EXE 안에 포함합니다.

## 사용자 실행 방법

1. 검증된 `nice-tools.exe`를 사용자 PC로 옮깁니다.
2. EXE를 더블클릭합니다. 실행창이 열리고 기본 브라우저가 자동으로 열립니다.
3. Chrome 또는 Edge의 왼쪽 메뉴에서 사용할 도구를 선택합니다.
4. 종료하려면 실행창에서 `Ctrl+C`를 누릅니다. 브라우저 탭만 닫으면 앱은 계속 실행됩니다.

사용자 PC에 Python이나 라이브러리를 설치할 필요가 없습니다.
앱은 `127.0.0.1`의 빈 포트를 자동으로 선택합니다.
브라우저가 자동으로 열리지 않으면 실행창에 표시된 주소를 Chrome/Edge에 입력합니다.
선택한 파일의 전송과 비교는 같은 PC 안에서 처리합니다.

## 실행 파일 받기

GitHub의 **Actions → Build Windows executable**에서 성공한 작업을 열고
`nice-tools-windows-x64` 아티팩트를 내려받습니다.
GitHub가 내려주는 ZIP의 내용은 `nice-tools.exe` 한 파일입니다.
압축을 풀어 EXE만 내부망 PC로 반입합니다.

`main` 변경, `v*` 태그와 Pull Request에는 Windows 빌드·검증이 자동 실행됩니다.
Actions의 **Run workflow**로 수동 빌드할 수도 있습니다.
소스 테스트나 Linux 패키징 검사만으로 Windows EXE가 검증되었다고 판단하지 않습니다.

## 개발 환경 설정 (개발자용)

Python 3.12 이상이 필요합니다. 저장소 루트에서 실행합니다.

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m uvicorn nice_tools.main:app --reload --host 127.0.0.1 --port 8000
```

실행한 PC의 브라우저에서 `http://127.0.0.1:8000` 주소를 엽니다.
폴더 선택 기능을 지원하는 Chrome 또는 Edge 사용을 권장합니다.

Windows에서는 가상 환경의 Python 경로를 다음처럼 바꿉니다.

```powershell
py -3.12 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-dev.txt
.venv\Scripts\python -m uvicorn nice_tools.main:app --reload --host 127.0.0.1 --port 8000
```

## 디렉토리 비교

1. 왼쪽 폴더와 오른쪽 폴더를 선택합니다.
2. 필요한 비교 옵션을 설정합니다.
3. **비교 시작**을 누릅니다.
4. 요약과 파일별 결과를 확인하고, 상태 필터로 필요한 파일만 봅니다.
5. 파일 이름을 선택하면 **Git diff 형태**로 변경된 줄을 봅니다. 왼쪽의 삭제는 `-`와 빨간색,
   오른쪽의 추가는 `+`와 초록색이며, 양쪽 줄 번호와 변경 주변 3줄을 표시합니다.

상세 보기의 **옵션 적용**은 동일 여부를 판단한 줄을 보여 줍니다. **원본**으로 바꾸면
줄 정렬·공백 무시·앞뒤 글자 제외를 적용하지 않은 줄의 차이를 확인할 수 있습니다.
옵션 적용 보기의 줄 번호는 변환된 줄 기준이며, 정렬했으면 정렬된 순서입니다.
두 보기 모두 기존 비교 규칙처럼 줄바꿈 형식과 마지막 줄바꿈 유무는 무시합니다.
한쪽에만 있는 파일도 전체 줄의 추가·삭제로 볼 수 있습니다.
긴 diff는 화면에서 200줄씩 넘겨 보며, 파일 전체의 비교나 diff 계산을 자르지 않습니다.

예를 들어 `before/sub/a.txt`와 `after/sub/a.txt`는 서로 비교합니다.
최상위 폴더 이름은 달라도 됩니다. 결과 상태는 동일, 차이, 왼쪽만,
오른쪽만, 읽기 오류입니다. 하위 디렉토리를 포함하며 확장자로 파일을 제외하지 않습니다.

| 옵션 | 동작 |
| --- | --- |
| 줄 정렬 | 변환한 줄들을 정렬한 뒤 비교합니다. 중복 줄은 유지합니다. |
| 공백 무시 | 각 줄 안의 스페이스, 탭과 기타 Unicode 공백을 제거합니다. 빈 줄은 유지합니다. |
| 앞에서 무시할 글자 수 | 각 줄의 처음에서 지정한 수만큼 제외합니다. |
| 뒤에서 무시할 글자 수 | 각 줄의 끝에서 지정한 수만큼 제외합니다. |

처리 순서는 **앞·뒤 글자 제외 → 공백 제거 → 줄 정렬 → 비교**입니다.
글자 수는 Unicode 코드 포인트 기준이며, 한글과 이모지도 바이트로 자르지 않습니다.
앞·뒤 제외 범위가 겹치면 해당 줄은 빈 문자열이 됩니다.

각 파일의 인코딩을 자동 감지합니다. UTF-8, BOM이 있는 UTF-8·UTF-16·UTF-32와
한글 CP949/EUC-KR, 일본어 Shift-JIS/CP932, 서유럽 Windows-1252 등의 텍스트를 처리합니다.
양쪽 파일의 인코딩이 달라도 해석한 글자가 같으면 동일하게 비교합니다.
BOM이 없는 UTF-16·UTF-32는 바이트 배열에서 충분한 근거가 있을 때 감지합니다.
짧은 파일이나 여러 인코딩으로 해석 가능한 파일은 자동 감지가 모호할 수 있습니다.
이런 파일은 UTF-8 또는 BOM이 있는 Unicode로 저장하면 명확하게 비교할 수 있습니다.
감지에는 일부 구간을 사용해도 최종 해석·검증·비교와 diff는 **파일 전체**에 적용하며,
해석할 수 없는 바이트를 임의의 문자로 바꾸거나 건너뛰지 않습니다.
올바른 텍스트로 읽을 수 없거나 해석 후 NUL 문자가 포함된 파일은 읽기 오류로 표시합니다.
CRLF, LF, CR은 동일한 줄바꿈으로 처리하며 마지막 줄 끝의 줄바꿈 유무는 무시합니다.
중간의 빈 줄은 비교 대상입니다.
한쪽에만 있는 파일은 내용 해석 없이 존재 상태를 표시합니다.

파일과 업로드 요청에 고정된 크기 제한을 두지 않습니다.
실제 처리 가능한 크기는 사용자 PC의 메모리와 임시 저장 공간에 따라 달라집니다.
파일 개수는 양쪽 합계 **1,000개**까지 처리합니다.
브라우저가 제공하지 않는 빈 폴더와 빈 디렉토리 구조는 비교할 수 없습니다.

선택한 파일은 비교를 위해 서버로 전송합니다. 비교 과정에서 메모리를 사용하며,
프레임워크가 만든 임시 업로드 파일은 요청 완료 후 정리합니다.
파일과 비교 기록을 영구 저장하지 않습니다.
상세 보기를 열 때 선택한 파일만 같은 PC의 앱으로 다시 보내며, 요청 후 임시 업로드를 정리합니다.

## Jasypt 암호화

왼쪽 메뉴의 **Jasypt 암호화**를 선택합니다. 알고리즘·비밀번호와 기존 서비스의
설정을 입력하고, **Encrypt** 또는 **Decrypt**를 실행합니다.
결과를 복사하거나 **결과를 입력으로** 눌러 반대 작업에 사용할 수 있습니다.
**모두 지우기**는 입력·비밀번호·결과와 설정을 초기화합니다.

| 입력 | 지원 값 / 대응하는 Jasypt 설정 |
| --- | --- |
| 알고리즘 | `PBEWithMD5AndDES` (기본), `PBEWITHHMACSHA512ANDAES_256` |
| 비밀번호 | `password`: ASCII 영문·숫자·기호·스페이스. Java SunJCE와 동일한 문자 조건을 사용합니다. |
| 키 생성 반복 횟수 | `keyObtentionIterations`: 기본 1,000, 입력 범위 1~1,000,000 |
| Salt 생성기 | `org.jasypt.salt.RandomSaltGenerator` (기본), `org.jasypt.salt.ZeroSaltGenerator` |
| IV 생성기 | `org.jasypt.iv.RandomIvGenerator` (AES 기본), `org.jasypt.iv.NoIvGenerator` (MD5/DES 기본) |
| 암호문 형식 | `stringOutputType`: `base64` (기본), `hexadecimal` |

처음 열거나 **모두 지우기**를 누르면 MD5/DES와 NoIvGenerator가 선택됩니다.
AES에는 RandomIvGenerator가 필요합니다. 알고리즘을 바꾸면 IV 기본값도 변경됩니다.
SunJCE / Java Jasypt 1.9.3의 두 알고리즘과 salt·IV 포맷을 지원하며,
다른 알고리즘·provider·사용자 정의 생성기는 지원 범위에 포함하지 않습니다.
원문은 UTF-8로 처리하므로 한글·이모지·공백·줄바꿈과 빈 문자열을 그대로 처리합니다.
랜덤 salt 또는 IV를 사용하면 같은 원문도 암호문이 달라집니다.

**ENC(...)로 감싸기**를 선택하면 Spring 설정에 사용할 형태로 암호문을 출력합니다.
복호화는 감싸진 값과 순수 암호문 모두 받습니다. 복호화 시 비밀번호·알고리즘·반복 횟수·
salt/IV·암호문 형식을 암호화한 서비스와 동일하게 맞춰야 합니다.
입력·비밀번호·결과를 파일·로그·브라우저 저장소에 저장하지 않고 같은 PC 안에서 처리합니다.
사용자 PC에는 Java를 설치할 필요가 없습니다.

## ASE / BCP

1. 왼쪽 **ASE / BCP** 메뉴에서 설정 파일의 서버를 선택하고 **Jasypt 복호화 키**를 입력해 연결합니다.
   설정 파일이 없으면 **직접 입력**을 선택해 호스트, 포트(기본 5000), 사용자와 DB 비밀번호로 연결할 수 있습니다.
2. 데이터베이스를 선택합니다. 소유자는 기본 **전체 소유자**이며 선택하지 않아도 됩니다.
   특정 소유자가 필요하면 소유자 목록을 열어 선택합니다.
   객체 이름과 종류를 지정한 뒤 **검색** 버튼 또는 Enter로 서버에서 조회합니다.
   DB·소유자·검색 조건을 바꿔도 객체를 자동 조회하지 않습니다.
   검색 결과의 소유자와 객체를 확인하고 선택하여 컬럼의 순서·자료형·NULL·IDENTITY 정보를 확인합니다.
3. **OUT** 또는 **IN**을 선택하고 파일 인코딩, 컬럼/행 구분자와 배치 행 수를 설정합니다.
4. **파일 선택**을 누릅니다. Windows 파일 선택 창에서 IN은 기존 파일,
   OUT은 저장할 위치를 지정합니다. 파일을 브라우저에 업로드하지 않습니다.
5. **실행 내용 확인**에서 대상·설정을 확인합니다. IN은 전체 파일을 검사하고 첫 20행을 보여 줍니다.
6. **BCP 시작**을 누릅니다. 상태와 처리 행 수를 확인하며, 필요하면 **취소**합니다.

OUT이 완료되면 **내보낸 행 수·전체 소요 시간·초당 평균 행 수·결과 파일 크기**를 표시합니다.
전체 소요 시간은 작업 시작부터 최종 파일 저장 완료까지이며 작업 프로세스 시작, ASE 접속,
테이블 구조 재확인과 실제 내보내기를 포함합니다. 초당 평균 행 수는 완료된 행 수를 이 시간으로 나눈 값입니다.
시간 측정에는 시스템 시각 변경의 영향을 받지 않는 타이머를 사용합니다.
빈 결과는 0행으로 표시하며 측정 시간이 0이면 처리 속도를 `측정 불가`로 표시합니다.
실패하거나 취소한 작업에는 완료 통계를 표시하지 않습니다.

서버 연결과 메타데이터 조회의 시간 정보에는 전체 시간, 서버 로그인, 세션 설정,
실제 조회와 준비 시간을 구분해 표시합니다. 현재 탐색 요청마다 별도 worker와 실제 ASE 연결을
생성하므로 로그인 비용이 반복됩니다. 이 값으로 EXE 실행 준비, 서버 로그인과 조회 중 어느 구간이
느린지 확인할 수 있습니다. 첫 화면의 연결 상태는 접속 정보를 유지하는 상태이며 열린 DB 연결의 재사용을 뜻하지 않습니다.
사용자 PC에서 관측한 지연의 원인은 그 PC의 시간 정보와 서버 상태를 함께 확인해야 합니다.

테이블은 IN/OUT, 뷰는 OUT을 지원합니다. 프로시저는 탐색만 가능합니다.
**IN은 기존 데이터에 행을 추가합니다. 삭제·TRUNCATE를 실행하지 않습니다.**
완료된 배치는 실패·취소 후에도 남을 수 있습니다. 같은 파일을 재실행하면 중복 행이 생길 수 있습니다.
OUT은 같은 폴더의 임시 파일에 쓰고, 전송이 성공했을 때만 지정한 파일을 교체합니다.
기존 파일을 덮어쓰려면 Windows 저장 창에서 확인합니다.
파일이나 테이블 구조가 확인 이후 변경되면 다시 확인해야 합니다.
IN을 실행하는 동안 입력 파일은 Windows에서 읽기 전용 공유로 열어 편집·삭제를 막습니다.

| 설정 | 문자형 BCP 규칙 |
| --- | --- |
| 파일 형식 | SAP `bcp -c`에 해당하는 문자 형식. native 형식과 format 파일은 지원하지 않습니다. |
| 인코딩 | UTF-8, CP949, EUC-KR. 파일에 실제 사용한 인코딩을 선택합니다. |
| 컬럼 구분자 | 기본 `\t`(탭). 쉼표·파이프 등의 문자와 `\t`, `\n`, `\r`, `\\`을 입력할 수 있습니다. |
| 행 구분자 | 기본 `\n`(LF). Windows CRLF 파일에는 `\r\n`을 선택합니다. 마지막 행에도 구분자가 필요합니다. |
| NULL / 빈 문자열 | 빈 필드는 NULL, 문자 필드의 단일 NUL 바이트는 BCP 빈 문자열 표시입니다. |
| 컬럼 순서 | 선택한 테이블의 모든 컬럼을 순서대로 포함합니다. CSV 따옴표·이스케이프 규칙은 적용하지 않습니다. |
| IDENTITY | 기본은 새 ID 생성입니다. 파일에 IDENTITY 자리도 포함합니다. 기존 ID를 유지하려면 체크합니다. |
| 배치 | 기본 1,000행. IN의 배치 완료 행 수와 최종 전송 행 수를 표시합니다. |

파일 전체를 스트리밍 검사하며 고정된 파일 크기 제한을 두지 않습니다.
미리보기에서 긴 셀을 줄여 표시해도 검사하거나 전송하는 파일은 자르지 않습니다.
숫자의 범위·소수 자릿수와 문자 길이를 검사하며, 날짜 변환·제약조건·권한은 ASE가 최종 판단합니다.
`unichar`/`univarchar`는 UTF-16 저장 길이를 검사합니다. 일반 문자 컬럼은 서버 문자셋을
알 수 없는 사전 검사에서 파일 인코딩의 바이트 길이를 보수적으로 사용합니다.
UTF-8 파일을 CP949 서버의 좁은 문자 컬럼에 넣으면 실제로 들어갈 값도 사전 검사에서
거절될 수 있으므로, 이 경우 서버 문자셋에 맞는 파일 인코딩을 사용하세요.
FreeTDS는 문자 값의 끝 공백을 제거할 수 있어 해당 값이 있으면 확인 화면에 알립니다.
첫 버전 IN은 binary/varbinary/image/timestamp와 알 수 없는 자료형을 포함한 테이블을 거절합니다.
계산·자동 생성·암호화 컬럼의 IN은 지원 범위에 포함하지 않습니다.

EXE에는 FreeTDS 1.5.19, GNU libiconv와 OpenSSL을 포함하며 TDS 5.0으로 ASE에 접속합니다.
SAP 클라이언트, ODBC 드라이버, 별도 `bcp.exe` 설치는 필요 없습니다.
설정 파일에는 접속 정보와 암호화된 DB 비밀번호를 보관합니다. 복호화 키와 평문 DB 비밀번호는
실행 중 메모리에서만 사용하며 명령행·파일·로그·브라우저 저장소에 남기지 않습니다.
데이터가 이동하는 범위는 **사용자 PC와 입력한 ASE 서버 사이**입니다.
서버 네트워크 접근과 계정의 조회/BCP 권한, DB의 bulk-copy 설정은 기존 사내 설정을 사용합니다.

현재 포함된 연결 방식은 FreeTDS입니다. jConnect는 Java JDBC 드라이버이므로 별도의 Java 런타임과
JAR 통합, BCP 파일 처리 구현·검증을 추가하면 지원할 수 있으며 현재 EXE에는 포함되어 있지 않습니다.

소스·브라우저 검사와 Windows DLL/EXE 실행 검증은 실제 ASE 왕복 테스트를 대신하지 않습니다.
현재 개발 환경에 ASE 서버가 없어 실제 서버의 문자셋, IDENTITY, LOB와 날짜 데이터를
IN/OUT하여 검증하지 못했습니다. 운영 데이터에 사용하기 전에 사내 ASE의 테스트 테이블에서
같은 설정으로 OUT → IN과 행 수·내용을 확인하세요.

### ASE 연결 설정 파일

실행 파일과 **같은 폴더**에 `ase-connections.txt`를 UTF-8로 저장합니다. UTF-8 BOM도 허용합니다.
실행한 작업 폴더나 EXE의 임시 압축 해제 폴더에서 찾지 않습니다.
소스로 실행할 때는 저장소 루트의 파일을 읽습니다.
배포 ZIP에는 EXE 한 개를 유지하며 이 설정 파일은 사용자가 자신의 서버 정보로 만듭니다.
저장소의 [`ase-connections.example.txt`](ase-connections.example.txt)를 복사해 시작할 수 있습니다.

1. **Jasypt 암호화** 도구에서 DB 비밀번호를 원문으로 넣고, 별도로 정한 복호화 키를 비밀번호에 입력합니다.
2. 기본 `PBEWithMD5AndDES`, 반복 1,000회, random salt, NoIvGenerator, base64 설정을 사용하고
   **ENC(...)로 감싸기**를 선택하여 Encrypt합니다.
3. 결과를 아래 `password` 값에 붙여 넣습니다. 복호화 키와 평문 DB 비밀번호는 파일에 쓰지 않습니다.
4. **ASE / BCP → 설정 파일 다시 읽기**를 누르고 서버를 선택합니다.
   **Jasypt 복호화 키**에 암호화할 때 사용한 키를 입력하여 연결합니다. 연결 성공 후 입력창의 키를 지웁니다.

```ini
[jasypt]
algorithm = PBEWithMD5AndDES
iterations = 1000
salt_generator = random
iv_generator = none
output_type = base64

[개발 ASE]
host = ase-dev.example.internal
port = 5000
user = bcp_user
password = ENC(PASTE_JASYPT_RESULT_HERE)
database = appdb
```

`PASTE_JASYPT_RESULT_HERE`는 예시 자리 표시자입니다. 실제 Encrypt 결과의 `ENC(...)` 전체로 교체하세요.
서버마다 새 `[서버 이름]` 구역을 추가하며 `host`, `port`, `user`, `password`는 필수입니다.
`database`는 선택 사항으로 연결 후 처음 선택할 DB를 지정합니다.
`[jasypt]` 설정은 파일의 모든 서버 비밀번호에 공통 적용되며 암호화할 때의 설정과 같아야 합니다.
이 구역을 생략하면 위 MD5/DES 기본값을 사용합니다. AES 암호문에는
`algorithm = PBEWITHHMACSHA512ANDAES_256`과 `iv_generator = random`을 지정합니다.
지원하는 값은 salt `random`/`zero`, IV `none`/`random`, 암호문 형식 `base64`/`hexadecimal`입니다.

주석은 별도 줄에서 `#` 또는 `;`로 시작합니다. 값 뒤에 주석을 붙이지 마세요.
중복 항목, `[DEFAULT]`, 알 수 없는 항목과 평문 `password`는 거절합니다.
파일이 없거나 읽기에 실패하면 직접 입력으로 연결할 수 있습니다. 파일을 수정한 뒤에는 목록을 다시 읽으세요.
설정 파일의 호스트·계정·DB 이름은 평문이므로 파일 접근 권한은 사용자 PC에서 관리합니다.

## 테스트와 실행 확인

```bash
.venv/bin/python -m pytest -q
```

서버를 실행한 상태에서 실제 HTTP 요청을 확인합니다.

```bash
.venv/bin/python scripts/smoke.py
```

기존 10 MiB 파일·50 MiB 요청 한도를 넘는 실제 비교도 확인할 수 있습니다.

```bash
.venv/bin/python scripts/smoke.py --large-files
```

Chromium 또는 Chrome이 설치되어 있으면 실제 폴더 선택과 화면 동작도 확인할 수 있습니다.
개발 의존성의 Playwright는 실행 중인 서버와 설치된 브라우저를 사용합니다.

```bash
.venv/bin/python tests/browser_smoke.py --chromium /usr/bin/chromium
.venv/bin/python tests/browser_jasypt.py --chromium /usr/bin/chromium
.venv/bin/python scripts/browser_ase.py --url http://127.0.0.1:8000
```

다른 서버/포트에는 `--url`을 사용합니다. Chrome 실행 파일이 다른 위치에 있으면
`--chromium`에 해당 경로를 지정합니다. 화면 캡처는 선택적으로 `--screenshot 경로.png`를 지정합니다.

Jasypt 기준 암호문은 실제 Java Jasypt 1.9.3으로 만든 12개 설정 조합입니다.
Java가 없는 개발 환경에서도 기준 암호문 테스트를 실행할 수 있습니다.
양방향 호환을 다시 검사하려면 **개발 환경에만** Java 17 이상 JDK를 준비하고 실행합니다.

```bash
.venv/bin/python scripts/check_jasypt_java.py
```

검사용 JAR을 Maven에서 내려받고 고정된 SHA256을 확인합니다.
기존 JAR이 있으면 `--jar 경로`로 지정할 수 있습니다. Windows CI도 이 검사를 실행합니다.
검사용 JAR·JDK는 배포 EXE에 포함하거나 사용자 PC에 요구하지 않습니다.

## Windows EXE 만들기 (개발자용)

Windows x64 빌드 PC에 Python 3.12, Visual Studio 2022의 C++ 빌드 도구와 Windows SDK,
CMake, Git, Perl을 준비한 뒤 저장소 루트에서 실행합니다. Git for Windows의 Perl도 사용할 수 있습니다.
이 도구들은 빌드 개발자에게만 필요합니다.

```powershell
powershell -NoProfile -File scripts\build_windows.ps1
```

빌드 도구는 `.venv-build`에 설치합니다. 빌드와 실제 실행 검증이 통과하면
`dist\nice-tools.exe`가 만들어집니다. 배포 폴더에는 이 파일 하나만 있어야 합니다.
PowerShell 실행 정책은 사내 정책을 따릅니다.
빌드 중 고정된 vcpkg revision과 FreeTDS·libiconv·OpenSSL 소스를 내려받고 체크섬을 검사합니다.
ASE DLL·필요한 CRT와 라이선스·원본 소스·패치를 EXE에 포함합니다.
FreeTDS에는 Unicode 경로, 바이너리 파일 열기, 날짜 소수 6자리, 오버플로 검사와
전송 실패 전파 패치를 적용합니다. `build/ase-native`와 vcpkg 바이너리 캐시는 개발용 중간 산출물입니다.

실행기 개발은 다음 명령으로 확인할 수 있습니다.

```bash
.venv/bin/python -m nice_tools.desktop --no-browser
.venv/bin/python -m pytest -q
```

`--no-browser`와 `--port`는 자동 검증이나 개발할 때 사용하는 실행 옵션입니다.
실행 상태 확인 경로는 `GET /health`입니다.

PyInstaller는 대상 OS에서 실행해야 합니다. Windows 릴리스는 Windows에서 빌드합니다.
`scripts/verify_bundle.py`는 파일 하나만 있는 별도 폴더(공백·한글 경로)에서 EXE를 실행하고
화면/정적 파일, 실제 비교 API와 파일별 diff API를 검사합니다. 큰 파일의 끝부분 차이와 diff, 54 MiB
업로드 요청도 검증하며 Windows x64 PE 헤더를 확인합니다.
서로 다른 인코딩의 동일 텍스트와 10 MiB 이후 CP949 한글 diff를 확인합니다.
Jasypt의 AES/DES, Base64/Hex와 salt·IV 조합 12개 및 MD5/DES 기본값도 실제 EXE에서 검사합니다.
ASE는 EXE의 실제 worker에서 모든 네이티브 함수의 로드와 접속 거절 오류·비밀번호 비노출을 검사합니다.
격리 폴더에 만든 암호화 설정 파일의 위치, 키 오류와 복호화 후 네이티브 접속 시도도 확인하고 파일을 지웁니다.
Windows 소스 검사에는 입력 파일의 읽기 공유 잠금도 포함됩니다. ASE 전송 성공은 서버가 있어야 검증할 수 있습니다.

## 구조와 도구 추가

- `nice_tools/comparison.py`: 텍스트 정규화와 디렉토리 비교
- `nice_tools/text_encoding.py`: 전체 파일을 엄격히 해석하는 인코딩 자동 감지
- `nice_tools/diff.py`: 선택한 파일의 unified diff, 줄 번호와 주변 문맥
- `nice_tools/jasypt.py`: Java Jasypt 호환 문자열 암호화·복호화
- `nice_tools/ase_native.py`: FreeTDS DB-Library 연결·카탈로그·문자형 BCP
- `nice_tools/ase_files.py`: 스트리밍 파일 검사와 미리보기
- `nice_tools/ase_worker.py`, `ase_picker.py`: 격리 프로세스와 Windows 파일 선택
- `nice_tools/ase_api.py`, `ase_jobs.py`: 로컬 API, 메모리 접속·확인·취소 작업
- `nice_tools/ase_profiles.py`: EXE 옆의 암호화된 ASE 연결 설정 읽기
- `scripts/build_ase_native.ps1`: 네이티브 라이브러리 빌드·체크섬·배포 자료
- `nice_tools/uploads.py`: 업로드·경로·옵션 검증
- `nice_tools/limits.py`: 비교할 파일 개수 정책
- `nice_tools/main.py`: 정적 화면, 상태 확인, 비교·diff API
- `nice_tools/desktop.py`: 자동 포트 선택과 브라우저 실행
- `nice_tools/static/`: 메뉴와 도구 화면
- `packaging/nice-tools.spec`: 런타임과 화면을 포함한 단일 실행 파일 구성
- `scripts/build_windows.ps1`: Windows 빌드와 배포 파일 검증
- `.github/workflows/windows-exe.yml`: Windows 자동 빌드·검증·아티팩트 업로드
- `tests/`: 비교·API·화면 테스트

새 도구를 추가할 때는 메뉴와 도구 화면을 연결하고, 필요한 Python 로직을
별도 모듈과 API로 추가합니다. 비교 로직은 HTTP에 의존하지 않습니다.
의존성 버전은 런타임과 개발 요구 파일에 전이 의존성까지 고정했습니다.

클라우드 작업은 기존 `/workspace/nice-tools` 체크아웃을 사용합니다.
각 작업이 이미 격리되어 있으므로 별도 Git worktree를 만들 필요가 없습니다.
