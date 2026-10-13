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
PREVIEW_PER_FEED = 3
USER_AGENT = "aion2-relay (+https://github.com/Jagomeiister/aion2-relay)"

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
            r"\bevents?\b|festival|celebrat|campaign|login reward|log-?in bonus|attendance|double (xp|exp|drop)|"
            r"limited[- ]time|giveaway|giv(?:e|es|ing) away|\bgifts?\b|livestream|live stream|coupon|redeem|"
            r"이벤트|출석|쿠폰|방송|선물|イベント|キャンペーン|配信|プレゼント|配布|ログインボーナス", re.I),
        "title": "🎉 Upcoming event",
        "color": 0x2ECC71,
        "ping": False,
    },
    {
        "key": "update",
        "pattern": re.compile(
            r"patch notes?|\bupdate\b|new (season|class|dungeon|zone|content)|season \d|"
            r"업데이트|패치|시즌|アップデート|パッチ", re.I),
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


def clean(text: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", text or "", flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def find_date(text: str, ref: date) -> date | None:
    """First valid date in the text. Invalid matches ("Season 2-30") are skipped, not fatal."""
    found = []
    for rx in (NUM_DATE_RE, CJK_DATE_RE):
        found += [(m.start(), m.group(1), m.group(2), m.group(3)) for m in rx.finditer(text)]
    found += [(m.start(), m.group(3), MONTHS[m.group(1)[:3].lower()], m.group(2)) for m in NAME_MD_RE.finditer(text)]
    found += [(m.start(), m.group(3), MONTHS[m.group(2)[:3].lower()], m.group(1)) for m in NAME_DM_RE.finditer(text)]
    for _, year, month, day in sorted(found, key=lambda f: f[0]):
        try:
            d = date(int(year) if year else ref.year, int(month), int(day))
            if not year:  # no year given: pick the one nearest the tweet (Dec tweet about "1/5")
                if d < ref - timedelta(days=180):
                    d = d.replace(year=d.year + 1)
                elif d > ref + timedelta(days=180):
                    d = d.replace(year=d.year - 1)
        except ValueError:
            continue
        return d
    return None


# What may sit between two times for them to count as one range: "19:45-20:45", "10:00 ~ 14:00",
# "10 am to 2 pm", "10:00から14:00", "10:00부터 14:00".
RANGE_GAP_RE = re.compile(r"\s*(?:-|–|—|~|～|〜|to|until|till|から|부터)\s*", re.I)


def parse_times(text: str, published: datetime | None, default_tz: str | None = None):
    """Return (tz_label, [(start, end or None)]) for times found in the post, repeats removed."""
    m = TZ_RE.search(text)
    if m and m.group(1):  # explicit offset, e.g. "(UTC+9)"
        offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        tz_label = f"UTC{m.group(1)}{int(m.group(2))}" + (f":{m.group(3)}" if m.group(3) else "")
        src = timezone(offset if m.group(1) == "+" else -offset, tz_label)
    else:
        tz_label = m.group(4).upper() if m else (default_tz or SOURCE_TZ)
        src = ZoneInfo(TZ_ALIASES.get(tz_label, "Asia/Seoul"))

    ref = (published or datetime.now(src)).astimezone(src)
    d = find_date(text, ref.date()) or ref.date()
    base = datetime(d.year, d.month, d.day, tzinfo=src)

    plain = TZ_RE.sub(lambda t: " " * len(t.group()), text)  # blank "+09:00" offsets, keep positions
    found = []  # (match, datetime)
    for t in TIME_RE.finditer(plain):
        hh, mm, ampm = t.groups()
        h = int(hh)
        if ampm:
            h = h % 12 + (12 if ampm.lower() == "p" else 0)
        found.append((t, base + timedelta(hours=h, minutes=int(mm))))

    ranges, i = [], 0
    while i < len(found):
        (m1, start), nxt = found[i], found[i + 1] if i + 1 < len(found) else None
        if nxt and RANGE_GAP_RE.fullmatch(plain[m1.end():nxt[0].start()]):
            end = nxt[1]
            if end <= start:  # e.g. 22:00 ~ 02:00 rolls past midnight
                end += timedelta(days=1)
            ranges.append((start, end))
            i += 2
        else:
            ranges.append((start, None))
            i += 1

    # Quoted/repeated tweets mention the same time again: drop exact repeats, and a lone
    # time that is just the start of a range already listed ("19:45" + "19:45-20:45").
    range_starts = {s for s, e in ranges if e}
    results = []
    for s, e in ranges:
        if (s, e) not in results and not (e is None and s in range_starts):
            results.append((s, e))
    return tz_label, results


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


def format_times(tz_label, times):
    lines = []
    for start, end in times:
        parts = [f"**{fmt_span(start, end)} {tz_label}**"]
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
# Maintenance tweets matching this are cancellations: own title, no ping.
CANCEL_RE = re.compile(r"cancel|call(ed)? off|취소|중지|中止|キャンセル", re.I)


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
    """Maintenance with the same start time = same maintenance (catches reminder tweets)."""
    if cat["key"] != "maintenance" or not times or CANCEL_RE.search(text):
        return None  # a cancellation shares the start time but is news, not a reminder
    return f"maint:{times[0][0].astimezone(ZoneInfo('UTC')):%Y%m%d%H%M}"


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
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)  # atomic: never leaves a half-written file


HANDLE_RE = re.compile(r"(?:x|twitter)\.com/([A-Za-z0-9_]{1,15})/status", re.I)


def build_payload(cat, entry, text, tz_label, times, source="AION 2 official X"):
    cancelled = cat["key"] == "maintenance" and CANCEL_RE.search(text)
    embed = {
        "title": "✅ Maintenance cancelled" if cancelled else cat["title"],
        "url": entry.get("link"),
        "description": text[:3500],
        "color": 0xF1C40F if cancelled else cat["color"],
        "footer": {"text": source[:100]},
    }
    # Which account tweeted it, e.g. "@AION2_JP", linked to the profile.
    m = HANDLE_RE.search(entry.get("link") or "")
    if m:
        embed["author"] = {"name": f"@{m.group(1)}", "url": f"https://x.com/{m.group(1)}"}
    else:
        embed["author"] = {"name": source[:256]}
    if times:
        embed["fields"] = [{"name": "🕒 Converted times", "value": format_times(tz_label, times)[:1024]}]
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}  # no surprise @everyone
    if cat["ping"] and not cancelled and ROLE_ID.isdigit():
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
        dated = [t for t in map(entry_time, feed.entries) if t]
        statuses.append({"n": n, "key": key, "ok": True, "newest": max(dated) if dated else None})
    # Feeds aren't reliably newest-first, so sort by date (undated last) to post in order.
    far_future = datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    items.sort(key=lambda it: entry_time(it[0]) or far_future)
    return items, failed, statuses


def decide(entry, source, state):
    """What to do with one feed entry: ("post", info) or (skip reason, None). Shared by real runs
    and preview, so a preview shows exactly what would be posted."""
    eid = tweet_key(entry)
    text = clean(entry.get("summary") or entry.get("title", ""))
    cat = classify(text)
    if not cat:
        return "not important", None
    try:
        # Tweets rarely name their zone; label Japanese-account times JST (same UTC+9 as KST).
        handle = HANDLE_RE.search(entry.get("link") or "")
        default_tz = "JST" if handle and handle.group(1).upper().endswith("_JP") else None
        tz_label, times = parse_times(text, entry_time(entry), default_tz)
    except Exception as e:  # odd date text must never block the post itself
        print(f"time parse failed {eid}: {type(e).__name__}: {e}")
        tz_label, times = SOURCE_TZ, []
    fp = "text:" + content_fingerprint(text)
    wk = window_key(cat, times, text)
    if is_repeat(state, fp, eid) or is_repeat(state, wk, eid):
        return "duplicate/reminder", None
    payload = build_payload(cat, entry, text, tz_label, times, source)
    return "post", {"cat": cat, "times": times, "fp": fp, "wk": wk, "payload": payload}


def preview(state, items):
    """PREVIEW=1: show what the newest tweets of each feed would post as. Posts and saves nothing."""
    print(f"PREVIEW: newest {PREVIEW_PER_FEED} tweets per feed; nothing is posted or saved")
    shown = {}
    for entry, source, _, n in reversed(items):  # newest first
        if shown.get(n, 0) >= PREVIEW_PER_FEED:
            continue
        shown[n] = shown.get(n, 0) + 1
        verdict, info = decide(entry, source, state)
        what = f"would post {info['cat']['key']}" if info else f"skip ({verdict})"
        print(f"\n=== feed {n} · {tweet_key(entry)} · {what}")
        print("fields:", ", ".join(sorted(entry.keys())))
        print("html:", html_skeleton(entry.get("summary") or ""))
        if info:
            deliver(info["payload"])  # prints, doesn't post


def main():
    if not FEED_URLS or not (WEBHOOK or PREVIEW):
        sys.exit("Set FEED_URL and DISCORD_WEBHOOK_URL")

    state = load_state()
    if state is not None and "feeds" not in state:
        # State from before multi-feed support: the single feed back then was FEED_URL's first line.
        state["feeds"] = [feed_key(FEED_URLS[0])]
    state = state or {"ids": [], "recent": {}, "feeds": [], "schema": 2}

    items, failures, statuses = fetch_feeds(state)
    if PREVIEW:
        return preview(state, items)
    if not items:
        sys.exit("Feed error: no feed could be read")  # don't touch state on a bad fetch
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
        if not info:
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
        save_state(state)  # save after every post, so a crash can't cause reposts
        print(f"posted {info['cat']['key']} {eid} ({len(info['times'])} times)")
    save_state(state)
    if failures:
        sys.exit(f"{failures} feed/post error(s) above; failed posts are retried next run")  # run shows red


if __name__ == "__main__":
    main()
