#!/usr/bin/env python3
"""
AION 2 news relay: RSS (X feed) -> categorise -> time conversion -> Discord webhook.

Only important posts are relayed:
  MAINTENANCE  priority post (red, optional role ping)
  EVENT        upcoming events / campaigns (green)
  UPDATE       patch notes / content updates (blue)
Everything else (memes, retweets, replies, fan art shares...) is skipped.

Env vars (GitHub Actions secrets, or a chmod 600 env file):
  FEED_URL             RSS feed(s) of AION 2 X accounts (e.g. from rss.app), one per line
  DISCORD_WEBHOOK_URL  Discord channel webhook (treat as a secret)
  DISCORD_ROLE_ID      Optional: role to @ping on maintenance posts
  CATEGORIES           Optional: which to relay (default: maintenance,event,update)
  SOURCE_TZ            Fallback tz if the tweet doesn't say one (default: KST)
  STATE_FILE           Where seen post IDs are stored (default: ./aion2_seen.json)
  REMINDER_MINUTES     Optional: how long before maintenance the reminder goes out (default: 60)
  PREVIEW, PREVIEW_COUNT  Optional: print what the newest tweets would post as; post/save nothing
  LAST_DISPATCH        Set by the workflow on GitHub's scheduled runs (15-minute trigger check)

Deps: pip install feedparser requests
"""
import hashlib
import html
import json
import os
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import feedparser
import requests

# One or more feeds: put each URL on its own line (or separate with commas/spaces).
FEED_URLS = [u for u in re.split(r"[\s,]+", os.environ.get("FEED_URL", "")) if u]
WEBHOOK = os.environ.get("DISCORD_WEBHOOK_URL")
SOURCE_TZ = os.environ.get("SOURCE_TZ", "KST").upper()
STATE_FILE = os.environ.get("STATE_FILE", "aion2_seen.json")

ROLE_ID = os.environ.get("DISCORD_ROLE_ID", "").strip()
ENABLED = {c.strip().lower() for c in os.environ.get("CATEGORIES", "maintenance,event,update").split(",")}
# PREVIEW=1: print what the newest tweets of each feed would post as. Posts and saves nothing.
PREVIEW = os.environ.get("PREVIEW", "").strip().lower() in ("1", "true", "yes")
try:
    PREVIEW_PER_FEED = max(1, int(float(os.environ.get("PREVIEW_COUNT") or 3)))
except ValueError:
    PREVIEW_PER_FEED = 3
USER_AGENT = "aion2-relay (+https://github.com/Jagomeiister/aion2-relay)"
# "⏰ Maintenance starts in ~N min" goes out this long before a maintenance (45-60 min with
# 15-minute runs). No reminder if the announcement itself came less than 15 min before that.
try:
    REMINDER_MINUTES = max(15, int(float(os.environ.get("REMINDER_MINUTES") or 60)))
except ValueError:
    REMINDER_MINUTES = 60
REMINDER_MIN_LEAD = 15

# Checked in order: first match wins, so maintenance beats event/update.
# English / Korean / Japanese keywords.
CATEGORIES = [
    {
        "key": "maintenance",
        "pattern": re.compile(
            r"maint(enance)?|downtime|server\s*(down|outage|restart|issue)|emergency|hotfix|"
            r"점검|서버\s*(장애|오류)|긴급|メンテ|障害", re.I),
        "title": "🚨 MAINTENANCE — servers going down",
        "color": 0xE74C3C,
        "ping": True,
    },
    {
        "key": "event",
        "pattern": re.compile(
            r"\bevents?\b|festival|festivit(?:y|ies)|celebrat|campaign|login reward|log-?in bonus|attendance|double (xp|exp|drop)|"
            r"limited[- ]time|giveaway|giv(?:e|es|ing) away|\bgifts?\b|livestream|live stream|coupon|redeem|"
            r"loot drop|\brewards?\b|present items?|"
            # "FREE OUTFIT", "free pet"; not "first move is free" (a transfer) or "Play on Steam for free" (an ad)
            r"\bfree\s+(?:\w+\s+)?(?:pets?|outfits?|costumes?|skins?|mounts?|items?|tickets?|gifts?|rewards?|wings?)\b|"
            r"이벤트|출석|쿠폰|방송|선물|무료|보상|イベント|キャンペーン|配信|プレゼント|配布|ログインボーナス|無料", re.I),
        "title": "🎉 Upcoming event",
        "color": 0x2ECC71,
        "ping": False,
    },
    {
        "key": "update",
        "pattern": re.compile(
            r"patch notes?|\bupdate\b|new (season|class|dungeon|zone|content)|season \d|"
            r"\bserver\s+(?:transfers?|moves?|migrations?)\b|\bnew servers?\b|\bmatching servers?\b|"
            r"\baddition of (?:new )?servers?\b|\badd(?:s|ing|ed)?\s+(?:new\s+)?servers?\b|\bservers?\s+(?:addition|expansion)\b|"
            r"업데이트|패치|시즌|서버\s*(?:이전|이동|추가|증설)|"
            r"アップデート|パッチ|サーバー(?:移動|移転|追加|増設)|マッチングサーバー|キャラクター移動", re.I),
        "title": "📜 Game update",
        "color": 0x3498DB,
        "ping": False,
    },
]
# Never relay these even if a keyword matches
SKIP = re.compile(r"^(RT\b|R to @|@\w+)")
# "Maintenance" used only as a deadline isn't a maintenance notice: "log in before the start of
# regular maintenance on Oct 14", "until the next maintenance", "定期メンテナンス開始前までに",
# "정기 점검 전까지". These phrases are blanked out before matching, so a giveaway can't post
# as a red maintenance alert. Real notices ("Weekly Maintenance", "under maintenance",
# "maintenance will be held 10:00-14:00") don't use this form and still match.
INCIDENTAL_MAINT_RE = re.compile(
    r"\b(?:before|until|till|after|following|prior to|ahead of|up to)\s+(?:the\s+)?"
    r"(?:(?:start|beginning|end|completion)\s+of\s+(?:the\s+)?)?"
    r"(?:(?:this|next)\s+)?(?:\w+['’]s\s+)?"
    r"(?:(?:next|regular|scheduled|weekly|periodic|routine|upcoming|planned)\s+)*maint(?:enance)?\b"
    r"|(?:定期|臨時)?メンテ(?:ナンス)?(?:開始|終了)?(?:前|後|まで|以降|明け)"
    r"|(?:정기|임시)?\s*점검\s*(?:전까지|이전|이후|전|후|까지)",
    re.I)


def classify(text: str):
    if SKIP.search(text):
        return None
    text = INCIDENTAL_MAINT_RE.sub(" ", text)
    for cat in CATEGORIES:
        if cat["key"] in ENABLED and cat["pattern"].search(text):
            return cat
    return None


# "Events" that are really region-only promotions: social-media prize draws, shop and café
# tie-ins, real-world collaborations. Only the opening lines are checked, so a round-up of in-game
# events that lists one such campaign further down still posts. Never applied to maintenance/updates.
PROMO_RE = re.compile(
    r"follow\s*(?:&|and|＆)\s*re(?:post|tweet)|phorolipo|lottery|prize draw|\bwinners?\b|gift card|\bamazon\b|"
    r"\d[\d,]*\s*yen\b|wishlist|\bmerch(?:andise)?\b|\bgoods\b|\b(?:pc|game|partner)\s+shops?\b|"
    r"facilit(?:y|ies)|nationwide|\bbung(?:ee|y)\b|caf[eé]s?\b|highlight video|already ended|"
    r"抽選|当選|フォロー|リポスト|フォロリポ|グッズ|店舗|カフェ|全国|ギフト券|円分|추첨|당첨|팔로우|굿즈",
    re.I)
PROMO_HEAD_LINES = 5


def is_promo(text: str) -> bool:
    head = [line for line in text.splitlines() if line.strip()][:PROMO_HEAD_LINES]
    return bool(PROMO_RE.search("\n".join(head)))


# What a maintenance tweet says about the maintenance, checked in this order. A plain announcement
# (none of these) is the red "servers going down" post: the only kind that pings.
CANCEL_RE = re.compile(r"cancel|call(ed)? off|취소|중지|中止|キャンセル", re.I)
COMPLETE_RE = re.compile(
    r"\bhas (?:been )?(?:completed|finished|ended)\b|\b(?:is|are) now (?:complete|over|open|online|back)\b|"
    r"\bservers? (?:is|are|has|have|has been|have been) (?:now )?(?:re)?opened?\b|"
    r"\b(?:re)?opened the servers?\b|\bcan now play (?:the game )?(?:as usual|normally)\b|"
    # Past tense is a fact even after "when/after" ("...the server was opened at 19:30"); see says().
    # No bare "completed as scheduled": "is expected to be completed as scheduled" is an early-end notice.
    r"(?P<past>\b(?:was|were) (?:successfully )?(?:completed|finished)\b|\bservers? (?:was |were )?(?:re)?opened\b)|"
    r"終了しました|完了しました|終了いたしました|完了いたしました|종료되었습니다|완료되었습니다", re.I)
EARLY_RE = re.compile(
    r"\bearly end\b|\bend(?:s|ed|ing)? early\b|\bearlier than (?:planned|scheduled|announced|originally)|"
    r"早期終了|前倒し|조기\s*종료", re.I)
EXTEND_RE = re.compile(  # a change ("has been extended to 16:00"), not an "Extended Maintenance" notice
    r"\b(?:has|have) been extended\b|\b(?:will be|is being) extended\b|\bextended (?:to|until|by)\b|"
    r"\bextension of\b|延長(?:いた)?し(?:ます|ました)|延長となり|연장(?:됩니다|되었습니다|합니다)", re.I)
RESCHEDULE_RE = re.compile(
    r"\bpostpone[ds]?\b|\breschedule[ds]?\b|\bdelayed\b|延期|日程(?:を)?変更|연기|일정\s*변경", re.I)
MAINT_STATUS = [  # (status, pattern, title, colour)
    ("cancelled", CANCEL_RE, "✅ Maintenance cancelled", 0xF1C40F),
    ("complete", COMPLETE_RE, "✅ Maintenance complete — servers are back", 0x2ECC71),
    ("early", EARLY_RE, "⏩ Maintenance ending early", 0x2ECC71),
    ("extended", EXTEND_RE, "⏳ Maintenance extended", 0xE67E22),
    ("rescheduled", RESCHEDULE_RE, "📅 Maintenance rescheduled", 0xE67E22),
]
# "Once maintenance has ended, ..." describes the future, not what happened.
CONDITIONAL_RE = re.compile(r"\b(?:once|after|when|until|before|if)\b[^.!?\n]{0,30}$", re.I)


def says(rx, text: str) -> bool:
    """rx matches somewhere that isn't inside a "once/after/when ..." clause. A past-tense match
    (the pattern's "past" group) counts even there: "...after the work was completed" is a fact."""
    return any(m.groupdict().get("past") or not CONDITIONAL_RE.search(text[max(0, m.start() - 40):m.start()])
               for m in rx.finditer(text))


def maint_status(cat, text: str):
    """The MAINT_STATUS key a maintenance tweet announces, or None for a plain announcement."""
    if cat["key"] != "maintenance":
        return None
    return next((key for key, rx, _, _ in MAINT_STATUS if says(rx, text)), None)


# rss.app renders tweets as X embeds. A trailer ends the text: "— AION2 (@AION2_JP)Oct 9, 2026"
# (or "— @fan Oct 9, 2026" on a retweet). A quoted tweet is glued in as
# "...#Aion2AION2 (@AION2_JP)<quoted text>— https://x.com/AION2_JP/status/<id>—".
TRAILER_RE = re.compile(
    r"\s*—\s*(?:[^\n—]{0,60}?\(@\w{1,15}\)|@\w{1,15})\s*[A-Z][a-z]{2,8}\.? \d{1,2}, \d{4}\s*$")
QUOTE_END_RE = re.compile(r"—\s*(https?://(?:x|twitter)\.com/(\w{1,15})/status/\d+)\s*—?\s*$")


def feed_identity(source: str):
    """("AION2", "AION2_JP") from a feed title like "AION2 (@AION2_JP) / Twitter"; ("", "") if absent."""
    m = re.match(r"\s*(.*?)\s*\(@(\w{1,15})\)", source or "")
    return (m.group(1), m.group(2)) if m else ("", "")


def split_quote(text: str, own_name: str = ""):
    """(the tweet's own text, quoted tweet text, quoted tweet URL); "" and None without a quote."""
    m = QUOTE_END_RE.search(text)
    head = text.rfind(f"(@{m.group(2)})", 0, m.start()) if m else -1
    if head < 0:
        return text, "", None
    own = text[:head].rstrip()
    if own_name and own.endswith(own_name):  # the quoted author's name is glued on: "#Aion2AION2"
        own = own[: -len(own_name)].rstrip()
    quote = text[head + len(m.group(2)) + 3 : m.start()].strip()
    return own, quote, m.group(1)

TZ_ALIASES = {
    "KST": "Asia/Seoul",
    "JST": "Asia/Tokyo",
    "UTC": "UTC",
    "GMT": "UTC",
    "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles",
    "EST": "America/New_York",
    "EDT": "America/New_York",
    "CET": "Europe/Berlin",
    "CEST": "Europe/Berlin",
    "BST": "Europe/London",
    "AEST": "Australia/Brisbane",
    "SGT": "Asia/Singapore",
}
TARGETS = [
    ("Brisbane", ZoneInfo("Australia/Brisbane")),   # AEST, no daylight saving
    ("New Zealand", ZoneInfo("Pacific/Auckland")),  # NZST/NZDT auto-switches
]

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
MONTH_NAME = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
# Each date regex yields (year or None, month, day). Lookarounds stop version
# numbers like "1.5.2" or times like "10:30" being read as dates.
NUM_DATE_RE = re.compile(r"(?<![\d.:/])(?:(\d{4})[./-])?(\d{1,2})[./-](\d{1,2})(?![\d:]|\.\d)")
CJK_DATE_RE = re.compile(r"(?:(\d{4})\s*[년年]\s*)?(\d{1,2})\s*[월月]\s*(\d{1,2})")
NAME_MD_RE = re.compile(r"\b" + MONTH_NAME + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", re.I)
NAME_DM_RE = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + MONTH_NAME + r"(?:,?\s+(\d{4}))?", re.I)
# Digit lookarounds instead of \b: \b fails between "00" and "부터"/"から".
TIME_RE = re.compile(r"(?<![\d:])([01]?\d|2[0-4]):([0-5]\d)(?![\d:])(?:\s*([ap])\.?m\b\.?)?", re.I)
TZ_RE = re.compile(
    r"\b(?:UTC|GMT)\s*([+-])\s*(\d{1,2})(?::?([0-5]\d))?|\b(" + "|".join(TZ_ALIASES) + r")\b", re.I)
# Generic US zones ("1:00 PM PT"); capitals only, so they can't match inside ordinary words.
SHORT_TZ = {"PT": "America/Los_Angeles", "ET": "America/New_York", "CT": "America/Chicago"}
SHORT_TZ_RE = re.compile(r"\b(PT|ET|CT)\b")
NEXT_TO_TIME_RE = re.compile(r"[\s(（]*")  # what may sit between a time and its zone: "10:00 (KST)"


def clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def find_dates(text: str, ref: date):
    """[(start, end, date)] for every valid date in the text, in order. Invalid matches
    ("Season 2-30") are skipped, not fatal."""
    found = []
    for rx in (NUM_DATE_RE, CJK_DATE_RE):
        found += [(m.start(), m.end(), m.group(1), m.group(2), m.group(3)) for m in rx.finditer(text)]
    found += [(m.start(), m.end(), m.group(3), MONTHS[m.group(1)[:3].lower()], m.group(2))
              for m in NAME_MD_RE.finditer(text)]
    found += [(m.start(), m.end(), m.group(3), MONTHS[m.group(2)[:3].lower()], m.group(1))
              for m in NAME_DM_RE.finditer(text)]
    out, taken_until = [], -1
    for start, end, year, month, day in sorted(found, key=lambda f: (f[0], -f[1])):
        if start < taken_until:
            continue  # overlaps a date already found
        try:
            d = date(int(year) if year else ref.year, int(month), int(day))
            if not year:  # no year given: pick the one nearest the tweet (Dec tweet about "1/5")
                if d < ref - timedelta(days=180):
                    d = d.replace(year=d.year + 1)
                elif d > ref + timedelta(days=180):
                    d = d.replace(year=d.year - 1)
        except ValueError:
            continue
        out.append((start, end, d))
        taken_until = end
    return out


def find_date(text: str, ref: date) -> date | None:
    """First valid date in the text."""
    dates = find_dates(text, ref)
    return dates[0][2] if dates else None


def zone_tokens(text: str):
    """[(start, end, tzinfo, label)] for every time zone named in the text, in order."""
    out = []
    for m in TZ_RE.finditer(text):
        if m.group(1):  # explicit offset, e.g. "(UTC+9)"
            offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
            label = f"UTC{m.group(1)}{int(m.group(2))}" + (f":{m.group(3)}" if m.group(3) else "")
            out.append((m.start(), m.end(), timezone(offset if m.group(1) == "+" else -offset, label), label))
        else:
            label = m.group(4).upper()
            out.append((m.start(), m.end(), ZoneInfo(TZ_ALIASES[label]), label))
    out += [(m.start(), m.end(), ZoneInfo(SHORT_TZ[m.group(1)]), m.group(1)) for m in SHORT_TZ_RE.finditer(text)]
    return sorted(out, key=lambda z: z[0])


def blank(text: str, spans) -> str:
    """Overwrite the given (start, end) spans with spaces, keeping every other position."""
    chars = list(text)
    for a, b in spans:
        chars[a:b] = " " * (b - a)
    return "".join(chars)


# What may sit between two times for them to count as one range: "19:45-20:45", "10:00 ~ 14:00",
# "10 am to 2 pm", "10:00から14:00", "10:00부터 14:00".
RANGE_GAP_RE = re.compile(r"\s*(?:-|–|—|~|～|〜|to|until|till|から|부터)\s*", re.I)


def parse_times(text: str, published: datetime | None, default_tz: str | None = None):
    """Return (zone label, [(start, end or None)]) for the times in a post, repeats removed.
    Each time uses the zone written right after it ("1:00 PM PT / 22:00 CET"), else the post's
    first zone, else default_tz/SOURCE_TZ; and the nearest date written before it."""
    zones = zone_tokens(text)
    if zones:
        dflt_tz, dflt_label = zones[0][2], zones[0][3]
    else:
        dflt_label = default_tz or SOURCE_TZ
        dflt_tz = ZoneInfo(TZ_ALIASES.get(dflt_label, "Asia/Seoul"))
    published = published or datetime.now(timezone.utc)
    dates = find_dates(text, published.astimezone(dflt_tz).date())

    plain = blank(text, [z[:2] for z in zones])           # "+09:00" in "UTC+09:00" isn't a time
    gaps = blank(plain, [d[:2] for d in dates])           # "Oct 9 22:00 - Oct 10 02:00" is a range

    tokens = []
    for t in TIME_RE.finditer(plain):
        hh, mm, ampm = t.groups()
        h = int(hh)
        if ampm:
            h = h % 12 + (12 if ampm.lower() == "p" else 0)
        zone = next((z for z in zones if z[0] >= t.end() and NEXT_TO_TIME_RE.fullmatch(text[t.end():z[0]])), None)
        before = [d for _, end, d in dates if end <= t.start()]
        day = before[-1] if before else (dates[0][2] if dates else None)
        tokens.append((t, h * 60 + int(mm), zone, day))

    def instant(token, zone):
        _, minutes, _, day = token
        tz, label = (zone[2], zone[3]) if zone else (dflt_tz, dflt_label)
        day = day or published.astimezone(tz).date()
        return datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(minutes=minutes), label

    ranges, i = [], 0
    while i < len(tokens):
        t1, t2 = tokens[i], tokens[i + 1] if i + 1 < len(tokens) else None
        if t2 and RANGE_GAP_RE.fullmatch(gaps[t1[0].end():t2[0].start()]):
            zone = t1[2] or t2[2]  # "10:00 - 14:00 KST": the zone covers both ends
            start, label = instant(t1, t1[2] or zone)
            end, _ = instant(t2, t2[2] or zone)
            if end <= start:  # e.g. 22:00 ~ 02:00 rolls past midnight
                end += timedelta(days=1)
            ranges.append((start, end, label))
            i += 2
        else:
            start, label = instant(t1, t1[2])
            ranges.append((start, None, label))
            i += 1

    # Quoted/repeated tweets mention the same time again: drop exact repeats (also the same
    # instant written in two zones), and a lone time that is just the start of a range already
    # listed ("19:45" + "19:45-20:45").
    range_starts = {s for s, e, _ in ranges if e}
    results, labels = [], []
    for s, e, label in ranges:
        if (s, e) not in results and not (e is None and s in range_starts):
            results.append((s, e))
            labels.append(label)
    return (labels[0] if labels else dflt_label), results


DURATION_RE = re.compile(  # "3 hours 30min", "5 hours 30 min", "90 minutes", "1時間30分", "1시간 30분"; not "8h30"
    r"(?<![\d:.])(?:(\d{1,2})\s*(?:hours?|hrs?|h|時間|시간)(?:\s*(\d{1,2})\s*(?:minutes?|mins?|m|分|분))?"
    r"|(\d{1,3})\s*(?:minutes?|mins?|分|분))(?![A-Za-z0-9])", re.I)
DURATION_CONTEXT_RE = re.compile(r"duration|lasts?|takes?|approx|expected|所要|予定時間|程度|소요|예정", re.I)
DURATION_LABEL_RE = re.compile(r"\(([^()]{1,40}?)(?:\s+servers?)?\)\s*:?\s*$", re.I)  # "Duration (ASIA servers): "


def add_duration_ends(text: str, times):
    """A post giving one start time plus durations gets end times: "When: 23:30 PDT ... Duration
    (NA, EU, LATAM servers): 3 hours 30min. Duration (ASIA servers): 5 hours 30 min." -> two labelled
    ranges. Returns (times, labels); posts with explicit ranges or several times are left alone."""
    if not times or any(end for _, end in times) or len({s for s, _ in times}) != 1:
        return times, [None] * len(times)
    start, found = times[0][0], []
    for m in DURATION_RE.finditer(text):
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line, before = text[line_start:line_end if line_end >= 0 else len(text)], text[line_start:m.start()]
        in_brackets = before.count("(") + before.count("（") > before.count(")") + before.count("）")
        if not (in_brackets or DURATION_CONTEXT_RE.search(line)):
            continue  # "2 hours of new content" isn't how long the maintenance takes
        hours, mins, only_mins = m.groups()
        minutes = int(hours) * 60 + int(mins or 0) if hours else int(only_mins)
        label = DURATION_LABEL_RE.search(before)
        item = (minutes, label.group(1).strip() if label else None)
        if 5 <= minutes <= 24 * 60 and item not in found:
            found.append(item)
    if not found:
        return times, [None]
    return [(start, start + timedelta(minutes=mins)) for mins, _ in found], [label for _, label in found]


def tz_from_label(label: str):
    """The zone a parse_times() label stands for: "JST", "PDT", "PT", "UTC+9"."""
    m = re.fullmatch(r"UTC([+-])(\d{1,2})(?::(\d{2}))?", label or "")
    if m:
        offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        return timezone(offset if m.group(1) == "+" else -offset, label)
    return ZoneInfo(TZ_ALIASES.get(label) or SHORT_TZ.get(label) or "Asia/Seoul")


def fmt_12h(dt) -> str:
    """e.g. "Thu 8 Oct 7:45 pm" (built by hand: %-I isn't portable)."""
    return f"{dt:%a} {dt.day} {dt:%b} {dt.hour % 12 or 12}:{dt.minute:02d} {'am' if dt.hour < 12 else 'pm'}"


def fmt_span(start, end) -> str:
    """ "Thu 8 Oct 7:45 pm", or a range: "Thu 8 Oct 7:45 – 8:45 pm" / "... 11:45 pm – Fri 9 Oct 12:45 am"."""
    if end is None:
        return fmt_12h(start)
    if end.date() != start.date():
        return f"{fmt_12h(start)} – {fmt_12h(end)}"
    s, e = fmt_12h(start), fmt_12h(end).split(" ", 3)[3]  # end: clock part only
    if s[-2:] == e[-2:]:
        s = s[:-3]  # same am/pm: "7:45 – 8:45 pm"
    return f"{s} – {e}"


def format_times(tz_label, times, labels=None):
    lines = []
    for i, (start, end) in enumerate(times):
        name = start.tzname() or ""
        if not name or name[0] in "+-":  # zones without an abbreviation report "+08"
            name = tz_label
        label = labels[i] if labels and i < len(labels) and labels[i] else ""
        parts = [f"**{label + ': ' if label else ''}{fmt_span(start, end)} {name}**"]
        for name, tz in TARGETS:
            ls, le = start.astimezone(tz), end.astimezone(tz) if end else None
            parts.append(f"{name}: {fmt_span(ls, le)} {ls:%Z}")
        yours = f"<t:{int(start.timestamp())}:F>"
        if end:  # Discord renders these in each reader's own zone and clock format
            same_day = end - start < timedelta(hours=12)
            yours += f" – <t:{int(end.timestamp())}:{'t' if same_day else 'F'}>"
        parts.append(f"Your time: {yours}")
        lines.append(" → ".join(parts[:1]) + "\n" + "\n".join("• " + p for p in parts[1:]))
    return "\n\n".join(lines)


TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
URL_RE = re.compile(r"https?://\S+")
DEDUPE_DAYS = 14  # how long to remember content/time-window fingerprints
# Never post tweets older than this. A feed can suddenly expose older history it
# never showed before (e.g. an rss.app plan change raising the item count).
MAX_AGE_HOURS = 48
# A newly added feed records its backlog without posting, except tweets this recent.
NEW_FEED_RECENT_HOURS = 3


def tweet_key(entry) -> str:
    """Stable ID: the numeric tweet ID if we can find it (survives feed GUID changes)."""
    for field in (entry.get("link", ""), entry.get("id", "")):
        m = TWEET_ID_RE.search(field or "")
        if m:
            return m.group(1)
    return entry.get("id") or entry.get("link")


def content_fingerprint(text: str) -> str:
    """Same wording with different links/spacing/emoji = same post."""
    t = URL_RE.sub("", text.lower())
    t = re.sub(r"[^\w]+", "", t)
    return hashlib.sha256(t.encode()).hexdigest()[:16]


def window_key(cat, times, text=""):
    """Maintenance with the same start time = same maintenance (catches reminder tweets). Plain
    announcements only: a cancellation/completion/extension shares the start time but is news."""
    if cat["key"] != "maintenance" or not times or maint_status(cat, text):
        return None
    return maint_key(times[0][0])


def maint_key(start) -> str:
    return f"maint:{start.astimezone(ZoneInfo('UTC')):%Y%m%d%H%M}"


def load_state():
    try:
        with open(STATE_FILE) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None  # first run
    except json.JSONDecodeError as e:
        # Fail loudly: silently treating a hand-edit typo as "first run" would hide it.
        sys.exit(f"{STATE_FILE} is not valid JSON ({e}). Fix it (look for a stray comma) or delete it to start fresh.")
    if isinstance(data, list):  # old format: just a list of IDs
        data = {"ids": data}
    data.setdefault("ids", [])
    data.setdefault("recent", {})  # fingerprint/window -> unix time first posted
    data.setdefault("schema", 2)  # state format version (its first write also proves git push works)
    data.setdefault("upcoming", {})  # maintenance windows that may still need a reminder
    return data


def recent_time(v):
    return v["t"] if isinstance(v, dict) else v  # old format stored a bare timestamp


def is_repeat(state, key, eid):
    """Seen within DEDUPE_DAYS from a *different* tweet (so re-testing a deleted ID still posts)."""
    v = state["recent"].get(key) if key else None
    return v is not None and not (isinstance(v, dict) and v.get("id") == eid)


def save_state(state):
    if PREVIEW:
        return  # a preview never writes state
    cutoff = now_ts() - DEDUPE_DAYS * 86400
    state["ids"] = state["ids"][-1000:]  # keep newest 1000, in order
    state["recent"] = {k: v for k, v in state["recent"].items() if recent_time(v) >= cutoff}
    day_ago = now_ts() - 86400
    state["upcoming"] = {k: w for k, w in state.get("upcoming", {}).items() if w["start"] >= day_ago}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)  # atomic: never leaves a half-written file


HANDLE_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status", re.I)


def is_repost(entry, own_handle: str) -> bool:
    """The feed's account reposting someone else's tweet: the entry's link names another author."""
    author = HANDLE_RE.search(entry.get("link") or "")
    return bool(own_handle and author and author.group(1).lower() != own_handle.lower())


ANCHOR_RE = re.compile(r"""<a\b[^>]*?\bhref\s*=\s*["']([^"']+)["'][^>]*>(.*?)</a>""", re.I | re.S)
PIC_LINK_RE = re.compile(r"(?:https?://)?pic\.(?:twitter|x)\.com/", re.I)
RSS_APP_HOST_RE = re.compile(r"https?://(?:[\w-]+\.)*rss\.app\b", re.I)
MD_LINK_RE = re.compile(r"\[[^\]]*\]\([^)]*\)")
TRAILING_TAGS_RE = re.compile(r"(?:\s*#[^\s#]+)+\s*$")
VIDEO_LINE_RE = re.compile(r"^[ \t]*Video[ \t]*$", re.M)  # rss.app's placeholder for an embedded video


def anchors(raw: str):
    """[(label, href)] for the links in an entry's HTML, labels exactly as they appear in clean() text."""
    out = []
    for href, inner in ANCHOR_RE.findall(raw or ""):
        label = clean(inner)
        if label:
            out.append((label, html.unescape(href).strip()))
    return out


def md_link(label: str, href: str) -> str:
    label = re.sub(r"^https?://", "", label).replace("[", "(").replace("]", ")")[:200]
    for ch, code in ((" ", "%20"), ("(", "%28"), (")", "%29"), ("<", "%3C"), (">", "%3E")):
        href = href.replace(ch, code)
    return f"[{label}]({href})"


def linkify(text: str, links) -> str:
    """Make a tweet's links clickable for Discord: "aion2.plaync.com/…" → [label](url); picture
    links are dropped (the image is attached); hashtags, mentions and non-web links stay as text."""
    pos = 0
    for label, href in links:
        i = text.find(label, pos)
        if i < 0:
            continue
        if PIC_LINK_RE.match(label):
            new = ""
        elif label[0] in "#@$" or not href.lower().startswith(("http://", "https://")) or RSS_APP_HOST_RE.match(href):
            pos = i + len(label)
            continue
        else:
            new = md_link(label, href)
        text = text[:i] + new + text[i + len(label):]
        pos = i + len(new)
    return text


def tidy(text: str) -> str:
    """Post-ready text: no "Video" placeholder lines or trailing hashtags, at most one blank line."""
    text = VIDEO_LINE_RE.sub("", text)
    without_tags = TRAILING_TAGS_RE.sub("", text)
    text = without_tags if without_tags.strip() else text
    return re.sub(r"\n[ \t]*(?:\n[ \t]*)+\n", "\n\n", text).strip()


def truncate_md(text: str, limit: int) -> str:
    """Shorten to limit characters with "…", never cutting a [label](url) link in half."""
    if len(text) <= limit:
        return text
    cut = limit - 1
    for m in MD_LINK_RE.finditer(text):
        if m.start() < cut < m.end():
            cut = m.start()
            break
    return text[:cut].rstrip() + "…"


IMAGE_URL_RE = re.compile(r"\.(?:jpe?g|png|webp|gif)(?:$|\?)|[?&]format=(?:jpe?g|png|webp)|pbs\.twimg\.com/media/", re.I)


def usable_image(url) -> bool:
    url = (url or "").strip()
    return (url.startswith("https://") and len(url) <= 2000 and not RSS_APP_HOST_RE.match(url)
            and not re.search(r"/emoji/|/profile_images/|\.svg(?:$|\?)", url, re.I))


def entry_image(entry):
    """The tweet's first picture: rss.app's media_content, then thumbnails, image enclosures,
    then any <img> in the HTML. None if there's no usable https image."""
    candidates = []
    for m in entry.get("media_content") or []:
        kind = f"{m.get('medium', '')} {m.get('type', '')}"
        if "image" in kind or IMAGE_URL_RE.search(m.get("url") or ""):
            candidates.append(m.get("url"))
    candidates += [m.get("url") for m in entry.get("media_thumbnail") or []]
    candidates += [e.get("href") for e in entry.get("enclosures") or [] if str(e.get("type", "")).startswith("image/")]
    for part in [entry.get("summary") or ""] + [c.get("value") or "" for c in entry.get("content") or []]:
        candidates += re.findall(r"""<img\b[^>]*?\bsrc\s*=\s*["']([^"']+)["']""", part, re.I)
    return next((u for u in (html.unescape(c or "").strip() for c in candidates) if usable_image(u)), None)


def quote_block(quote: str, url) -> str:
    """A quoted tweet as a Discord block quote under the post ("" without one)."""
    if not quote:
        return ""
    quote = truncate_md(quote, 700)
    head = f"> ↪ [Quoted post]({url})" if url else "> ↪ Quoted post"
    body = "\n".join(f"> {line}" if line.strip() else ">" for line in quote.splitlines())
    return f"\n\n{head}\n{body}"


def build_payload(cat, entry, text, tz_label, times, source="AION 2 official X", quote="", quote_url=None,
                  display=None, display_quote=None, labels=None):
    """text/quote are what was classified; display/display_quote (with links) are what's shown."""
    status = maint_status(cat, text)
    title, color = cat["title"], cat["color"]
    for key, _, status_title, status_color in MAINT_STATUS:
        if key == status:
            title, color = status_title, status_color
    body = truncate_md(display if display is not None else text, 3000)
    shown_quote = display_quote if display_quote is not None else quote
    handle = (HANDLE_RE.search(entry.get("link") or "") or [None, ""])[1]
    translated = handle.upper().endswith("_JP") and not re.search(r"[぀-ヿ]", text)
    embed = {
        "title": title,
        "url": entry.get("link"),
        "description": (body + quote_block(shown_quote, quote_url))[:4000],  # Discord's limit is 4096
        "color": color,
        "footer": {"text": "via X" + (" · translated from Japanese" if translated else "")},
    }
    published = entry_time(entry)
    if published:
        embed["timestamp"] = published.isoformat()  # Discord shows it in each reader's own time
    image = entry_image(entry)
    if image:
        embed["image"] = {"url": image}
    # Which account tweeted it, e.g. "@AION2_JP", linked to the profile.
    m = HANDLE_RE.search(entry.get("link") or "")
    if m:
        embed["author"] = {"name": f"@{m.group(1)}", "url": f"https://x.com/{m.group(1)}"}
    else:
        embed["author"] = {"name": source[:256]}
    if times:
        embed["fields"] = [{"name": "🕒 Converted times", "value": format_times(tz_label, times, labels)[:1024]}]
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}  # no surprise @everyone
    if cat["ping"] and status is None and ROLE_ID.isdigit():  # only plain "servers going down" posts ping
        payload["content"] = f"<@&{ROLE_ID}> maintenance incoming"
        payload["allowed_mentions"] = {"roles": [ROLE_ID]}
    return payload


def now_ts() -> float:
    """Current unix time (one seam, so tests can set the clock)."""
    return time.time()


def sleep(seconds: float):
    time.sleep(seconds)  # one seam, so tests don't really wait


class PostError(Exception):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status


MAX_RATE_WAIT = 10  # seconds; a longer Discord rate limit fails the post (retried next run)


def rate_wait(r) -> float:
    """Seconds Discord asks us to wait after a 429. Headers first: the JSON retry_after's
    units differ between API versions."""
    for header in ("Retry-After", "X-RateLimit-Reset-After"):
        try:
            return float(r.headers[header])
        except (KeyError, TypeError, ValueError):
            pass
    try:
        data = r.json()
    except ValueError:
        return 1.0
    return float(data.get("retry_after", 1)) if isinstance(data, dict) else 1.0


def post(payload):
    # Never let the webhook URL reach the log: requests puts the full URL in
    # raise_for_status() and connection-error messages, and public repo logs are public.
    for attempt in range(3):
        try:
            r = requests.post(WEBHOOK, json=payload, timeout=15)
        except requests.RequestException as e:
            raise PostError(type(e).__name__) from None
        if r.status_code == 429 and attempt < 2:
            wait = rate_wait(r)
            if wait > MAX_RATE_WAIT:
                raise PostError(f"HTTP 429 (retry after {wait:.0f}s)", 429)
            sleep(wait)
            continue
        if r.status_code >= 400:
            raise PostError(f"HTTP {r.status_code} {r.text[:200]}", r.status_code)
        return


def deliver(payload):
    """Send one Discord message; in preview mode print it instead."""
    if PREVIEW:
        print(redact(json.dumps(payload, ensure_ascii=False, indent=1)))
        return
    try:
        post(payload)
    except PostError as e:
        embeds = payload.get("embeds") or []
        if e.status != 400 or not any("image" in em for em in embeds):
            raise
        for em in embeds:  # Discord rejected the post, most likely the image URL: send the text alone
            em.pop("image", None)
        post(payload)


RSS_APP_URL_RE = re.compile(r"https?://(?:[\w-]+\.)*rss\.app\b[^\s\"'<>)\]]*", re.I)


def redact(text: str) -> str:
    """Remove secrets before printing to the (public) Actions log."""
    for secret in [*FEED_URLS, WEBHOOK or ""]:
        if secret:
            text = text.replace(secret, "[secret]")
    return RSS_APP_URL_RE.sub("[rss.app URL]", text)


def html_skeleton(raw: str) -> str:
    """An entry's HTML tag structure (tag and class names only, no text) for preview logs."""
    out = []
    for close, name, attrs in re.findall(r"<(/?)([a-zA-Z][\w-]*)([^>]*)>", raw or ""):
        cls = re.search(r"""class\s*=\s*["']([^"']*)""", attrs)
        out.append(f"<{close}{name.lower()}{'.' + '.'.join(cls.group(1).split()) if cls else ''}>")
    return " ".join(out)[:1500]


def feed_key(url: str) -> str:
    """State remembers feeds by hash: the state file is public, feed URLs are secrets."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def entry_time(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=ZoneInfo("UTC")) if t else None


def fetch_feed(url):
    """Download and parse one feed: (feed, None), or (None, problem). The problem is like "HTTP 404"
    or an exception name, never the URL (feed URLs are secrets; logs are public)."""
    problem = None
    for attempt in (1, 2):
        try:
            r = requests.get(url, timeout=(5, 20), headers={"User-Agent": USER_AGENT})
        except requests.RequestException as e:
            problem = type(e).__name__
        else:
            if r.status_code < 400:
                feed = feedparser.parse(
                    r.content, response_headers={"content-type": r.headers.get("Content-Type", "")})
                if feed.entries:
                    return feed, None
                # An empty first fetch would also cause a backlog flood later, so it's an error.
                return None, type(feed.bozo_exception).__name__ if feed.bozo else "no entries"
            problem = f"HTTP {r.status_code}"
            if r.status_code < 500:
                break  # a 4xx won't fix itself in 3 seconds
        if attempt == 1:
            sleep(3)
    return None, problem


def fetch_feeds(state):
    """Return ([(entry, source, is_new_feed, feed_number)] oldest first, failed feed count,
    per-feed statuses)."""
    items, failed, statuses = [], 0, []
    for n, url in enumerate(FEED_URLS, 1):
        key = feed_key(url)
        feed, problem = fetch_feed(url)
        if feed is None:
            print(f"Feed error (feed {n}): {problem}")  # skip this feed only
            failed += 1
            statuses.append({"n": n, "key": key, "ok": False, "problem": problem})
            continue
        is_new = key not in state["feeds"]
        if is_new:
            state["feeds"].append(key)
            print(f"feed {n} is new: recording its {len(feed.entries)} existing posts, nothing posted")
        source = feed.feed.get("title") or "AION 2 official X"
        items += [(e, source, is_new, n) for e in feed.entries]
        handle = feed_identity(source)[1]
        # The account's own posts only: a fan repost appearing doesn't mean the feed is current.
        own = [t for t in (entry_time(e) for e in feed.entries if not is_repost(e, handle)) if t]
        statuses.append({"n": n, "key": key, "ok": True, "newest_own": max(own) if own else None,
                         "handle": handle})
    # Feeds aren't reliably newest-first, so sort by date (undated last) to post in order.
    far_future = datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    items.sort(key=lambda it: entry_time(it[0]) or far_future)
    return items, failed, statuses


def window_record(info, eid, start):
    """A pending maintenance window for state["upcoming"]: plain numbers and labels, no secrets."""
    ranges = [[int(s.timestamp()), int(e.timestamp()) if e else None, label]
              for (s, e), label in zip(info["times"], info["labels"] or [None] * len(info["times"])) if s == start]
    return {"start": int(start.timestamp()), "tz": info["tz_label"], "id": eid,
            "handle": info["handle"], "ranges": ranges}


def note_maintenance(state, info, eid, now):
    """Keep state["upcoming"] (maintenance that may need a reminder) in step with a posted
    maintenance tweet: an announcement adds its window, a cancellation removes it, and a
    reschedule moves it to the latest time mentioned."""
    if info["cat"]["key"] != "maintenance" or info["status"] not in (None, "cancelled", "rescheduled"):
        return
    upcoming, status, times = state.setdefault("upcoming", {}), info["status"], info["times"]
    if status is None:
        if not times or times[0][0].timestamp() <= now:
            return
        w = upcoming.setdefault(maint_key(times[0][0]), window_record(info, eid, times[0][0]))
        if w["start"] - now < (REMINDER_MINUTES + REMINDER_MIN_LEAD) * 60:
            w.setdefault("reminded", int(now))  # announced so close to the start that the post is the warning
        return
    starts = {int(s.timestamp()) for s, _ in times}
    new_start = max((s for s, _ in times), default=None) if status == "rescheduled" else None
    hit = [k for k, w in upcoming.items() if w["start"] in starts and not w.get("cancelled")
           and (new_start is None or w["start"] != int(new_start.timestamp()))]
    if not hit:  # no time matches: the account's only pending window (next 24 h; 7 days for a reschedule)
        horizon = 24 * 3600 if status == "cancelled" else 7 * 86400
        pending = [k for k, w in upcoming.items() if w.get("handle") == info["handle"] and not w.get("cancelled")
                   and now < w["start"] <= now + horizon
                   and (new_start is None or w["start"] != int(new_start.timestamp()))]
        hit = pending if len(pending) == 1 else []
    for k in hit:
        upcoming[k]["cancelled"] = int(now)
    if new_start and new_start.timestamp() > now:
        upcoming.setdefault(maint_key(new_start), window_record(info, eid, new_start))


def reminder_payload(w, now):
    minutes = max(5, int(round((w["start"] - now) / 300.0)) * 5)
    tz = tz_from_label(w["tz"])
    times = [(datetime.fromtimestamp(s, tz), datetime.fromtimestamp(e, tz) if e else None) for s, e, _ in w["ranges"]]
    url = f"https://x.com/{w['handle']}/status/{w['id']}" if w.get("handle") else None
    embed = {
        "title": f"⏰ Maintenance starts in ~{minutes} min",
        "description": f"Starts <t:{w['start']}:R>." + (f" [Original announcement]({url})" if url else ""),
        "color": 0xE67E22,
        "fields": [{"name": "🕒 Converted times",
                    "value": format_times(w["tz"], times, [label for _, _, label in w["ranges"]])[:1024]}],
        "footer": {"text": "Reminder"},
    }
    if url:
        embed["url"] = url
        embed["author"] = {"name": f"@{w['handle']}", "url": f"https://x.com/{w['handle']}"}
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}
    if ROLE_ID.isdigit():
        payload["content"] = f"<@&{ROLE_ID}> maintenance starts in ~{minutes} min"
        payload["allowed_mentions"] = {"roles": [ROLE_ID]}
    return payload


def send_reminders(state, now) -> int:
    """Post one "⏰ Maintenance starts in ~N min" per pending window, REMINDER_MINUTES before it.
    Returns the number of failed posts (they're retried next run)."""
    failures = 0
    for key, w in sorted(state.get("upcoming", {}).items(), key=lambda kv: kv[1]["start"]):
        if w.get("reminded") or w.get("cancelled"):
            continue
        left = w["start"] - now
        if left <= 0:
            w["reminded"] = int(now)  # runs were missed: never remind after the start
            continue
        if left > REMINDER_MINUTES * 60:
            continue
        try:
            deliver(reminder_payload(w, now))
        except PostError as e:
            print(f"reminder failed {key}: {e}")
            failures += 1
            continue
        w["reminded"] = int(now)
        save_state(state)
        print(f"reminded {key}")
    return failures


FEED_DOWN_MINUTES = 60   # a feed failing this long gets one alert
TRIGGER_STALE_MINUTES = 60
# A quiet feed is only a problem if the account posted something the feed missed. When a feed's
# newest post by its own account is over STALE_CHECK_HOURS old, the account's public X profile is
# checked (allowing rss.app RSS_LAG_HOURS to catch up). If X can't tell, warn after STALE_DAYS.
STALE_CHECK_HOURS = 24
RSS_LAG_HOURS = 2
STALE_DAYS = 7
X_CHECK_EVERY_HOURS = 6  # X is asked only by runs starting in the first 15 min of 00/06/12/18 UTC
X_PROFILE_URL = "https://api.fxtwitter.com/2/profile/{}/statuses"  # free public API, no key
BRISBANE = dict(TARGETS)["Brisbane"]


def notify(text: str) -> bool:
    """Plain-text bot-health message in the news channel (no pings). False if posting failed."""
    try:
        deliver({"content": text, "allowed_mentions": {"parse": []}})
    except PostError as e:
        print(f"alert failed: {e}")
        return False
    return True


def account_latest(handle: str):
    """Unix time of the account's newest original post on X (not a reply or repost), from the
    fxtwitter API; None if that can't be told. Logs the handle only (it's public)."""
    try:
        r = requests.get(X_PROFILE_URL.format(handle), timeout=(5, 15), headers={"User-Agent": USER_AGENT})
        if r.status_code != 200:
            print(f"X check @{handle} unavailable (HTTP {r.status_code})")
            return None
        results = r.json().get("results") or []
        times = [int(t["created_timestamp"]) for t in results
                 if isinstance(t, dict) and not t.get("replying_to") and not t.get("reposted_by")
                 and str((t.get("author") or {}).get("screen_name", "")).lower() == handle.lower()
                 and isinstance(t.get("created_timestamp"), (int, float))]
    except Exception as e:  # network, bad JSON, unexpected shape: X just can't tell this time
        print(f"X check @{handle} unavailable ({type(e).__name__})")
        return None
    if not times:
        print(f"X check @{handle} unavailable (no original posts listed)")
        return None
    latest = max(times)  # results are ordered by repost time and a pinned post comes first
    print(f"X check @{handle}: newest original post {datetime.fromtimestamp(latest, timezone.utc):%Y-%m-%d %H:%M} UTC")
    return latest


def x_check_due(now) -> bool:
    """Ask X only on runs starting in the first 15 minutes of every X_CHECK_EVERY_HOURS (00, 06, 12
    and 18 UTC). Stateless, so checking never writes state (which would commit on every check)."""
    return now % (X_CHECK_EVERY_HOURS * 3600) < 15 * 60


def stale_alert(rec, who, now, due):
    """The stale-feed warning to post for this feed now, or None."""
    own, handle, lag = rec.get("newest_own"), rec.get("handle"), RSS_LAG_HOURS * 3600
    if not own or rec.get("stale_alerted") or now - own <= STALE_CHECK_HOURS * 3600:
        return None
    if handle:
        if not due:
            return None
        latest = account_latest(handle)
        if latest is not None:
            if latest - own > lag and now - latest > lag:
                t = datetime.fromtimestamp(latest, BRISBANE)
                return (f"⚠️ {who}: @{handle} posted at {fmt_12h(t)} {t:%Z} but the rss.app feed hasn't "
                        "picked it up — check rss.app")
            return None  # the account has just been quiet
    if now - own > STALE_DAYS * 86400:  # X can't tell (or no handle): fall back to time alone
        return f"⚠️ {who}: no new tweets for {int((now - own) // 86400)} days — check the rss.app feed is still updating"
    return None


def check_feed_health(state, statuses, now) -> int:
    """Alert once when a feed has been down FEED_DOWN_MINUTES (and once when it's back), and once
    when a feed seems to have stopped picking up posts (see stale_alert). State only changes on
    those transitions, so an outage or a quiet account doesn't commit every run. Returns the number
    of failed alert posts."""
    health, failures = state.setdefault("feed_health", {}), 0
    due = PREVIEW or x_check_due(now)  # a preview always asks X, to show what the check would say
    for st in statuses:
        rec = health.setdefault(st["key"], {})
        who = f"Feed {st['n']}" + (f" (@{rec['handle']})" if rec.get("handle") else "")
        if not st["ok"]:
            if "fail_since" not in rec:
                rec["fail_since"] = int(now)
            elif not rec.get("alerted") and now - rec["fail_since"] >= FEED_DOWN_MINUTES * 60:
                if notify(f"⚠️ {who} hasn't loaded for over an hour ({st['problem']}) — check rss.app"):
                    rec["alerted"], rec["problem"] = int(now), st["problem"]
                else:
                    failures += 1
            continue
        if st.get("handle") and rec.get("handle") != st["handle"]:
            rec["handle"] = st["handle"]
            who = f"Feed {st['n']} (@{st['handle']})"
        if "fail_since" in rec:
            if rec.get("alerted"):
                down_h = max(1, round((now - rec["fail_since"]) / 3600))
                if not notify(f"✅ {who} is working again (was down ~{down_h} h)"):
                    failures += 1
                    continue
            for k in ("fail_since", "alerted", "problem"):
                rec.pop(k, None)
        rec.pop("newest", None)  # older state: newest of any entry; now newest_own (the account's own)
        newest = int(st["newest_own"].timestamp()) if st.get("newest_own") else None
        if newest and newest > rec.get("newest_own", 0):
            rec["newest_own"] = newest
            rec.pop("stale_alerted", None)
        message = stale_alert(rec, who, now, due)
        if message:
            if notify(message):
                rec["stale_alerted"] = int(now)
            else:
                failures += 1
    configured = {st["key"] for st in statuses}
    state["feed_health"] = {k: v for k, v in health.items() if k in configured}
    return failures


def check_trigger(state, now) -> int:
    """On GitHub's own scheduled runs the workflow passes LAST_DISPATCH (the last cron-job.org run).
    Warn once if it's over TRIGGER_STALE_MINUTES old (e.g. its GitHub token expired), and once
    when it's running again. Other runs don't set LAST_DISPATCH and check nothing."""
    try:
        last = datetime.fromisoformat(os.environ["LAST_DISPATCH"].strip().replace("Z", "+00:00")).timestamp()
    except (KeyError, ValueError):
        return 0
    rec = state.setdefault("trigger_health", {})
    if now - last > TRIGGER_STALE_MINUTES * 60:
        if rec.get("alerted"):
            return 0
        hours = max(1, round((now - last) / 3600))
        if not notify(f"⚠️ The 15-minute trigger (cron-job.org) hasn't run for ~{hours} h — check the "
                      "cron-job.org job and its GitHub token. GitHub's own backup timer still runs a few times a day."):
            return 1
        rec["alerted"] = int(now)
    elif rec.get("alerted"):
        if not notify("✅ The 15-minute trigger (cron-job.org) is running again"):
            return 1
        rec.pop("alerted")
    return 0


def decide(entry, source, state):
    """What to do with one feed entry: ("post", info with a payload) or (skip reason, info).
    Shared by real runs and preview, so a preview shows exactly what would be posted."""
    eid = tweet_key(entry)
    raw = entry.get("summary") or entry.get("title", "")
    full = TRAILER_RE.sub("", clean(raw))
    own_name, own_handle = feed_identity(source)
    author = HANDLE_RE.search(entry.get("link") or "")
    if is_repost(entry, own_handle):
        return "retweet", {"text": full}
    # Judge the tweet by its own words; a quoted tweet is only shown (and used as a fallback).
    text, quote, quote_url = split_quote(full, own_name)
    cat = classify(text) or (classify(f"{text}\n{quote}") if quote else None)
    if not cat:
        return "not important", {"text": full}
    if cat["key"] == "event" and is_promo(text):
        return "promo", {"text": full}
    published = entry_time(entry)
    try:
        # Tweets rarely name their zone; label Japanese-account times JST (same UTC+9 as KST).
        default_tz = "JST" if author and author.group(1).upper().endswith("_JP") else None
        tz_label, times = parse_times(text, published, default_tz)
        if not times and quote:  # e.g. a reminder that quotes the original notice
            tz_label, times = parse_times(quote, published, default_tz)
        times, labels = add_duration_ends(text, times)
    except Exception as e:  # odd date text must never block the post itself
        print(f"time parse failed {eid}: {type(e).__name__}: {e}")
        tz_label, times, labels = SOURCE_TZ, [], []
    status = maint_status(cat, text)
    fp = "text:" + content_fingerprint(text + quote)
    wk = window_key(cat, times, text)
    if is_repeat(state, fp, eid) or is_repeat(state, wk, eid):
        return "duplicate/reminder", {"text": full}
    links = anchors(raw)
    display = tidy(linkify(text, links))
    display_quote = tidy(linkify(quote, links)) if quote else ""
    payload = build_payload(cat, entry, text, tz_label, times, source, quote, quote_url, display, display_quote,
                            labels)
    return "post", {"text": full, "cat": cat, "status": status, "times": times, "labels": labels,
                    "tz_label": tz_label, "handle": author.group(1) if author else "",
                    "fp": fp, "wk": wk, "payload": payload}


def preview(state, items, statuses=()):
    """PREVIEW=1: show what the newest tweets of each feed would post as. Posts and saves nothing."""
    print(f"PREVIEW: newest {PREVIEW_PER_FEED} tweets per feed; nothing is posted or saved")
    shown = {}
    for entry, source, _, n in reversed(items):  # newest first
        if shown.get(n, 0) >= PREVIEW_PER_FEED:
            continue
        shown[n] = shown.get(n, 0) + 1
        verdict, info = decide(entry, source, state)
        what = f"would post {info['cat']['key']}" if verdict == "post" else f"skip ({verdict})"
        print(f"\n=== feed {n} · {tweet_key(entry)} · {what}")
        print("fields:", ", ".join(sorted(entry.keys())))
        print("html:", html_skeleton(entry.get("summary") or ""))
        print("text:", redact(json.dumps(info["text"][:1500], ensure_ascii=False)))
        if verdict == "post":
            deliver(info["payload"])  # prints, doesn't post
    print("\n=== reminders due now (from saved state)")
    send_reminders(state, now_ts())  # prints, doesn't post or save
    print("\n=== bot-health alerts due now")
    check_feed_health(state, list(statuses), now_ts())
    check_trigger(state, now_ts())


def main():
    if not FEED_URLS or not (WEBHOOK or PREVIEW):
        sys.exit("Set FEED_URL and DISCORD_WEBHOOK_URL")

    state = load_state()
    if state is not None and "feeds" not in state:
        # State from before multi-feed support: the single feed back then was FEED_URL's first line.
        state["feeds"] = [feed_key(FEED_URLS[0])]
    state = state or {"ids": [], "recent": {}, "feeds": [], "schema": 2, "upcoming": {}}

    items, failures, statuses = fetch_feeds(state)
    if PREVIEW:
        return preview(state, items, statuses)
    # Even with every feed down this carries on, so reminders and alerts still go out; feeds that
    # failed to load are never marked seen or baselined (fetch_feeds only records loaded feeds).
    seen_ids = set(state["ids"])
    now = now_ts()

    for entry, source, is_new_feed, _ in items:
        eid = tweet_key(entry)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        state["ids"].append(eid)
        published = entry_time(entry)
        age_hours = (now - published.timestamp()) / 3600 if published else None
        if is_new_feed and (age_hours is None or age_hours > NEW_FEED_RECENT_HOURS):
            continue  # don't spam a feed's backlog the first time we see it
        if age_hours is not None and age_hours > MAX_AGE_HOURS:
            print(f"skipped (too old) {eid}")
            continue

        verdict, info = decide(entry, source, state)
        if verdict != "post":
            print(f"skipped ({verdict}) {eid}")
            continue
        try:
            deliver(info["payload"])
        except PostError as e:
            # Not marked as seen, so it is retried next run instead of being lost.
            state["ids"].remove(eid)
            print(f"post failed {eid}: {e}")
            failures += 1
            continue
        state["recent"][info["fp"]] = {"t": now, "id": eid}
        if info["wk"]:
            state["recent"][info["wk"]] = {"t": now, "id": eid}
        note_maintenance(state, info, eid, now)
        save_state(state)  # save after every post, so a crash can't cause reposts
        kind = info["cat"]["key"] + (f"/{info['status']}" if info["status"] else "")
        print(f"posted {kind} {eid} ({len(info['times'])} times)")
    failures += send_reminders(state, now)
    failures += check_feed_health(state, statuses, now)
    failures += check_trigger(state, now)
    save_state(state)
    if not items:
        sys.exit("Feed error: no feed could be read")
    if failures:
        sys.exit(f"{failures} feed/post error(s) above; failed posts are retried next run")  # run shows red


if __name__ == "__main__":
    main()
