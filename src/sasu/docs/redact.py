"""문서를 넣기 전에 비밀을 가린다. 가린 글만 저장하고 원본 파일은 복사하지 않는다.

가리는 것 (종류 → 바꾸는 글):
- 접속 문자열의 비밀번호: jdbc:oracle:thin:user/pass@host, scheme://user:pass@host → 비밀번호 부분만
- 비밀번호: "비밀번호: abc", "password=abc", "DB_PASSWORD = 'abc'", "비번은 abc", "비밀번호 Abc#123"(띄어 쓴 꼴은 글자+숫자 섞인 값만)
- 계정/암호 쌍: "계정 admin / P@ss1", "ID/PW: admin/1234" → 계정은 두고 암호만
- API 키: sk-…, AKIA…, AIza…, ghp_…, xox…, "api_key=…", "secret: …", "서비스키: …", "API 키 ABC-123…"(16자 이상, 글자+숫자)
- 토큰: JWT(eyJ….….…), "token: …", "Bearer …"
- 주민등록번호: 900101-1234567
- 카드번호: 1234-5678-9012-3456 (4자리 넷), 카드 낱말 옆 16자리
- IP: 사설·공인 IPv4 (127.0.0.1·0.0.0.0은 둔다, 제목 번호 "1.2.3.4 …"는 두려고 줄 맨 앞은 뺀다)

돌려주는 값: (가린 글, 종류별 개수). 가린 자리는 [가림:종류]로 남아 문서를 읽는 사람이 무엇이 있었는지는 안다.
"""

import re
from collections import Counter

PASSWORD, PAIR, API_KEY, TOKEN, RRN, CARD, IP, CONN = (
    "비밀번호", "계정/암호", "API 키", "토큰", "주민번호", "카드번호", "IP", "접속 문자열")

_VALUE = r"[^\s가-힣,;'\"<>()\[\]]{3,}"  # 비밀 값: 공백·한글·구두점 전까지 3글자 이상


def _mask(kind: str) -> str:
    return f"[가림:{kind}]"


def _sub(pattern: re.Pattern, text: str, counts: Counter, kind: str, group: int = 1,
         keep=lambda m: False, found: list | None = None) -> str:
    """pattern의 group 부분만 [가림:kind]로 바꾼다. 이미 가린 자리는 건드리지 않는다.
    found가 있으면 (종류, 가린 값)을 더한다 (평가에서 무엇을 가렸나 볼 때만, 저장하지 않는다)."""
    out, last = [], 0
    for m in pattern.finditer(text):
        value = m.group(group)
        if (not value or "[가림:" in value or text.startswith("[가림:", m.end(group))
                or value.endswith("/") or keep(m)):
            continue
        out.append(text[last:m.start(group)])
        out.append(_mask(kind))
        last = m.end(group)
        counts[kind] += 1
        if found is not None:
            found.append((kind, value))
    out.append(text[last:])
    return "".join(out)


# 접속 문자열 ─────────────────────────────────────────────
_JDBC_ORACLE = re.compile(r"jdbc:oracle:[a-z]+:([\w.$-]+)/([^@\s]+)@", re.IGNORECASE)
_URL_CREDENTIAL = re.compile(r"\b[a-z][a-z0-9+.-]*://([^\s:/@]+):([^\s@/]+)@", re.IGNORECASE)

# 이름=값 꼴 ─────────────────────────────────────────────
_KEY_WORDS_PASSWORD = r"(?:password|passwd|pass_?wd|passwrd|pwd|(?<![A-Za-z])pw(?![A-Za-z])|비밀\s*번호|비번|패스\s*워드|암호)"
_PASSWORD_ASSIGN = re.compile(
    rf"(?<![A-Za-z])[\w.-]*{_KEY_WORDS_PASSWORD}[\w.-]*\s*(?:[:=：]|은|는|:=|->|=>)\s*[\"'`]?({_VALUE})",
    re.IGNORECASE)
_SECRET_ASSIGN = re.compile(
    rf"(?<![A-Za-z])[\w.-]*(?:api[_-]?key|apikey|secret(?:[_-]?key)?|client[_-]?secret|access[_-]?key|private[_-]?key"
    rf"|키\s*값|인증\s*키|승인\s*키|접근\s*키|비밀\s*키|개인\s*키|연계\s*키|암호화\s*키|서비스\s*키|service[_-]?key|api\s*키|시크릿)[\w.-]*\s*(?:[:=：]|은|는)\s*[\"'`]?({_VALUE})",
    re.IGNORECASE)
# 구분 기호 없이 띄어 쓴 꼴: "연계 API 키 SMSAPI-7f3a…", "비밀번호 Nas#Mnt2024" — 값이 글자와 숫자를 섞은 경우만
_SECRET_SPACED = re.compile(
    r"(?<![A-Za-z])(?:api[_-]?key|api\s*키|서비스\s*키|service[_-]?key|인증\s*키|승인\s*키|접근\s*키|비밀\s*키|개인\s*키|연계\s*키|암호화\s*키|시크릿|secret(?:[_-]?key)?)\s+"
    r"((?=[^\s]*\d)(?=[^\s]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9_\-+/=%.]{15,})", re.IGNORECASE)
_PASSWORD_SPACED = re.compile(
    rf"(?<![A-Za-z])[\w.-]*{_KEY_WORDS_PASSWORD}\s+((?=[^\s]*\d)(?=[^\s]*[A-Za-z])[^\s가-힣,;'\"<>()\[\]]{{6,}})",
    re.IGNORECASE)
_TOKEN_ASSIGN = re.compile(
    rf"(?<![A-Za-z])[\w.-]*(?:token|토큰)[\w.-]*\s*(?:[:=：]|은|는)\s*[\"'`]?({_VALUE})", re.IGNORECASE)
_BEARER = re.compile(r"\bBearer\s+([A-Za-z0-9._~+/=-]{12,})")

# 계정/암호 쌍: "계정 admin / P@ss1", "ID/PW: admin/1234", "sa / manager1 (DB 계정)"
_PAIR = re.compile(
    r"(?:(?:계정|아이디|ID|id)\s*/\s*(?:비밀번호|비번|암호|패스워드|PW|pw|PASS(?:WORD)?)|계정|아이디|ID\s*/\s*PW|id\s*/\s*pw|ID\s*/\s*PASS(?:WORD)?|user\s*/\s*pass(?:word)?|account|로그인\s*정보|접속\s*정보)"
    r"[^\n/]{0,20}?[:：]?\s*([A-Za-z0-9_.@-]{2,40})\s*/\s*([^\s/가-힣,;()]{3,})",
    re.IGNORECASE)

# 모양으로 알아보는 것 ─────────────────────────────────────
_KNOWN_KEYS = re.compile(
    r"\b(sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}"
    r"|gh[pousr]_[A-Za-z0-9]{30,}|xox[abprs]-[A-Za-z0-9-]{10,}|glpat-[A-Za-z0-9_-]{16,})")
_JWT = re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,})")
_RRN = re.compile(r"(?<!\d)(\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\s?-\s?[1-8]\d{6})(?!\d)")
_CARD = re.compile(r"(?<![\d-])(\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}|\d{4}-\d{6}-\d{5})(?![\d-])")
_CARD_WORD = re.compile(r"(?:카드|card)[^\n\d]{0,15}((?<!\d)\d{15,16}(?!\d))", re.IGNORECASE)
_IP = re.compile(r"(?<![\w.])((?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3})(?![\w.]*\d)")
_IP_KEEP = {"127.0.0.1", "0.0.0.0", "255.255.255.255", "255.255.255.0"}


def redact(text: str, found: list | None = None) -> tuple[str, Counter]:
    """found: 평가용. 주면 가린 (종류, 원래 값)을 순서대로 더한다."""
    counts: Counter = Counter()
    if not text:
        return text, counts
    f = found
    # 1. 접속 문자열 (IP 규칙보다 먼저: 비밀번호 자리가 따로 있다)
    text = _sub(_JDBC_ORACLE, text, counts, CONN, group=2, found=f)
    text = _sub(_URL_CREDENTIAL, text, counts, CONN, group=2, found=f)
    # 2. 모양이 분명한 키·토큰
    text = _sub(_KNOWN_KEYS, text, counts, API_KEY, found=f)
    text = _sub(_JWT, text, counts, TOKEN, found=f)
    text = _sub(_BEARER, text, counts, TOKEN, found=f)
    # 3. 이름=값
    text = _sub(_SECRET_ASSIGN, text, counts, API_KEY, found=f)
    text = _sub(_TOKEN_ASSIGN, text, counts, TOKEN, keep=_not_secret_value, found=f)
    text = _sub(_PAIR, text, counts, PAIR, group=2, keep=_not_pair, found=f)
    text = _sub(_PASSWORD_ASSIGN, text, counts, PASSWORD, keep=_not_secret_value, found=f)
    text = _sub(_SECRET_SPACED, text, counts, API_KEY, found=f)
    text = _sub(_PASSWORD_SPACED, text, counts, PASSWORD, keep=_not_secret_value, found=f)
    # 4. 개인정보
    text = _sub(_RRN, text, counts, RRN, found=f)
    text = _sub(_CARD, text, counts, CARD, found=f)
    text = _sub(_CARD_WORD, text, counts, CARD, found=f)
    # 5. IP (줄 맨 앞의 "1.2.3.4 제목" 같은 번호는 둔다)
    text = _sub(_IP, text, counts, IP, found=f,
                keep=lambda m: m.group(1) in _IP_KEEP or _is_heading_number(m) or _is_version(m))
    return text, counts


# 비밀번호 낱말 뒤에 오지만 값이 아닌 말 ("비밀번호는 90일마다", "password: 변경 필요", "암호=SHA-256")
_NOT_SECRET = re.compile(
    r"^(?:\d{1,3}(?:일|회|자|자리|글자|개월|번)?|SHA-?\d+|MD5|AES-?\d*|ARIA|SEED|bcrypt|encrypt\w*|"
    r"null|none|N/?A|TBD|\*+|x{3,}|변경|확인|초기화|\d{4}[-./]\d{1,2}[-./]\d{1,2}\.?)$", re.IGNORECASE)


def _not_secret_value(m: re.Match) -> bool:
    value = m.group(1).rstrip(".")
    return bool(_NOT_SECRET.match(value)) or value.lower() in {"required", "optional", "string", "true", "false"}


def _not_pair(m: re.Match) -> bool:
    """"계정 관리 화면은 /uss/umt …"처럼 계정 낱말 뒤 슬래시가 경로일 때."""
    return bool(re.match(r"^[a-z]{2,5}$", m.group(1)) and re.match(r"^[a-z]+(?:/|\.do)", m.group(2)))


def _is_heading_number(m: re.Match) -> bool:
    line_start = m.string.rfind("\n", 0, m.start(1)) + 1
    before = m.string[line_start:m.start(1)]
    after = m.string[m.end(1):m.end(1) + 2]
    small = all(1 <= int(x) <= 30 for x in m.group(1).split("."))  # 절 번호에는 0이나 큰 수가 없다
    return small and not before.strip() and after.startswith((" ", ". "))


def _is_version(m: re.Match) -> bool:
    """"버전 4.1.0.0", "v1.2.3.4"는 IP가 아니다."""
    return bool(re.search(r"(?:버전|(?<![A-Za-z])(?:version|ver\.?|v))\s*$", m.string[max(0, m.start(1) - 10):m.start(1)], re.IGNORECASE))


def merge_counts(*counters: Counter) -> dict[str, int]:
    total: Counter = Counter()
    for c in counters:
        total.update(c)
    return dict(total)
