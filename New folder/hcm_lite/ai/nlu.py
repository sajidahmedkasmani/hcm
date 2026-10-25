"""Deterministic natural-language helpers (English + Roman Urdu).

Used by the offline provider (no API key / LLM outage) and as pure, unit-tested utilities:
relative date parsing ("next Monday and Tuesday"), leave-type detection, period detection.
"""

from __future__ import annotations

import re
from datetime import date, timedelta

WEEKDAYS: dict[str, int] = {
    "monday": 0, "mon": 0, "peer": 0, "pir": 0,
    "tuesday": 1, "tue": 1, "tues": 1, "mangal": 1,
    "wednesday": 2, "wed": 2, "budh": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "jumeraat": 3, "jumerat": 3,
    "friday": 4, "fri": 4, "jumma": 4, "juma": 4,
    "saturday": 5, "sat": 5, "hafta": 5,
    "sunday": 6, "sun": 6, "itwar": 6, "itwaar": 6,
}
MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9, "october": 10, "oct": 10, "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}
ROMAN_URDU_MARKERS = {
    "kitne", "kitni", "kitna", "hai", "hain", "mera", "meri", "mere", "mujhe", "chutti", "chuttiyan",
    "chhutti", "baqi", "baki", "kaun", "kon", "aaj", "kal", "parson", "tankhwah", "tankhah", "der",
    "se", "ki", "ka", "ke", "nahi", "chahiye", "batao", "bataen", "thay", "tha", "log", "hafte",
    "mahine", "saal", "shaadi", "shadi", "bimar", "bukhar", "aya", "aye", "dafter", "daftar",
}
LEAVE_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "sick": ("sick", "ill", "illness", "fever", "flu", "doctor", "hospital", "medical", "surgery", "unwell",
             "bimar", "beemar", "bukhar", "dentist", "migraine", "covid"),
    "annual": ("vacation", "holiday trip", "trip", "travel", "annual", "umrah", "hajj", "tour", "abroad",
               "visit home", "honeymoon"),
    "unpaid": ("unpaid", "without pay", "leave without pay", "lwp"),
    "casual": ("wedding", "shaadi", "shadi", "family", "personal", "function", "event", "urgent", "errand",
               "casual", "funeral", "moving", "exam", "ceremony", "mehndi", "baraat", "walima"),
}


def normalize(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", text).strip()


def tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9']+", normalize(text))


def is_roman_urdu(text: str) -> bool:
    words = set(tokens(text))
    hits = words & ROMAN_URDU_MARKERS
    return len(hits) >= 2 or bool(words & {"chutti", "chuttiyan", "chhutti", "tankhwah", "tankhah", "kitni", "kitne"})


# ------------------------------------------------------------------ weekdays & dates
def monday_of_next_week(today: date) -> date:
    return today + timedelta(days=7 - today.weekday())


def resolve_weekday(today: date, weekday: int, qualifier: str | None) -> date:
    """'next X' = X in the next calendar week (Mon-Sun); 'this X' = X this week (or next if passed);
    bare 'X' = the next occurrence strictly after today."""
    if qualifier == "next":
        return monday_of_next_week(today) + timedelta(days=weekday)
    delta = (weekday - today.weekday()) % 7
    if qualifier == "this":
        return today + timedelta(days=delta if delta else 0)
    return today + timedelta(days=delta or 7)


def _roll_year(today: date, month: int, day: int) -> date | None:
    try:
        d = date(today.year, month, day)
    except ValueError:
        return None
    if d < today - timedelta(days=7):
        try:
            d = date(today.year + 1, month, day)
        except ValueError:
            return None
    return d


def _explicit_dates(text: str, today: date) -> list[date]:
    found: list[tuple[int, date]] = []
    for m in re.finditer(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        try:
            found.append((m.start(), date(int(m[1]), int(m[2]), int(m[3]))))
        except ValueError:
            pass
    month_names = "|".join(sorted(MONTHS, key=len, reverse=True))
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({month_names})\b", text):
        d = _roll_year(today, MONTHS[m[2]], int(m[1]))
        if d:
            found.append((m.start(), d))
    for m in re.finditer(rf"\b({month_names})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", text):
        d = _roll_year(today, MONTHS[m[1]], int(m[2]))
        if d:
            found.append((m.start(), d))
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", text):  # dd/mm[/yyyy] (Pakistan)
        try:
            year = int(m[3]) if m[3] else None
            if year is not None and year < 100:
                year += 2000
            d = date(year, int(m[2]), int(m[1])) if year else _roll_year(today, int(m[2]), int(m[1]))
            if d:
                found.append((m.start(), d))
        except ValueError:
            pass
    # "5 to 7 november" -> the first number borrows the month of the second
    for m in re.finditer(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s*(?:to|-|till|until)\s*(\d{{1,2}})(?:st|nd|rd|th)?\s+({month_names})\b", text):
        d = _roll_year(today, MONTHS[m[3]], int(m[1]))
        if d:
            found.append((m.start(), d))
    found.sort(key=lambda x: x[0])
    return [d for _, d in found]


def _add_working_days(start: date, n: int, weekend: frozenset[int]) -> date:
    d, counted = start, 0
    while True:
        if d.weekday() not in weekend:
            counted += 1
            if counted >= n:
                return d
        d += timedelta(days=1)


def parse_leave_dates(text: str, today: date, weekend: frozenset[int] = frozenset({5, 6})) -> tuple[date, date] | None:
    """Best-effort extraction of a (start, end) date range from a sentence."""
    t = normalize(text)
    dates: list[date] = []

    explicit = _explicit_dates(t, today)
    dates.extend(explicit)

    if re.search(r"\bday after tomorrow\b|\bparson\b|\bparso\b", t):
        dates.append(today + timedelta(days=2))
        if re.search(r"\btomorrow and (the )?day after\b", t):
            dates.append(today + timedelta(days=1))
    elif re.search(r"\btomorrow\b|\bkal\b", t):
        dates.append(today + timedelta(days=1))
    if re.search(r"\btoday\b|\baaj\b", t) and not explicit:
        dates.append(today)

    if re.search(r"\bnext week\b|\bagle hafte\b", t) and not re.search(r"next week'?s? \w+day", t):
        has_weekday = any(re.search(rf"\b{w}\b", t) for w in WEEKDAYS if len(w) > 3)
        if not has_weekday:
            mon = monday_of_next_week(today)
            work = [mon + timedelta(days=i) for i in range(7) if (mon + timedelta(days=i)).weekday() not in weekend]
            if work:
                dates.extend([work[0], work[-1]])

    weekday_names = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
    anchor: date | None = None
    for m in re.finditer(rf"\b(?:(next|this|coming|agle|is)\s+)?({weekday_names})\b", t):
        word = m[2]
        if word in {"sat", "sun", "mar", "hafta"} and not m[1]:
            continue  # ambiguous short words
        qualifier = {"next": "next", "agle": "next", "this": "this", "is": "this", "coming": None}.get(m[1] or "", None)
        wd = WEEKDAYS[word]
        if anchor is not None and qualifier is None:
            d = anchor + timedelta(days=(wd - anchor.weekday()) % 7)  # continue from the previous weekday
        else:
            d = resolve_weekday(today, wd, qualifier)
        dates.append(d)
        anchor = d

    if not dates:
        return None
    start, end = min(dates), max(dates)
    m = re.search(r"\bfor (\d{1,2}|two|three|four|five|six|seven|ten) (?:working )?days?\b", t)
    if m and start == end:
        words = {"two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "ten": 10}
        n = int(m[1]) if m[1].isdigit() else words[m[1]]
        end = _add_working_days(start, n, weekend)
    return start, end


def detect_leave_type(text: str, available: list[str] | None = None) -> str:
    t = normalize(text)
    for code in ("unpaid", "sick", "annual", "casual"):
        if any(re.search(rf"\b{re.escape(k)}\b", t) for k in LEAVE_TYPE_KEYWORDS[code]):
            if available is None or code in available:
                return code
    if available:
        return "casual" if "casual" in available else available[0]
    return "casual"


def extract_reason(text: str) -> str:
    t = text.strip()
    m = re.search(r"\b(?:for|because of|due to|as|ki wajah se|ke liye)\s+(.+)$", t, flags=re.IGNORECASE)
    reason = m[1] if m else t
    reason = re.sub(r"^(my|meri|mere|mera)\s+", "", reason, flags=re.IGNORECASE).strip(" .!?")
    if len(reason) < 3:
        reason = t
    return (reason[:1].upper() + reason[1:])[:200]


# ------------------------------------------------------------------ periods for analytics questions
def detect_period(text: str) -> str | None:
    t = normalize(text)
    rules = [
        (r"\btoday\b|\baaj\b", "today"),
        (r"\byesterday\b|\bkal\b.*\b(tha|thay|the|thi)\b", "yesterday"),
        (r"\blast week\b|\bpichl[ae] hafte\b", "last_week"),
        (r"\bthis week\b|\bis hafte\b", "this_week"),
        (r"\blast month\b|\bpichl[ae] mahine\b", "last_month"),
        (r"\bthis month\b|\bis mahine\b|\bmonth'?s\b|\bmonthly\b", "this_month"),
        (r"\bthis year\b|\bis saal\b|\byear\b", "this_year"),
    ]
    for pattern, period in rules:
        if re.search(pattern, t):
            return period
    return None


def detect_month(text: str, today: date) -> str | None:
    t = normalize(text)
    m = re.search(r"\b(\d{4})-(\d{2})\b", t)
    if m:
        return f"{m[1]}-{m[2]}"
    names = "|".join(sorted(MONTHS, key=len, reverse=True))
    m = re.search(rf"\b({names})\b(?:\s+(\d{{4}}))?", t)
    if m and m[1] not in {"may", "mar"}:
        year = int(m[2]) if m[2] else today.year
        month = MONTHS[m[1]]
        if not m[2] and month > today.month:
            year -= 1
        return f"{year:04d}-{month:02d}"
    if re.search(r"\blast month\b|\bpichl[ae] mahine\b", t):
        first = today.replace(day=1) - timedelta(days=1)
        return f"{first.year:04d}-{first.month:02d}"
    if re.search(r"\bthis month\b|\bis mahine\b", t):
        return f"{today.year:04d}-{today.month:02d}"
    return None


def extract_department(text: str) -> str | None:
    t = normalize(text)
    m = re.search(r"\b(?:in|from|of)\s+(?:the\s+)?([a-z][a-z &/-]{1,40}?)\s+(?:department|dept|team)\b", t)
    if m:
        return m[1].strip()
    m = re.search(r"\b(?:in|from)\s+(?:the\s+)?([a-z][a-z &/-]{1,40}?)\s*(?:\?|$|\bthis\b|\btoday\b|\blast\b)", t)
    if m and m[1].strip() not in {"office", "company", "total", "all", "leave", "the company"}:
        return m[1].strip()
    m = re.search(r"\b([a-z][a-z&/-]{1,30})\s+(?:department|dept)\b", t)
    if m and m[1] not in {"which", "each", "every", "the", "my"}:
        return m[1]
    m = re.search(r"\b([a-z][a-z&/-]{2,30})\s+(?:department\s+|dept\s+)?mein\b", t)  # Roman Urdu: "sales mein"
    if m and m[1] not in {"company", "office", "daftar", "dafter", "is", "us", "aaj"}:
        return m[1]
    return None


def extract_person(text: str) -> str | None:
    """Name of another person in questions like "What is Ali Khan's salary?" or "salary of Sara"."""
    raw = text.strip()
    patterns = [
        r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)'s\b",
        r"\b(?:salary|pay|payslip|tankhwah|tankhah)\s+(?:of|for)\s+([A-Za-z]+(?:\s+[A-Za-z]+)?)",
        r"\b([A-Za-z]+(?:\s+[A-Za-z]+)?)\s+(?:ki|ka|ke)\s+(?:salary|tankhwah|tankhah|pay)\b",
        r"\b([a-z]+(?:\s+[a-z]+)?)'s\s+(?:salary|pay|payslip)\b",
        r"\b(?i:how much (?:does|do|is))\s+([A-Za-z]+(?:\s+[A-Z][a-z]+)?)\s+(?i:earn|make|get|paid)",
    ]
    stop = {"my", "i", "me", "what", "whats", "what's", "the", "company", "this", "month", "today", "mera",
            "meri", "mujhe", "employee", "colleague", "your", "our", "his", "her", "their", "net", "gross",
            "is", "was", "are", "tell", "show", "about", "how", "much", "does", "do", "get", "earn", "earns"}
    for p in patterns:
        m = re.search(p, raw)
        if m:
            name = m[1].strip()
            words = [w for w in name.split() if w.lower() not in stop]
            if words:
                return " ".join(words)
    return None
