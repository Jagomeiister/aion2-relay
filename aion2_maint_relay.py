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
            r"\bevents?\b|festival|celebrat|campaign|login reward|attendance|double (xp|exp|drop)|"
            r"limited[- ]time|giveaway|livestream|live stream|coupon|redeem|"
            r"이벤트|출석|쿠폰|방송|イベント|キャンペーン|配信", re.I),
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


def classify(text: str):
    if SKIP.search(text):
        return None
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


def parse_times(text: str, published: datetime | None):
    """Return (tz_label, [aware datetimes]) for times found in the post."""
    m = TZ_RE.search(text)
    if m and m.group(1):  # explicit offset, e.g. "(UTC+9)"
        offset = timedelta(hours=int(m.group(2)), minutes=int(m.group(3) or 0))
        tz_label = f"UTC{m.group(1)}{int(m.group(2))}" + (f":{m.group(3)}" if m.group(3) else "")
        src = timezone(offset if m.group(1) == "+" else -offset, tz_label)
    else:
        tz_label = m.group(4).upper() if m else SOURCE_TZ
        src = ZoneInfo(TZ_ALIASES.get(tz_label, "Asia/Seoul"))

    ref = (published or datetime.now(src)).astimezone(src)
    d = find_date(text, ref.date()) or ref.date()
    base = datetime(d.year, d.month, d.day, tzinfo=src)

    results, prev = [], None
    for hh, mm, ampm in TIME_RE.findall(TZ_RE.sub(" ", text)):  # drop "+09:00" offsets first
        h = int(hh)
        if ampm:
            h = h % 12 + (12 if ampm.lower() == "p" else 0)
        dt = base + timedelta(hours=h, minutes=int(mm))
        if prev and dt < prev:          # e.g. 22:00 ~ 02:00 rolls past midnight
            dt += timedelta(days=1)
        results.append(dt)
        prev = dt
    return tz_label, results


def format_times(tz_label, times):
    lines = []
    for dt in times:
        parts = [f"**{dt:%a %d %b %H:%M} {tz_label}**"]
        for name, tz in TARGETS:
            local = dt.astimezone(tz)
            parts.append(f"{name}: {local:%a %d %b %H:%M} {local:%Z}")
        parts.append(f"Your time: <t:{int(dt.timestamp())}:F>")
        lines.append(" → ".join(parts[:1]) + "\n" + "\n".join("• " + p for p in parts[1:]))
    return "\n\n".join(lines)


TWEET_ID_RE = re.compile(r"/status(?:es)?/(\d+)")
URL_RE = re.compile(r"https?://\S+")
DEDUPE_DAYS = 14  # how long to remember content/time-window fingerprints


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


def window_key(cat, times):
    """Maintenance with the same start time = same maintenance (catches reminder tweets)."""
    if cat["key"] != "maintenance" or not times:
        return None
    return f"maint:{times[0].astimezone(ZoneInfo('UTC')):%Y%m%d%H%M}"


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
    return data


def recent_time(v):
    return v["t"] if isinstance(v, dict) else v  # old format stored a bare timestamp


def is_repeat(state, key, eid):
    """Seen within DEDUPE_DAYS from a *different* tweet (so re-testing a deleted ID still posts)."""
    v = state["recent"].get(key) if key else None
    return v is not None and not (isinstance(v, dict) and v.get("id") == eid)


def save_state(state):
    cutoff = datetime.now(ZoneInfo("UTC")).timestamp() - DEDUPE_DAYS * 86400
    state["ids"] = state["ids"][-1000:]  # keep newest 1000, in order
    state["recent"] = {k: v for k, v in state["recent"].items() if recent_time(v) >= cutoff}
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1)
    os.replace(tmp, STATE_FILE)  # atomic: never leaves a half-written file


def build_payload(cat, entry, text, tz_label, times, source="AION 2 official X"):
    embed = {
        "title": cat["title"],
        "url": entry.get("link"),
        "description": text[:3500],
        "color": cat["color"],
        "footer": {"text": source[:100]},
    }
    if times:
        embed["fields"] = [{"name": "🕒 Converted times", "value": format_times(tz_label, times)[:1024]}]
    payload = {"embeds": [embed], "allowed_mentions": {"parse": []}}  # no surprise @everyone
    if cat["ping"] and ROLE_ID.isdigit():
        payload["content"] = f"<@&{ROLE_ID}> maintenance incoming"
        payload["allowed_mentions"] = {"roles": [ROLE_ID]}
    return payload


class PostError(Exception):
    pass


def post(payload):
    # Never let the webhook URL reach the log: requests puts the full URL in
    # raise_for_status() and connection-error messages, and public repo logs are public.
    try:
        r = requests.post(WEBHOOK, json=payload, timeout=15)
    except requests.RequestException as e:
        raise PostError(type(e).__name__) from None
    if r.status_code >= 400:
        raise PostError(f"HTTP {r.status_code} {r.text[:200]}")


def feed_key(url: str) -> str:
    """State remembers feeds by hash: the state file is public, feed URLs are secrets."""
    return hashlib.sha256(url.encode()).hexdigest()[:12]


def entry_time(entry) -> datetime | None:
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    return datetime(*t[:6], tzinfo=ZoneInfo("UTC")) if t else None


def fetch_feeds(state):
    """Return ([(entry, source, is_new_feed)] oldest first, failed feed count)."""
    items, failed = [], 0
    for n, url in enumerate(FEED_URLS, 1):
        feed = feedparser.parse(url)
        if not feed.entries:
            # Skip this feed only; an empty first fetch would also cause a backlog flood later.
            reason = type(feed.bozo_exception).__name__ if feed.bozo else "no entries"
            print(f"Feed error (feed {n}): HTTP {feed.get('status', '?')}, {reason}")
            failed += 1
            continue
        key = feed_key(url)
        is_new = key not in state["feeds"]
        if is_new:
            state["feeds"].append(key)
            print(f"feed {n} is new: recording its {len(feed.entries)} existing posts, nothing posted")
        source = feed.feed.get("title") or "AION 2 official X"
        items += [(e, source, is_new) for e in feed.entries]
    # Feeds aren't reliably newest-first, so sort by date (undated last) to post in order.
    far_future = datetime.max.replace(tzinfo=ZoneInfo("UTC"))
    items.sort(key=lambda it: entry_time(it[0]) or far_future)
    return items, failed


def main():
    if not FEED_URLS or not WEBHOOK:
        sys.exit("Set FEED_URL and DISCORD_WEBHOOK_URL")

    state = load_state()
    if state is not None and "feeds" not in state:
        # State from before multi-feed support: the single feed back then was FEED_URL's first line.
        state["feeds"] = [feed_key(FEED_URLS[0])]
    state = state or {"ids": [], "recent": {}, "feeds": []}

    items, failures = fetch_feeds(state)
    if not items:
        sys.exit("Feed error: no feed could be read")  # don't touch state on a bad fetch
    seen_ids = set(state["ids"])
    now = datetime.now(ZoneInfo("UTC")).timestamp()

    for entry, source, is_new_feed in items:
        eid = tweet_key(entry)
        if eid in seen_ids:
            continue
        seen_ids.add(eid)
        state["ids"].append(eid)
        if is_new_feed:
            continue  # don't spam a feed's backlog the first time we see it

        text = clean(entry.get("summary") or entry.get("title", ""))
        cat = classify(text)
        if not cat:
            print(f"skipped (not important) {eid}")
            continue

        published = entry_time(entry)
        try:
            tz_label, times = parse_times(text, published)
        except Exception as e:  # odd date text must never block the post itself
            print(f"time parse failed {eid}: {type(e).__name__}: {e}")
            tz_label, times = SOURCE_TZ, []

        fp = "text:" + content_fingerprint(text)
        wk = window_key(cat, times)
        if is_repeat(state, fp, eid) or is_repeat(state, wk, eid):
            print(f"skipped (duplicate/reminder) {eid}")
            continue

        try:
            post(build_payload(cat, entry, text, tz_label, times, source))
        except PostError as e:
            # Not marked as seen, so it is retried next run instead of being lost.
            state["ids"].remove(eid)
            print(f"post failed {eid}: {e}")
            failures += 1
            continue
        state["recent"][fp] = {"t": now, "id": eid}
        if wk:
            state["recent"][wk] = {"t": now, "id": eid}
        save_state(state)  # save after every post, so a crash can't cause reposts
        print(f"posted {cat['key']} {eid} ({len(times)} times)")
    save_state(state)
    if failures:
        sys.exit(f"{failures} feed/post error(s) above; failed posts are retried next run")  # run shows red


if __name__ == "__main__":
    main()
