"""關鍵字規則：模式、賽事名、名次、隊伍、開放狀態、區域判定。

這個模組是整個系統的正確性核心，全部是純函式 —— 不碰網路、不讀時鐘，
所以「相同輸入必定產生相同輸出」，可以對 fixtures/ 的真實快照做黃金測試。

規則表的調校全部集中在這個檔案。改規則後跑 `python -m pytest tests/`。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

# --------------------------------------------------------------------------
# 模式
# --------------------------------------------------------------------------

# 順序有意義：mania / taiko / catch 都比 std 特殊，要先判。
MODE_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("taiko", re.compile(r"(?i)\btaiko\b|\bTCT\b|\[taiko")),
    ("catch", re.compile(r"(?i)\b(?:osu!?)?catch\b|\bctb\b")),
    ("mania", re.compile(r"(?i)\bo!m\b|\bmania\b")),
    ("std", re.compile(r"(?i)\[std|osu!std|osu!standard|\bstd\b|\bstable\b|lazer")),
]

# 鍵數只在「已判定為 mania」之後才抽，否則 [9k-120.000] 這種名次會被誤認成 9K。
MANIA_KEYS = re.compile(r"(?<![\d#])([1-9])\s*[kK]\b")

MODE_LABEL = {
    "std": "osu!standard",
    "taiko": "osu!taiko",
    "catch": "osu!catch",
    "mania": "osu!mania",
}


def detect_mode(title: str) -> tuple[str, str]:
    """回傳 (mode, confidence)。沒有明確標籤時 fallback 到 std 並標 low。"""
    for name, rx in MODE_RULES:
        if rx.search(title):
            return name, "high"
    return "std", "low"


def detect_mania_keys(title: str) -> Optional[int]:
    m = MANIA_KEYS.search(title)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------
# 名次區間
# --------------------------------------------------------------------------

_NUM = r"\d[\d,.]*\s*[kK]?"
RANGE = re.compile(
    rf"(?i)#?\s*({_NUM})\s*(?:-|–|—|~|to)\s*#?\s*({_NUM}|inf(?:inity)?|inf\.?)\s*\+?"
)

RANK_MIN_YEAR = 1900
RANK_MAX_YEAR = 2100


def _norm_rank(tok: str) -> Optional[int]:
    tok = tok.strip().lower().rstrip(".")
    if tok.startswith("inf"):
        return None
    mult = 1000 if tok.endswith("k") else 1
    digits = re.sub(r"[,.\s]", "", tok.rstrip("k"))
    if not digits.isdigit():
        return None
    return int(digits) * mult


def extract_rank_range(title: str) -> Optional[tuple[Optional[int], Optional[int]]]:
    """解析名次區間，例如 50K-100K / #10,000-#99,999 / #2500-infinity。

    infinity 表達成 None。兩端都落在年份範圍時視為年份（例如 "2026-2027 season"）而丟棄。
    """
    m = RANGE.search(title)
    if not m:
        return None
    lo, hi = _norm_rank(m.group(1)), _norm_rank(m.group(2))
    if lo is None:
        return None
    if hi is not None and RANK_MIN_YEAR <= lo <= RANK_MAX_YEAR and RANK_MIN_YEAR <= hi <= RANK_MAX_YEAR:
        return None  # 這是年份區間，不是名次
    return (lo, hi)


def format_rank_compact(rng: Optional[tuple[Optional[int], Optional[int]]]) -> str:
    """人類看的短版：50000 → '50K'、100000 → '100K'、None → '∞'。"""
    if not rng:
        return ""

    def one(n: Optional[int]) -> str:
        if n is None:
            return "∞"
        if n >= 1000 and n % 1000 == 0:
            return f"{n // 1000}K"
        return f"{n:,}"

    return f"{one(rng[0])}–{one(rng[1])}"


def format_rank_full(rng: Optional[tuple[Optional[int], Optional[int]]]) -> str:
    if not rng:
        return ""

    def one(n: Optional[int]) -> str:
        return "無上限" if n is None else f"{n:,}"

    return f"{one(rng[0])} – {one(rng[1])}"


# --------------------------------------------------------------------------
# 隊伍形式
# --------------------------------------------------------------------------

TEAM = re.compile(r"(?i)\b([1-9])\s*v\s*([1-9])\b|\bts\s*(\d{1,2})\b|team\s*size\s*(\d+)")


def extract_team_formats(title: str) -> tuple[list[str], list[str]]:
    """回傳 (隊伍形式清單, 隊員數清單)。

    刻意回傳清單而非單一值：`[1v1/2v2, team size 2]` 是「或」，
    `[1v1->2v2]` 是賽程進程，都不該被壓成一個值。
    """
    teams: list[str] = []
    sizes: list[str] = []
    for m in TEAM.finditer(title):
        if m.group(1):
            fmt = f"{m.group(1)}v{m.group(2)}"
            if fmt not in teams:
                teams.append(fmt)
        elif m.group(3):
            sizes.append(m.group(3))
        elif m.group(4):
            sizes.append(m.group(4))
    return teams, sizes


# --------------------------------------------------------------------------
# 工作人員帖（要排除）
# --------------------------------------------------------------------------

# 只比對「標題開頭的獨立標籤」或明確的招募標籤。
# 不能用寬鬆的 staff.*reg —— 那會錯殺同時也在徵工作人員的「選手」賽事，
# 例如「UK Catch Tournament 2026 (Staff Wanted)」、「[Staff + Player Regs OPEN]」。
STAFF_TOPIC = re.compile(r"(?i)^\s*\[\s*staff\s*regs?\s*\]|\[\s*staff\s+recruit")


def is_staff_topic(title: str) -> bool:
    return bool(STAFF_TOPIC.search(title))


# --------------------------------------------------------------------------
# 開放狀態
# --------------------------------------------------------------------------

CLOSED = re.compile(
    r"(?i)regs?\s*closed|registrations?\s*closed|sign[ -]?ups?\s*closed|\[\s*closed\s*\]"
)

# (?!\s*rank) 是必要的：否則每個 "Open Rank" 都會被讀成「報名開放」。
OPEN = re.compile(
    r"(?i)regs?\s*open|registrations?\s*open|sign[ -]?ups?\s*open|\[\s*open\s*\]"
    r"|\bopen\b(?!\s*rank)"
)

STATUS_UNKNOWN = "unknown"
STATUS_OPEN = "open"
STATUS_CLOSED = "closed"


def extract_status(title: str) -> str:
    """回傳 open / closed / unknown。

    unknown 是誠實的答案 —— 真實語料中約一半的標題沒有標狀態，不可併入 open。
    """
    if CLOSED.search(title):
        return STATUS_CLOSED
    if OPEN.search(title):
        return STATUS_OPEN
    return STATUS_UNKNOWN


# --------------------------------------------------------------------------
# 區域判定
# --------------------------------------------------------------------------

# 兩字母代碼查表。Bug 修正：正則帶 (?i) 抓到的會是原大小寫（'BR'），
# 查表前務必 .lower()，否則 [BR Only]、[ID Only] 全部會查不到而掉進「待確認」。
CODE2REGION = {
    "tw": "taiwan", "cn": "china", "hk": "hong kong", "mo": "macau",
    "br": "brazil", "vn": "vietnam", "jp": "japan", "id": "indonesia",
    "mn": "mongolia", "ru": "russia", "us": "united states",
    "uk": "united kingdom", "kr": "south korea", "ph": "philippines",
    "my": "malaysia", "sg": "singapore", "th": "thailand", "in": "india",
    "de": "germany", "fr": "france", "pl": "poland", "es": "spain",
    "it": "italy", "pt": "portugal", "mx": "mexico", "cl": "chile",
    "pe": "peru", "ca": "canada", "tr": "turkey", "au": "australia",
    "nz": "new zealand",
}

ALLOWED_REGIONS = {"taiwan", "china", "hong kong", "macau"}

REGION_ALLOW = re.compile(
    r"(?i)\b(tw|taiwan|taiwanese|asia|asian|east asia|sea|southeast asia|apac|"
    r"world cup|worldwide|international|open to all|any country)\b|台灣|臺灣|中文|中國|香港"
)

REGION_DENY_COUNTRY = re.compile(
    r"(?i)\b(danish|denmark|balkan|brazilian|brazil|belgian|belgium|argentina|"
    r"united states|american|uk|united kingdom|british|japan|japanese|vietnam|"
    r"mongolia|indonesia|michigan|colorado|latam|latin america|russia|russian|"
    r"new zealand|australia|ausnz|korea|korean|philippines|malaysia|singapore|"
    r"thailand|india|polish|poland|german|germany|french|france|dutch|netherlands|"
    r"spanish|spain|italian|italy|mexican|mexico|chile|peru|canada|canadian|"
    r"turkey|turkish)\b|日本語|日本"
)

# Bug 修正：語言閘門。`[Ru-speaking only]` 抓不到，
# 因為 \b([a-z]{2})\s+only\b 要求兩個字母後直接接空白 + only。
# （`[SPEAK RU ONLY]` 剛好會被那條撿到，但 ru-speaking 不會。）
REGION_LANG_GATE = re.compile(
    r"(?i)\b(?:speak\s+)?(?:ru|jp|kr|cn|br)[- ]?speak\w*\b"
    r"|\b(?:russian|japanese|korean|chinese|portuguese)[- ]?\s*speaking\b"
    r"|日本語|僅限.*語|只限.*語"
)

REGION_CODE = re.compile(
    r"(?i)\[\s*([a-z]{2})\s*(?:only)?\s*\]|\(\s*([a-z]{2})\s*only\s*\)|\b([a-z]{2})\s+only\b"
)

INCLUDE = "include"
EXCLUDE = "exclude"
REVIEW = "review"


def classify_region(title: str) -> tuple[str, str]:
    """回傳 (include/exclude/review, 原因)。"""
    if REGION_LANG_GATE.search(title):
        return EXCLUDE, "language-gate"
    if REGION_ALLOW.search(title):
        return INCLUDE, "explicit-allow"
    if REGION_DENY_COUNTRY.search(title):
        return EXCLUDE, "named-region"

    codes = [c.lower() for g in REGION_CODE.findall(title) for c in g if c]
    regions = {CODE2REGION.get(c) for c in codes}
    if regions - {None}:
        if regions & ALLOWED_REGIONS:
            return INCLUDE, "code-allow"
        return EXCLUDE, "code-deny"
    if codes:
        # 未知的兩字母代碼（例如 [MN Only] 是 Minnesota 還是 Mongolia？）→ 交給人判。
        return REVIEW, "unknown-code"
    return INCLUDE, "no-region"


# --------------------------------------------------------------------------
# 名稱清洗
# --------------------------------------------------------------------------

_YEAR = re.compile(r"\b(?:19|20)\d{2}\b")

# 沒被括號包住的區域代碼清單，例如 "| CN,HK,TW,MO only |"。
# 刻意不加 (?i)：否則小寫的 no / to / in 會被 [A-Z]{2} 誤抓。
_REGION_CODE_LIST = re.compile(
    r"\b[A-Z]{2}(?:\s*[,/]\s*[A-Z]{2})+\s+only\b|\b[A-Z]{2}\s+only\b"
)

# 「Player and Staff Registrations Open」要**整段**清掉。只清 status 字的話，
# 會留下孤零零的 "Player and Staff" 掛在賽事名尾巴，而賽事名會直接進 FB 標題。
# 主詞限定在這組字彙，才不會把賽事名本身的字吃掉。
_STATUS_SUBJECT = r"(?:\b(?:players?|teams?|staff|fa|entries|applications?|duos?|and)\b[\s,/&+]*)+"

_STATUS_NOISE = re.compile(
    rf"(?i){_STATUS_SUBJECT}\b(?:regs?|registrations?|sign[ -]?ups?)\s+(?:are\s+|is\s+)?(?:open|closed)\b"
    r"|\b(?:regs?|registrations?|sign[ -]?ups?)\s+(?:are\s+|is\s+)?(?:open|closed)\b"
    r"|\bopen\s+rank\b|\bno\s+bws\b|\bbws\b"
)


def clean_name(title: str) -> str:
    """從標題淬取出可讀的賽事名。

    這只是「顯示用」的名稱，不是身分識別 —— 身分一律用 topic_id。
    例如 SMST 84 有兩個主題（50K-100K 與 100K-Infinity），清完會同名。
    """
    t = title
    # 順序很重要：先把名次與隊伍抽掉，才不會把 (Taiko, 1v1, Open Rank)
    # 這種「前括號前綴」裡帶的模式一起清掉。
    t = RANGE.sub(" ", t)
    t = TEAM.sub(" ", t)
    t = re.sub(r"\([^)]*\)", " ", t)
    t = re.sub(r"\[[^\]]*\]", " ", t)
    t = _REGION_CODE_LIST.sub(" ", t)
    t = _STATUS_NOISE.sub(" ", t)
    t = re.sub(r"[|]+", " ", t)
    # 抽掉 TEAM 之後常留下孤立的逗號（"| 1v1, LAN |" → "| , LAN |"）。
    # 只清「前面是空白或行首」的那一種，才不會動到賽事名自己的逗號。
    t = re.sub(r"(^|\s),\s*", r"\1 ", t)
    return re.sub(r"\s+", " ", t).strip(" -–—|·!,.")


def is_low_signal(title: str, rng, teams: list[str]) -> bool:
    """判不出名次、隊伍形式、也沒有年份 → 很可能是情報／討論帖而非比賽。"""
    return rng is None and not teams and not _YEAR.search(title)


# --------------------------------------------------------------------------
# 綜合
# --------------------------------------------------------------------------


@dataclass
class Verdict:
    """一場賽事的抽取結果與收錄判定。"""

    title: str
    mode: str
    mode_confidence: str
    mania_keys: Optional[int]
    name: str
    rank: Optional[tuple[Optional[int], Optional[int]]]
    rank_compact: str
    teams: list[str] = field(default_factory=list)
    team_sizes: list[str] = field(default_factory=list)
    status: str = STATUS_UNKNOWN
    decision: str = INCLUDE
    reason: str = ""
    is_staff_topic: bool = False

    @property
    def mode_label(self) -> str:
        base = MODE_LABEL[self.mode]
        if self.mode == "mania" and self.mania_keys:
            return f"{base} {self.mania_keys}K"
        return base


def analyze(title: str) -> Verdict:
    """對單一標題做完整抽取與判定。"""
    mode, mode_conf = detect_mode(title)
    rng = extract_rank_range(title)
    teams, sizes = extract_team_formats(title)
    status = extract_status(title)

    decision, reason = classify_region(title)
    staff = is_staff_topic(title)

    if staff:
        decision, reason = EXCLUDE, "staff-topic"
    elif decision == INCLUDE and is_low_signal(title, rng, teams):
        decision, reason = REVIEW, "low-signal"
    elif decision == INCLUDE and re.search(r"(?i)\blan\b", title):
        decision, reason = REVIEW, "lan"
    elif decision == INCLUDE and re.search(r"(?i)invitational|tryouts", title):
        decision, reason = REVIEW, "invitational"

    return Verdict(
        title=title,
        mode=mode,
        mode_confidence=mode_conf,
        mania_keys=detect_mania_keys(title) if mode == "mania" else None,
        name=clean_name(title),
        rank=rng,
        rank_compact=format_rank_compact(rng),
        teams=teams,
        team_sizes=sizes,
        status=status,
        decision=decision,
        reason=reason,
        is_staff_topic=staff,
    )


# --------------------------------------------------------------------------
# 首帖內文：報名截止時間
# --------------------------------------------------------------------------

# 截止時間有兩種寫法，而真實語料裡**標籤式比動詞式常見得多**：
#
#   (A) 動詞式：「Registrations will end for both brackets at 23:59 October 16th」
#   (B) 標籤式：「Registrations: 9th - 25th October」
#               「Signups » October 5th - October 18th」
#               「Regs -> September 27th」「玩家報名: Oct 3 ~ Oct 18」
#
# 2026-10-10 對 forum 55 列表上 50 篇首帖實測：只認動詞式時只過得了 8 篇。
# 漏掉的幾乎全是標籤式 —— 主辦把賽程整個貼出來，每一行都是「標籤：日期」，
# 而動詞式那條要求「報名」後面**直接接** end／close／until，接不上就整句漏掉。
#
# 句子抓得到，但把裡面的日期解析成精確時間**不可靠** —— 同一句可能還夾帶賽程日期
# （October 17th / 18th），而且年份通常沒寫。所以 deadline_raw 一律保存，
# deadline_iso 只做低信心猜測；猜不出來就讓草稿顯示原句，不捏造。
_MONTH_NAMES = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"

# 「報名」這個概念的各種寫法。中英夾雜是常態，台灣與中國的賽事尤其如此。
_REG_WORD = (
    r"(?:registrations?|registros?|regs?|sign[ -]?ups?|entries|applications?|報名|报名)"
)

# 只拿來判斷「標籤後面不遠處有沒有日期」，不用來取值 —— 取值是 DATE_IN_TEXT 的事。
# 所以這裡刻意寬鬆（連中文的 10月16日 都算），把關留給 parse_deadline_iso。
# 數字式後面的 (?![\d,]) 是為了擋掉「2-99,999」「1-9,500」這種名次區間。
_DATEISH = (
    r"(?:\d{1,2}\s*(?:st|nd|rd|th)?\s*" + _MONTH_NAMES + r"[a-z]*"
    r"|" + _MONTH_NAMES + r"[a-z]*\.?\s*\d{1,2}(?:st|nd|rd|th)?"
    r"|(?<![\d.,])\d{1,2}\s*[/\-]\s*\d{1,2}(?![\d,])"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日?"
    r")"
)

# 句尾：. ! ? 以及中文的 。！？。
#
# `.` 有一個例外：「September.30」「Sept. 30」的句點是「月.日」的分隔符，不是句尾。
# 2026-10-10 實測踩到過：Sonus Scope 2 的內文寫「Registration: September.30 -
# December.6」，切在句點上會得到「Registration: September」這半截話 —— 看板會
# 把它當成主辦寫的截止句顯示出來。挑錯句子比顯示「未標明」更糟。
_STOP = r"(?:[^.!?。！？]|\.(?=\s*\d))"

DEADLINE_SENTENCE = re.compile(
    r"(?i)(?:"
    # (A) 動詞式
    + _REG_WORD
    + r"\s+(?:will\s+|are\s+|is\s+)?"
    r"(?:end|ends|ending|close|closes|closing|closed|deadline|due|until|open\s+until)"
    # (B) 標籤式：報名的字 → 可有可無的階段詞 → 可有可無的分隔符 → 不遠處要有日期。
    # 最後那個 (?=...) 是必要的守門：「Registrations: OPEN」「Team Regs | Free Agent
    # Regs」這種沒有日期的命中會被它擋掉，否則看板會把「截止那句」顯示成一句跟截止
    # 無關的話 —— 那比顯示「未標明」更糟，讀者會以為主辦就是那樣寫的。
    r"|(?:player\s+|staff\s+|team\s+|free\s+agent\s+)?"
    + _REG_WORD
    + r"(?:\s+(?:phase|period|window|timeline|closes?|ends?|deadline|opens?|starts?|截止))?"
    r"\s*[:：»>~=\-–—]?\s*"
    r"(?=" + _STOP + r"{0,40}?" + _DATEISH + r")"
    r")"
    + _STOP
    + r"{0,160}"
)

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# 只認「日 + 月」或「月 + 日」這種明確寫出月份的格式。
#
# 前面的 (?<![\d:.,]) 是必要的：「at 23:59 October 16th」裡 search() 會先撞到
# `59 October`，把 59 當成「日」→ datetime() 爆掉 → 整個截止時間變 None。
# `12:30 October 16th` 更糟：30 是合法日期，會安靜地解析成 10 月 30 日。
# 要求日期前面不是數字或冒號（也不是千分位的 . 與 ,），才不會把時分讀成日。
#
# 月名與日之間是 \s* 而非 \s+：「September.30」「Sept. 30」這種把句點當分隔符的寫法
# 在語料裡真的出現過（Sonus Scope 2）。`\.?` 後面接 `\s*` 兩者都吃得下；
# 而「Oct2026」不會誤判 —— `\b` 卡在 20 與 26 之間，兩個都是數字，不成立。
DATE_IN_TEXT = re.compile(
    r"(?i)(?<![\d:.,])\b(\d{1,2})(?:st|nd|rd|th)?\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s*(\d{1,2})(?:st|nd|rd|th)?\b"
)

TIME_IN_TEXT = re.compile(r"\b(\d{1,2}):(\d{2})\b")


def find_deadline_sentence(body_text: str) -> Optional[str]:
    """從首帖內文挑出提到報名截止的那一句（原文，不改寫）。

    一個帖子裡常常有好幾處沾到「報名」兩個字。Yimasu's Hidden Tournament 2 就是
    現成的例子：內文先出現「Players must be between 20,000-99,999BWS when
    registrations close」——那只是順帶一提，真正的截止寫在後面的 Schedule 區塊
    （「Registrations » October 5th - October 18th」）。只回第一個命中的話，
    看板上的「主辦寫的截止那句」就會是前面那句廢話。

    所以掃過**所有**命中，優先回傳「句子裡真的有日期」的那一個；都沒有才退回
    第一個。挑錯句子比顯示「未標明」更糟 —— 讀者會以為主辦就是那樣寫的。
    """
    first: Optional[str] = None
    for m in DEADLINE_SENTENCE.finditer(body_text or ""):
        hit = m.group(0).strip()
        if first is None:
            first = hit
        if DATE_IN_TEXT.search(hit):
            return hit
    return first


# 日期區間的連接詞。中英西越語的寫法都在真實語料裡出現過：
#   「Registrations: 28 September - 11 October」（-）
#   「Regs: September 27th -> October 10th」（->）
#   「Player Registrations ■ 18th September ➔ 9th October」（➔，裝飾性箭頭）
#   「Registrations: 9th to 25th October」（to）
#   「Inicio y Fin de regs Mie 23/09 a Sab 17/10」（a，西語）
#
# 箭頭那幾個是 2026-10-10 實測才發現的 —— 漏掉 ➔ 會讓「18th September ➔ 9th
# October」被讀成 9 月 18 日，早了整整三週。
_RANGE_JOIN = re.compile(
    r"(?i)(?:[-–—~→➔➜➝➞➤⟶]|->|\bto\b|\buntil\b|\bthrough\b|\ba\b|至|到)"
)

# 「between 19 September and 27 September」也是一種區間，但連接詞是 and。
# and 單獨出現時多半是**列舉**而不是區間 —— 「Registrations close Oct 16 and
# screening starts Oct 20」的截止是 10/16，不是 10/20。所以只有在 and 前面出現過
# between／from／desde 這類字時，才把它當成區間連接詞。
_RANGE_AND = re.compile(r"(?i)\band\b")
_RANGE_LEAD = re.compile(r"(?i)\b(?:between|entre|from|desde)\b")


def _deadline_date_candidates(raw: str) -> list[tuple[int, int, int, int]]:
    """依序吐出句子裡的 (日, 月, 起點, 終點)，由呼叫端驗證合法性。

    位置是給 parse_deadline_iso 分辨「這是單日還是區間」用的。
    """
    out: list[tuple[int, int, int, int]] = []
    for m in DATE_IN_TEXT.finditer(raw):
        if m.group(1):
            out.append(
                (int(m.group(1)), _MONTHS[m.group(2).lower()[:3]], m.start(), m.end())
            )
        else:
            out.append(
                (int(m.group(4)), _MONTHS[m.group(3).lower()[:3]], m.start(), m.end())
            )
    return out


def parse_deadline_iso(raw: Optional[str], created_at: Optional[str]) -> Optional[str]:
    """從已抓到的截止句推測 ISO 時間；猜不出來回 None（呼叫端顯示原句，不捏造）。

    這是 `deadline_raw` 的純函式 —— 輸入只有那句話與發文時間。所以改進這裡的規則後，
    已經抓過的舊資料只要重跑就會自動修正，不必等重抓主題頁。
    """
    if not raw:
        return None

    year = None
    if created_at:
        try:
            year = int(created_at[:4])
        except ValueError:
            year = None
    if year is None:
        return None

    hm = TIME_IN_TEXT.search(raw)
    hour, minute = (int(hm.group(1)), int(hm.group(2))) if hm else (23, 59)

    cands = _deadline_date_candidates(raw)

    # 區間寫法（「Registrations: 28 September - 11 October」）的截止是**後面**那個
    # 日期；單日寫法（「Registrations Close: October 25th」）就是那一個。
    # 分辨方式：看前兩個候選之間夾的是不是連接詞，是的話往後推一格。
    #
    # 少了這一步，「Registrations: 28 September - 11 October」會被讀成 9 月 28 日 ——
    # 早了快三週，比賽還在報名就被看板標成「表定已截止」。
    start = 0
    if len(cands) >= 2:
        gap = raw[cands[0][3] : cands[1][2]]
        joined = _RANGE_JOIN.search(gap) or (
            _RANGE_LEAD.search(raw[: cands[0][2]]) and _RANGE_AND.search(gap)
        )
        if joined:
            start = 1

    # 候選一律用 datetime() 驗過才採用：不合法就換下一個，而不是整句放棄。
    # 這是第二層防線 —— 第一層是 DATE_IN_TEXT 的 lookbehind。
    dt = None
    for day, month, _start, _end in cands[start:]:
        if not 1 <= day <= 31:
            continue
        try:
            dt = datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
        except ValueError:
            continue
        break
    if dt is None:
        return None

    # 年份通常沒寫，取發文年份；若因此變成過去，代表跨年了。
    if created_at:
        try:
            created = datetime.fromisoformat(created_at)
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if dt < created.replace(hour=0, minute=0):
                dt = dt.replace(year=year + 1)
        except ValueError:
            pass

    return dt.isoformat()


def extract_deadline(
    body_text: str, created_at: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """回傳 (原始句子, 猜測的 ISO 時間或 None)。"""
    raw = find_deadline_sentence(body_text)
    if raw is None:
        return None, None
    return raw, parse_deadline_iso(raw, created_at)
