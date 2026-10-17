"""Regression tests for aion2_maint_relay.py. Run: python -m pytest -q"""
import json
import os
import time
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import requests

os.environ.setdefault("FEED_URL", "https://feed.example/x.xml")
os.environ.setdefault("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/123/SECRET_TOKEN")
import aion2_maint_relay as r  # noqa: E402

UTC = ZoneInfo("UTC")
PUB = datetime(2026, 10, 8, 1, 0, tzinfo=UTC)


def hm(dt, tz="Asia/Seoul"):
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M")


def parse_flat(text, published):
    """parse_times() as a flat list of datetimes (range starts and ends in order)."""
    tz, ranges = r.parse_times(text, published)
    return tz, [t for s, e in ranges for t in (s, e) if t]


# --- time parsing ---------------------------------------------------------

def test_basic_kst_range_and_targets():
    tz, ts = parse_flat("Maintenance 10/9 10:00 ~ 14:00 (KST)", PUB)
    assert tz == "KST"
    assert [hm(t) for t in ts] == ["2026-10-09 10:00", "2026-10-09 14:00"]
    assert hm(ts[0], "Australia/Brisbane") == "2026-10-09 11:00"
    assert hm(ts[0], "Pacific/Auckland") == "2026-10-09 14:00"  # NZDT in October


@pytest.mark.parametrize("text", [
    "Season 2 - 30 new dungeons, maintenance at 10:00",
    "Patch 0.9 maintenance 10:00",
    "Version 1.5.2 maintenance 10:00",
])
def test_version_numbers_do_not_crash(text):
    _, ts = parse_flat(text, PUB)
    assert len(ts) == 1


def test_utc_offset_is_not_plain_utc():
    tz, ts = parse_flat("Maintenance 10/9 10:00 (UTC+9)", PUB)
    assert tz == "UTC+9"
    assert hm(ts[0]) == "2026-10-09 10:00"
    assert len(ts) == 1  # the "+09" must not be read as a time


def test_korean_and_japanese_suffixes():
    _, ts = parse_flat("10월 9일 10:00부터 14:00까지 점검", PUB)
    assert [hm(t) for t in ts] == ["2026-10-09 10:00", "2026-10-09 14:00"]
    _, ts = parse_flat("10月9日 10:00から14:00までメンテナンス", PUB)
    assert [hm(t) for t in ts] == ["2026-10-09 10:00", "2026-10-09 14:00"]


def test_year_rollover():
    _, ts = parse_flat("Maintenance 1/5 10:00 KST", datetime(2026, 12, 28, tzinfo=UTC))
    assert hm(ts[0]) == "2027-01-05 10:00"


@pytest.mark.parametrize("text", [
    "Maintenance on Oct 15, 10:00 KST",
    "Maintenance on October 15th 10:00 KST",
    "Maintenance on 15 Oct 10:00 KST",
])
def test_english_month_names(text):
    _, ts = parse_flat(text, PUB)
    assert hm(ts[0]) == "2026-10-15 10:00"


def test_am_pm():
    _, ts = parse_flat("Maintenance Oct 9 10:00 AM - 2:00 PM PDT", PUB)
    assert [hm(t, "America/Los_Angeles") for t in ts] == ["2026-10-09 10:00", "2026-10-09 14:00"]


def test_overnight_rolls_to_next_day():
    _, ts = parse_flat("Maintenance 10/9 22:00 ~ 02:00 KST", PUB)
    assert [hm(t) for t in ts] == ["2026-10-09 22:00", "2026-10-10 02:00"]


def test_find_date_ignores_impossible_dates():
    assert r.find_date("Season 2-30 starts 10/9", date(2026, 10, 8)) == date(2026, 10, 9)


# --- classification -------------------------------------------------------

@pytest.mark.parametrize("text,key", [
    ("Scheduled maintenance tomorrow", "maintenance"),
    ("정기 점검 안내", "maintenance"),
    ("Login reward event starts now!", "event"),
    ("Patch notes for 1.2 are live", "update"),
    ("Check out this fan art!", None),
    ("RT @someone: maintenance soon", None),
    ("RT by @aion2: maintenance soon", None),
    ("R to @player: maintenance is at 10", None),
])
def test_classify(text, key):
    cat = r.classify(text)
    assert (cat["key"] if cat else None) == key


# --- webhook secrecy ------------------------------------------------------

def test_http_error_does_not_leak_webhook(monkeypatch):
    monkeypatch.setattr(r.requests, "post", lambda *a, **k: SimpleNamespace(status_code=404, text="Unknown Webhook"))
    with pytest.raises(r.PostError) as e:
        r.post({})
    assert "SECRET_TOKEN" not in str(e.value) and "404" in str(e.value)


def test_connection_error_does_not_leak_webhook(monkeypatch):
    def boom(*a, **k):
        raise requests.ConnectionError(f"Max retries exceeded with url: {r.WEBHOOK}")
    monkeypatch.setattr(r.requests, "post", boom)
    with pytest.raises(r.PostError) as e:
        r.post({})
    assert "SECRET_TOKEN" not in str(e.value)


# --- end-to-end main() ----------------------------------------------------

def entry(tid, text, minute=0):
    return r.feedparser.FeedParserDict(
        link=f"https://x.com/aion2/status/{tid}", summary=text,
        published_parsed=(2026, 10, 8, 1, minute, 0, 0, 0, 0))


@pytest.fixture
def harness(tmp_path, monkeypatch):
    state = tmp_path / "seen.json"
    monkeypatch.setattr(r, "STATE_FILE", str(state))
    sent, feed, fail = [], [], {"on": False}

    def fake_post(payload):
        if fail["on"]:
            raise r.PostError("HTTP 500")
        sent.append(payload)

    monkeypatch.setattr(r, "post", fake_post)
    monkeypatch.setattr(r, "MAX_AGE_HOURS", 10**6)  # fixture dates are fixed, not relative to now
    monkeypatch.setattr(r, "STALE_DAYS", 10**6)     # same reason; the stale-feed tests set it themselves
    feeds = {FEED_A: feed}  # url -> entries (oldest first); missing url = feed down
    problems = {}  # url -> problem text reported while that feed is down
    titles = {}    # url -> feed title (default "src <end of url>")
    monkeypatch.setattr(r, "FEED_URLS", [FEED_A])

    def fake_fetch(url):
        entries = feeds.get(url)
        if not entries:
            return None, problems.get(url, "HTTP 404")
        # feedparser returns newest first
        return SimpleNamespace(entries=list(reversed(entries)), feed={"title": titles.get(url, f"src {url[-5:]}")}), None

    monkeypatch.setattr(r, "fetch_feed", fake_fetch)
    clock = {"now": None}  # None = real time
    monkeypatch.setattr(r, "now_ts", lambda: clock["now"] if clock["now"] is not None else time.time())
    monkeypatch.delenv("LAST_DISPATCH", raising=False)
    x, x_calls = {}, []  # handle -> newest original post on X (missing = X can't tell); call log

    def fake_latest(handle):
        x_calls.append(handle)
        return x.get(handle)

    monkeypatch.setattr(r, "account_latest", fake_latest)
    return SimpleNamespace(state=state, sent=sent, feed=feed, fail=fail, feeds=feeds,
                           problems=problems, clock=clock, titles=titles, x=x, x_calls=x_calls)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    """No role ping, no preview and no real sleeping unless a test asks for it."""
    monkeypatch.setattr(r, "ROLE_ID", "")
    monkeypatch.setattr(r, "PREVIEW", False)
    slept = []
    monkeypatch.setattr(r, "sleep", slept.append)
    return slept


FEED_A = "https://rss.app/feeds/AAAAsecretA.xml"
FEED_B = "https://rss.app/feeds/BBBBsecretB.xml"


def test_end_to_end(harness, capsys):
    h = harness
    h.feed.append(entry(1, "Maintenance 10/9 10:00 ~ 14:00 KST"))
    r.main()  # first run: baseline only
    assert h.sent == [] and json.loads(h.state.read_text())["ids"] == ["1"]

    h.feed += [
        entry(2, "Fan art Friday!", 1),
        entry(3, "Maintenance 10/16 10:00 ~ 14:00 KST", 2),
        entry(4, "Reminder: maintenance starts 10/16 10:00 KST", 3),     # same start -> reminder
        entry(5, "Maintenance 10/16 10:00 ~ 14:00 KST https://t.co/x", 4),  # same text -> dup
        entry(6, "Login reward event 10/20 ~ 10/27", 5),
    ]
    r.main()
    out = capsys.readouterr().out
    assert [p["embeds"][0]["title"] for p in h.sent] == [r.CATEGORIES[0]["title"], r.CATEGORIES[1]["title"]]
    assert "skipped (not important) 2" in out
    assert "skipped (duplicate/reminder) 4" in out and "skipped (duplicate/reminder) 5" in out
    assert "content" not in h.sent[0]  # no DISCORD_ROLE_ID set -> no ping
    assert "Brisbane" in h.sent[0]["embeds"][0]["fields"][0]["value"]

    # Setup guide step 5: delete the last ID and rerun -> it posts again
    st = json.loads(h.state.read_text())
    st["ids"].remove("6")
    h.state.write_text(json.dumps(st))
    r.main()
    assert len(h.sent) == 3


def test_failed_post_is_retried_and_run_fails(harness):
    h = harness
    h.feed.append(entry(1, "hello"))
    r.main()
    h.feed.append(entry(2, "Maintenance 10/9 10:00 KST", 1))
    h.fail["on"] = True
    with pytest.raises(SystemExit):
        r.main()
    assert "2" not in json.loads(h.state.read_text())["ids"]
    h.fail["on"] = False
    r.main()
    assert len(h.sent) == 1


def test_corrupt_state_fails_loudly(harness):
    harness.state.write_text('{"ids": ["1",], "recent": {}}')
    harness.feed.append(entry(1, "hi"))
    with pytest.raises(SystemExit) as e:
        r.main()
    assert "not valid JSON" in str(e.value)


def test_role_ping_only_on_maintenance(monkeypatch):
    monkeypatch.setattr(r, "ROLE_ID", "987654321")
    cat = r.classify("Maintenance @everyone")
    p = r.build_payload(cat, {"link": "https://x.com/a/status/1"}, "Maintenance @everyone", "KST", [])
    assert p["content"].startswith("<@&987654321>")
    assert p["allowed_mentions"] == {"roles": ["987654321"]}  # no "parse" -> @everyone can't fire
    ev = r.build_payload(r.classify("New event!"), {"link": ""}, "New event!", "KST", [])
    assert "content" not in ev and ev["allowed_mentions"] == {"parse": []}


# --- multiple feeds -------------------------------------------------------

def test_feed_url_secret_splits_on_lines_and_commas(monkeypatch):
    monkeypatch.setenv("FEED_URL", f"{FEED_A}\n  {FEED_B} ,\n")
    import importlib
    try:
        assert importlib.reload(r).FEED_URLS == [FEED_A, FEED_B]
    finally:
        monkeypatch.delenv("FEED_URL")
        os.environ["FEED_URL"] = "https://feed.example/x.xml"
        importlib.reload(r)


def test_new_feed_is_baselined_not_flooded(harness, monkeypatch):
    h = harness
    h.feed.append(entry(1, "hi"))
    r.main()
    # Second feed added later with an important backlog tweet: must not post it.
    h.feeds[FEED_B] = [entry(10, "Maintenance 10/9 10:00 KST")]
    monkeypatch.setattr(r, "FEED_URLS", [FEED_A, FEED_B])
    r.main()
    assert h.sent == []
    # A new tweet on feed B after that does post, with feed B's name in the footer.
    h.feeds[FEED_B].append(entry(11, "Login reward event!", 5))
    r.main()
    assert len(h.sent) == 1
    assert h.sent[0]["embeds"][0]["url"].endswith("/status/11")  # feed B's new tweet
    # The state file is public: it must never contain the feed URLs.
    assert "secret" not in h.state.read_text()


def test_posts_are_sorted_by_date_across_feeds(harness, monkeypatch):
    h = harness
    h.feed.append(entry(1, "hi"))
    h.feeds[FEED_B] = [entry(2, "hi", 1)]
    monkeypatch.setattr(r, "FEED_URLS", [FEED_A, FEED_B])
    r.main()
    h.feed.append(entry(30, "Patch notes later", 30))
    h.feeds[FEED_B].append(entry(20, "Maintenance earlier 10/9 10:00 KST", 20))
    h.feed.append(entry(20, "Maintenance earlier 10/9 10:00 KST", 20))  # same tweet in both feeds
    r.main()
    assert [p["embeds"][0]["url"][-2:] for p in h.sent] == ["20", "30"]


def test_one_feed_down_others_still_post(harness, monkeypatch):
    h = harness
    h.feeds[FEED_B] = [entry(2, "hi")]
    h.feed.append(entry(1, "hi"))
    monkeypatch.setattr(r, "FEED_URLS", [FEED_A, FEED_B])
    r.main()
    del h.feeds[FEED_B]
    h.feed.append(entry(3, "Maintenance 10/9 10:00 KST", 3))
    with pytest.raises(SystemExit):  # run shows red...
        r.main()
    assert len(h.sent) == 1  # ...but feed A still posted


def test_old_state_without_feeds_is_not_rebaselined(harness):
    h = harness
    h.state.write_text(json.dumps({"ids": ["1"], "recent": {}}))  # pre-multi-feed state
    h.feed += [entry(1, "hi"), entry(2, "Maintenance 10/9 10:00 KST", 1)]
    r.main()
    assert len(h.sent) == 1


def at(tid, text, hours_ago):
    t = datetime.now(UTC) - timedelta(hours=hours_ago)
    return r.feedparser.FeedParserDict(link=f"https://x.com/aion2/status/{tid}", summary=text,
                                       published_parsed=t.timetuple()[:6] + (0, 0, 0))


def test_old_tweets_suddenly_in_feed_are_not_posted(harness, monkeypatch, capsys):
    # A known feed starts exposing older history it never showed before (e.g. rss.app plan change).
    h = harness
    monkeypatch.setattr(r, "MAX_AGE_HOURS", 48)
    h.feed.append(at(1, "hi", 1))
    r.main()
    h.feed[:0] = [at(5, "Login reward event!", 72)]  # old, newly visible
    h.feed.append(at(6, "Patch notes are live", 0.1))  # genuinely new
    r.main()
    assert [p["embeds"][0]["url"][-1] for p in h.sent] == ["6"]
    assert "skipped (too old) 5" in capsys.readouterr().out


def test_new_feed_still_posts_very_recent_tweets(harness, monkeypatch):
    # Adding a feed shouldn't swallow something tweeted minutes earlier (AION2_JP case).
    h = harness
    h.feed.append(at(1, "hi", 1))
    r.main()
    h.feeds[FEED_B] = [at(10, "Maintenance 10/1 10:00 KST", 30), at(11, "臨時メンテナンス 19:45より", 0.5)]
    monkeypatch.setattr(r, "FEED_URLS", [FEED_A, FEED_B])
    r.main()
    assert [p["embeds"][0]["url"][-2:] for p in h.sent] == ["11"]


@pytest.mark.parametrize("text", [
    "📢臨時メンテナンス中止のお知らせ 本日19:45より予定しておりましたメンテナンスは中止",
    "Today's maintenance has been cancelled",
    "오늘 예정된 점검이 취소되었습니다",
])
def test_cancelled_maintenance_gets_its_own_title_and_no_ping(text, monkeypatch):
    monkeypatch.setattr(r, "ROLE_ID", "987654321")
    cat = r.classify(text)
    assert cat["key"] == "maintenance"
    p = r.build_payload(cat, {"link": ""}, text, "KST", [])
    assert "cancel" in p["embeds"][0]["title"].lower() and "content" not in p


@pytest.mark.parametrize("link,author", [
    ("https://x.com/AION2_JP/status/2108147189541376329", "@AION2_JP"),
    ("https://twitter.com/AION2Official/status/1", "@AION2Official"),
    ("https://example.com/item/1", "src feed"),
])
def test_post_shows_source_account(link, author):
    p = r.build_payload(r.classify("Patch notes"), {"link": link}, "Patch notes", "KST", [], "src feed")
    assert p["embeds"][0]["author"]["name"] == author


def test_times_are_12_hour():
    _, ts = r.parse_times("メンテナンス 10月8日 19:45 ~ 00:30 JST", PUB)
    out = r.format_times("JST", ts)
    # Overnight range: end shows its own date; midnight hour is 12, not 0.
    assert "**Thu 8 Oct 7:45 pm – Fri 9 Oct 12:30 am JST**" in out
    assert "Brisbane: Thu 8 Oct 8:45 pm – Fri 9 Oct 1:30 am AEST" in out
    assert "New Zealand: Thu 8 Oct 11:45 pm – Fri 9 Oct 4:30 am NZDT" in out
    assert "19:45" not in out and "20:45" not in out


SCREENSHOT_TEXT = """Notice of temporary maintenance cancellation
We have decided to cancel the maintenance scheduled for today, October 8th (Thursday) 19:45.
#AION2 #Aion2AION2 (@AION2_JP) [Notice of temporary maintenance on October 8th (Thursday)]
Implementation date and time
Thursday, October 8th 19:45-20:45 (1 hour 00 minutes)"""


def test_quoted_repeat_collapses_to_one_range():
    tz, ts = r.parse_times(SCREENSHOT_TEXT, PUB, "JST")
    assert tz == "JST"
    assert [(hm(s), hm(e)) for s, e in ts] == [("2026-10-08 19:45", "2026-10-08 20:45")]
    out = r.format_times(tz, ts)
    assert out.count("Brisbane") == 1
    assert "**Thu 8 Oct 7:45 – 8:45 pm JST**" in out
    assert "Brisbane: Thu 8 Oct 8:45 – 9:45 pm AEST" in out
    assert "New Zealand: Thu 8 Oct 11:45 pm – Fri 9 Oct 12:45 am NZDT" in out
    assert "Your time: <t:" in out and "> – <t:" in out and ":t>" in out


@pytest.mark.parametrize("text,expected", [
    ("Maint 10:00 am to 2:00 pm KST", "10:00 am – 2:00 pm"),
    ("Maint 9:00 ~ 11:30 KST", "9:00 – 11:30 am"),
    ("Maint 10:00, servers open 14:00 KST", None),  # comma: two separate times
])
def test_range_detection(text, expected):
    _, ts = r.parse_times(text, PUB)
    if expected:
        assert len(ts) == 1 and expected in r.format_times("KST", ts)
    else:
        assert [e for s, e in ts] == [None, None]


# --- "maintenance" used only as a deadline ---------------------------------

GIVEAWAY_EN = """📢AION2 is hot right now!🔥

Thanks to your warm support,
Not only "AION2" but also "Character Creation" are trending in real time!🎉

To express our gratitude for the overwhelming response, we will be giving away a "Character appearance change ticket (7 days) (engraved) x1" to all Diva who log in to the game before the start of regular maintenance on October 14th (Wednesday)!🎁

Enjoy "AION2" even more with your unique character🪽

🎁Gift items
Character appearance change ticket (7 days) (engraved) ×1"""


@pytest.mark.parametrize("text", [
    GIVEAWAY_EN,
    "📢今、AION2が熱い！🔥 10月14日(水)定期メンテナンス開始前までにゲームへログインしたすべてのディーヴァの皆さまへ「キャラクター外見変更券」をプレゼント！",
    "정기 점검 전까지 접속한 모든 분께 선물을 드립니다!",
    "Double EXP weekend! Runs until the next maintenance.",
    "Log in after this week's maintenance to receive a gift!",
])
def test_deadline_mention_of_maintenance_is_not_maintenance(text):
    cat = r.classify(text)
    assert cat is not None and cat["key"] == "event"


@pytest.mark.parametrize("text", [
    "🪽Weekly Maintenance When: October 6, 2026 at 23:30 PDT / October 7, 2026 at 8h30 CEST",
    "Day 1 Servers are currently under maintenance⚔️ Thank you for joining us in Atreia today!",
    "[Notice of temporary maintenance on October 9th (Friday)] We will be conducting maintenance on the following dates.",
    "【10月9日(金) 臨時メンテナンス実施のお知らせ】 下記の日程でメンテナンスを実施いたします。",
    "정기 점검 안내: 10월 14일 06:00~10:00",
    "Patch notes for 1.2 are out! Maintenance will be held 10:00-14:00 KST to apply this update.",
    "Emergency maintenance notice. Please log out before the maintenance begins.",
])
def test_real_maintenance_notices_still_match(text):
    assert r.classify(text)["key"] == "maintenance"


def test_after_maintenance_patch_notes_are_an_update():
    assert r.classify("Patch notes will be released after the maintenance.")["key"] == "update"


# --- feed fetching and Discord rate limits ---------------------------------

RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>AION2 (@AION2_JP) / Twitter</title>
<item><title>x</title><link>https://x.com/AION2_JP/status/1</link><description>hi</description></item>
</channel></rss>"""


class Resp:
    def __init__(self, status=200, content=b"", headers=None, json_data=None):
        self.status_code, self.content, self.headers, self._json = status, content, headers or {}, json_data
        self.text = ""

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def scripted(monkeypatch, name, *outcomes):
    """Patch requests.<name> to return/raise the given outcomes in order; returns the call log."""
    calls, queue = [], list(outcomes)

    def fake(*args, **kwargs):
        calls.append((args, kwargs))
        out = queue.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(r.requests, name, fake)
    return calls


def test_fetch_feed_timeout_and_user_agent(monkeypatch):
    calls = scripted(monkeypatch, "get", Resp(200, RSS))
    feed, problem = r.fetch_feed(FEED_A)
    assert problem is None and len(feed.entries) == 1
    kwargs = calls[0][1]
    assert kwargs["timeout"] == (5, 20) and "aion2-relay" in kwargs["headers"]["User-Agent"]


def test_fetch_feed_retries_once_on_connection_error(monkeypatch, isolate):
    calls = scripted(monkeypatch, "get", requests.ConnectionError("boom"), Resp(200, RSS))
    feed, problem = r.fetch_feed(FEED_A)
    assert problem is None and len(calls) == 2 and isolate == [3]


def test_fetch_feed_retries_once_on_5xx(monkeypatch):
    calls = scripted(monkeypatch, "get", Resp(503), Resp(200, RSS))
    assert r.fetch_feed(FEED_A)[1] is None and len(calls) == 2


def test_fetch_feed_404_not_retried(monkeypatch):
    calls = scripted(monkeypatch, "get", Resp(404))
    assert r.fetch_feed(FEED_A) == (None, "HTTP 404") and len(calls) == 1


def test_fetch_feed_empty_is_an_error(monkeypatch):
    scripted(monkeypatch, "get", Resp(200, b"<rss><channel></channel></rss>"))
    feed, problem = r.fetch_feed(FEED_A)
    assert feed is None and problem


def test_fetch_errors_never_contain_url(monkeypatch):
    err = requests.ConnectionError(f"Max retries exceeded with url: {FEED_A}")
    scripted(monkeypatch, "get", err, err)
    feed, problem = r.fetch_feed(FEED_A)
    assert feed is None and problem == "ConnectionError" and "secret" not in problem


def test_post_429_waits_retry_after_header(monkeypatch, isolate):
    calls = scripted(monkeypatch, "post", Resp(429, headers={"Retry-After": "2"}), Resp(204))
    r.post({})
    assert len(calls) == 2 and isolate == [2.0]


def test_post_429_json_retry_after(monkeypatch, isolate):
    scripted(monkeypatch, "post", Resp(429, json_data={"retry_after": 1.5}), Resp(204))
    r.post({})
    assert isolate == [1.5]


def test_post_429_long_wait_gives_up_without_sleeping(monkeypatch, isolate):
    scripted(monkeypatch, "post", Resp(429, headers={"Retry-After": "60"}))
    with pytest.raises(r.PostError) as e:
        r.post({})
    assert e.value.status == 429 and isolate == []


def test_post_429_three_times_gives_up(monkeypatch, isolate):
    scripted(monkeypatch, "post", *[Resp(429, headers={"Retry-After": "1"})] * 3)
    with pytest.raises(r.PostError) as e:
        r.post({})
    assert e.value.status == 429 and isolate == [1.0, 1.0]


# --- preview mode -----------------------------------------------------------

def test_preview_posts_and_saves_nothing(harness, monkeypatch, capsys):
    h = harness
    h.feed += [at(1, "hi", 5), at(2, "Maintenance 10/9 10:00 ~ 14:00 KST", 4)]
    r.main()  # baseline: both seen
    before = h.state.read_bytes()
    h.feed += [at(i, f"Patch notes {i}", 2 - i / 10) for i in range(3, 8)]  # 7 is newest
    monkeypatch.setattr(r, "PREVIEW", True)
    capsys.readouterr()
    r.main()
    out = capsys.readouterr().out
    assert h.sent == [] and h.state.read_bytes() == before
    assert out.count("=== feed 1") == 3                     # newest 3 only
    assert "would post update" in out and '"embeds"' in out
    assert "=== feed 1 · 7 ·" in out                        # newest first


def test_preview_shows_already_posted_tweets(harness, monkeypatch, capsys):
    h = harness
    h.feed.append(at(1, "hi", 5))
    r.main()
    h.feed.append(at(2, "Patch notes are live", 1))
    r.main()  # really posted
    assert len(h.sent) == 1
    monkeypatch.setattr(r, "PREVIEW", True)
    capsys.readouterr()
    r.main()
    assert "would post update" in capsys.readouterr().out and len(h.sent) == 1


def test_preview_redacts_secrets(harness, monkeypatch, capsys):
    h = harness
    h.feed.append(at(1, f"Patch notes https://rss.app/feeds/xyz.xml and {FEED_A}", 1))
    monkeypatch.setattr(r, "PREVIEW", True)
    r.main()
    out = capsys.readouterr().out
    assert "rss.app/feeds" not in out and "secretA" not in out
    assert "[rss.app URL]" in out


def test_preview_without_webhook(harness, monkeypatch, capsys):
    harness.feed.append(at(1, "Patch notes", 1))
    monkeypatch.setattr(r, "PREVIEW", True)
    monkeypatch.setattr(r, "WEBHOOK", None)
    r.main()
    assert "would post update" in capsys.readouterr().out


def test_preview_shows_text_of_skipped_tweets(harness, monkeypatch, capsys):
    harness.feed.append(at(1, "Fan art Friday!", 1))
    monkeypatch.setattr(r, "PREVIEW", True)
    r.main()
    out = capsys.readouterr().out
    assert "skip (not important)" in out and 'text: "Fan art Friday!"' in out


# --- step D: classification, quotes, statuses and zones (real rss.app tweets) ---------------

CORPUS = json.load(open(os.path.join(os.path.dirname(__file__), "test_data", "rssapp_corpus.json"),
                        encoding="utf-8"))
BNE = ZoneInfo("Australia/Brisbane")
JP_SOURCE, GL_SOURCE = "AION2 (@AION2_JP) / Twitter", "AION 2 (@AION2Official) / Twitter"


def snowflake_time(tweet_id):
    return datetime.fromtimestamp(((int(tweet_id) >> 22) + 1288834974657) / 1000, UTC)


def feed_entry(link, text, published=None):
    published = published or snowflake_time(TWEET_ID.search(link).group(1))
    return r.feedparser.FeedParserDict(link=link, summary=text, published_parsed=published.timetuple())


TWEET_ID = __import__("re").compile(r"/status/(\d+)")


def fresh_state():
    return {"ids": [], "recent": {}, "feeds": []}


def verdict_of(entry, source):
    verdict, info = r.decide(entry, source, fresh_state())
    if verdict != "post":
        return verdict, info, []
    kind = info["cat"]["key"] + (f"/{info['status']}" if info["status"] else "")
    spans = [r.fmt_span(s.astimezone(BNE), e.astimezone(BNE) if e else None) for s, e in info["times"]]
    return kind, info, spans


@pytest.mark.parametrize("rec", CORPUS, ids=[c["id"] for c in CORPUS])
def test_rssapp_corpus(rec):
    """97 real rss.app tweets (both accounts, Jun-Oct 2026) with reviewed verdicts and Brisbane times."""
    kind, _, spans = verdict_of(feed_entry(rec["link"], rec["text"]), rec["source"])
    assert (kind, spans) == (rec["expected"], rec["brisbane"])


def test_trailer_date_is_not_the_tweet_date():
    # Published 23:30 UTC on Oct 9 = Oct 10 in Japan; the embed trailer still says "Oct 9".
    e = feed_entry("https://x.com/AION2_JP/status/1", "Emergency maintenance today from 19:45\n\n #AION2— AION2 (@AION2_JP)Oct 9, 2026",
                   datetime(2026, 10, 9, 23, 30, tzinfo=UTC))
    kind, _, spans = verdict_of(e, JP_SOURCE)
    assert kind == "maintenance" and spans == ["Sat 10 Oct 8:45 pm"]


def test_same_text_on_another_day_is_still_a_duplicate():
    state = fresh_state()
    first = feed_entry("https://x.com/AION2_JP/status/1", "Patch notes are live\n\n — AION2 (@AION2_JP)Oct 9, 2026")
    again = feed_entry("https://x.com/AION2_JP/status/2", "Patch notes are live\n\n — AION2 (@AION2_JP)Oct 10, 2026")
    verdict, info = r.decide(first, JP_SOURCE, state)
    state["recent"][info["fp"]] = {"t": 0, "id": "1"}
    assert r.decide(again, JP_SOURCE, state)[0] == "duplicate/reminder"


def test_split_quote():
    text = ("Hello, Daevas! Server Transfer Q&A\n\nLearn more here:  store.steampowered.com/news/…AION 2 (@AION2Official)"
            "Server transfers will be available starting on October 14th\n\nLearn more:  aion2.plaync.com/en-us/board…"
            "—  https://x.com/AION2Official/status/2105328638367605138")
    own, quote, url = r.split_quote(text, "AION 2")
    assert own == "Hello, Daevas! Server Transfer Q&A\n\nLearn more here:  store.steampowered.com/news/…"
    assert quote.startswith("Server transfers will be available") and quote.endswith("board…")
    assert url == "https://x.com/AION2Official/status/2105328638367605138"
    assert r.split_quote("No quote here", "AION 2") == ("No quote here", "", None)


def test_quote_shows_as_block_quote_and_its_times_are_not_used():
    rec = next(c for c in CORPUS if c["id"] == "2108467686644781299")  # "maintenance complete", quoting the notice
    kind, info, spans = verdict_of(feed_entry(rec["link"], rec["text"]), rec["source"])
    desc = info["payload"]["embeds"][0]["description"]
    assert "> ↪ [Quoted post](https://x.com/AION2_JP/status/2108181948137185376)" in desc
    assert "#Aion2AION2" not in desc and spans == ["Fri 9 Oct 6:00 pm"]


def test_retweet_of_a_fan_is_skipped():
    e = feed_entry("https://x.com/somefan/status/2109000000000000000", "Got the event mount! #AION2— @somefan Oct 12, 2026")
    assert r.decide(e, GL_SOURCE, fresh_state())[0] == "retweet"


def test_free_pet_loot_drop_is_an_event():
    text = ("🪽Exclusive loot drop🪽\n\nCreators have been battling it out in Atreia  and Series 2 unlocked a new "
            "wave of in-game rewards, including a FREE PET!⚔️\n\n*Bound items are non-tradable.\n#AION2 #AION2Global")
    assert r.classify(text)["key"] == "event"


@pytest.mark.parametrize("text,expected", [
    ("Join live on Twitch October 9 @ 1:00 PM PT / 22:00 CET", ["Sat 10 Oct 6:00 am"]),
    ("Join live on Twitch Mon, October 12 · 9:00 AM PT / 18:00 CEST", ["Tue 13 Oct 2:00 am"]),
    ("When: October 6, 2026 at 23:30 PDT / October 7, 2026 at 08:30 CEST", ["Wed 7 Oct 4:30 pm"]),
    ("Maintenance 10/9 10:00 KST - 14:00 KST", ["Fri 9 Oct 11:00 am – 3:00 pm"]),
])
def test_each_time_uses_its_own_zone(text, expected):
    _, ts = r.parse_times(text, datetime(2026, 10, 8, tzinfo=UTC))
    assert [r.fmt_span(s.astimezone(BNE), e.astimezone(BNE) if e else None) for s, e in ts] == expected


def test_short_zone_names_need_capitals():
    assert r.parse_times("Opt in at 10:00 pt", PUB)[0] == "KST"


@pytest.mark.parametrize("text,status", [
    ("Maintenance has been extended to 16:00 KST", "extended"),
    ("Extended Maintenance notice: 10/9 10:00-16:00 KST", None),
    ("Maintenance notice 10/9 10:00 KST. Once maintenance has ended, servers will reopen.", None),
    ("The maintenance has been postponed to October 15", "rescheduled"),
    ("Maintenance has been completed. Servers are now open!", "complete"),
    ("Maintenance will end early, at 15:00 KST", "early"),
    ("Today's maintenance is cancelled", "cancelled"),
    # rss.app's English for 「作業が完了しました為、19:30にサーバーをオープンいたしました」 (10-14)
    ("The regular maintenance carried out on October 14th (Wednesday) was completed as scheduled, "
     "and the server was opened at 19:30. You can now play the game as usual.", "complete"),
    ("Maintenance 10/9: the servers were opened at 17:00 KST.", "complete"),
    ("Maintenance notice: when the work was completed, we opened the servers.", "complete"),  # past after "when"
    ("Maintenance is expected to be completed as scheduled at 15:00 KST", None),
    ("Maintenance 10/20 14:00 KST. You can play as usual until 13:30.", None),
])
def test_maintenance_status(text, status):
    assert r.maint_status(r.classify(text), text) == status


@pytest.mark.parametrize("text,title", [
    ("Maintenance has been completed. Servers are now open!", "✅ Maintenance complete — servers are back"),
    ("Maintenance will end early, at 15:00 KST", "⏩ Maintenance ending early"),
    ("Maintenance has been extended to 16:00 KST", "⏳ Maintenance extended"),
])
def test_status_posts_never_ping(monkeypatch, text, title):
    monkeypatch.setattr(r, "ROLE_ID", "987654321")
    p = r.build_payload(r.classify(text), {"link": ""}, text, "KST", [])
    assert p["embeds"][0]["title"] == title and "content" not in p


# --- step E: better-looking posts --------------------------------------------

OEMBED = ('<blockquote class="twitter-tweet"><p lang="en" dir="ltr">Patch notes are live!<br><br>'
          'Click here <a href="https://t.co/abc">aion2.plaync.com/en-us/board…</a><br><br>'
          '<a href="https://twitter.com/hashtag/AION2?src=hash">#AION2</a> '
          '<a href="https://t.co/pic">pic.twitter.com/xyz</a></p>&mdash; AION 2 (@AION2Official) '
          '<a href="https://twitter.com/AION2Official/status/5">October 9, 2026</a></blockquote>')


def test_links_become_clickable_and_clutter_goes():
    e = feed_entry("https://x.com/AION2Official/status/2108229078675521703", OEMBED)
    kind, info, _ = verdict_of(e, GL_SOURCE)
    desc = info["payload"]["embeds"][0]["description"]
    assert kind == "update"
    assert desc == "Patch notes are live!\n\nClick here [aion2.plaync.com/en-us/board…](https://t.co/abc)"


def test_md_link_escaping():
    assert r.md_link("a]b [c", "https://x.com/a (b)") == "[a)b (c](https://x.com/a%20%28b%29)"


def test_truncate_never_splits_link():
    text = "x" * 20 + "[label](https://example.com/long)" + "y" * 20
    out = r.truncate_md(text, 30)
    assert out == "x" * 20 + "…" and "[label" not in out
    assert r.truncate_md("short", 30) == "short"


@pytest.mark.parametrize("entry,expected", [
    ({"media_content": [{"url": "https://pbs.twimg.com/media/A.jpg", "medium": "image"}]}, "https://pbs.twimg.com/media/A.jpg"),
    ({"media_content": [{"url": "https://pbs.twimg.com/media/B?format=jpg&name=large"}]}, "https://pbs.twimg.com/media/B?format=jpg&name=large"),
    ({"media_thumbnail": [{"url": "https://pbs.twimg.com/media/C.png"}]}, "https://pbs.twimg.com/media/C.png"),
    ({"enclosures": [{"href": "https://pbs.twimg.com/media/D.jpg", "type": "image/jpeg"}]}, "https://pbs.twimg.com/media/D.jpg"),
    ({"summary": '<p>hi <img alt="x" src="https://pbs.twimg.com/media/E.jpg"></p>'}, "https://pbs.twimg.com/media/E.jpg"),
    ({"media_content": [{"url": "http://insecure.example/F.jpg", "medium": "image"}]}, None),
    ({"media_content": [{"url": "https://rss.app/img/G.jpg", "medium": "image"}]}, None),
    ({"summary": '<img src="https://abs.twimg.com/emoji/v2/72x72/1f389.png">'}, None),
    ({"media_content": [{"url": "https://video.twimg.com/H.mp4", "medium": "video"}]}, None),
    ({}, None),
])
def test_entry_image(entry, expected):
    assert r.entry_image(entry) == expected


def test_payload_has_image_timestamp_and_footer():
    e = feed_entry("https://x.com/AION2_JP/status/2108543188281733255", "Patch notes are live!",
                   datetime(2026, 10, 9, 13, 0, tzinfo=UTC))
    e["media_content"] = [{"url": "https://pbs.twimg.com/media/A.jpg", "medium": "image"}]
    embed = r.decide(e, JP_SOURCE, fresh_state())[1]["payload"]["embeds"][0]
    assert embed["image"] == {"url": "https://pbs.twimg.com/media/A.jpg"}
    assert embed["timestamp"] == "2026-10-09T13:00:00+00:00"
    assert embed["footer"]["text"] == "via X · translated from Japanese"
    jp = feed_entry("https://x.com/AION2_JP/status/1", "アップデートのお知らせ")
    assert r.decide(jp, JP_SOURCE, fresh_state())[1]["payload"]["embeds"][0]["footer"]["text"] == "via X"


def test_rejected_image_is_retried_without_it(monkeypatch):
    sent = []

    def fake_post(payload):
        if any("image" in em for em in payload["embeds"]):
            raise r.PostError("HTTP 400 bad image", 400)
        sent.append(payload)

    monkeypatch.setattr(r, "post", fake_post)
    r.deliver({"embeds": [{"title": "t", "image": {"url": "https://pbs.twimg.com/media/bad.jpg"}}]})
    assert sent == [{"embeds": [{"title": "t"}]}]


def test_other_400s_are_not_retried(monkeypatch):
    def fake_post(payload):
        raise r.PostError("HTTP 400", 400)
    monkeypatch.setattr(r, "post", fake_post)
    with pytest.raises(r.PostError):
        r.deliver({"embeds": [{"title": "no image"}]})


@pytest.mark.parametrize("raw", [
    "", "<a href='x'>", "<a>no href</a>", "<img>", "<p><a href=\"https://t.co/1\"></a></p>",
    "<<<>>>", "&mdash;&mdash;", "<blockquote><p>Patch notes <a href='javascript:alert(1)'>click</a></p></blockquote>",
])
def test_odd_html_never_crashes(raw):
    e = r.feedparser.FeedParserDict(link="https://x.com/AION2Official/status/9", summary=raw)
    verdict, info = r.decide(e, GL_SOURCE, fresh_state())
    if verdict == "post":
        assert "javascript:" not in info["payload"]["embeds"][0]["description"]


# --- step F: maintenance end times and reminders ------------------------------

def test_weekly_notice_two_labelled_ranges():
    weekly = next(c for c in CORPUS if c["id"] == "2107507473251004529")
    _, info, _ = verdict_of(feed_entry(weekly["link"], weekly["text"]), weekly["source"])
    assert info["labels"] == ["NA, EU, LATAM", "ASIA"] and info["wk"] == "maint:202610070630"
    field = info["payload"]["embeds"][0]["fields"][0]["value"]
    assert "**NA, EU, LATAM: Tue 6 Oct 11:30 pm – Wed 7 Oct 3:00 am PDT**" in field
    assert "**ASIA: Tue 6 Oct 11:30 pm – Wed 7 Oct 5:00 am PDT**" in field
    assert "Brisbane: Wed 7 Oct 4:30 – 10:00 pm AEST" in field


def test_duration_never_overrides_a_range():
    ts = r.parse_times(SCREENSHOT_TEXT, PUB, "JST")[1]
    assert r.add_duration_ends(SCREENSHOT_TEXT, ts) == (ts, [None])
    text = "Maintenance 10/9 19:45-20:45 (1時間30分)"
    ts = r.parse_times(text, PUB)[1]
    assert r.add_duration_ends(text, ts)[0] == ts


def test_bracketed_duration_gives_an_end_time():
    text = "メンテナンス 10月9日 19:45より(1時間30分)"
    ts, labels = r.add_duration_ends(text, r.parse_times(text, PUB, "JST")[1])
    assert [(hm(s), hm(e)) for s, e in ts] == [("2026-10-09 19:45", "2026-10-09 21:15")] and labels == [None]


def test_duration_needs_context():
    text = "Maintenance 10/9 10:00 KST. The patch brings 2 hours of new story content."
    ts = r.parse_times(text, PUB)[1]
    assert r.add_duration_ends(text, ts) == (ts, [None])


T0 = datetime(2026, 10, 20, 0, 0, tzinfo=UTC).timestamp()  # Tue 20 Oct, 09:00 KST
START = T0 + 5 * 3600                                       # 14:00 KST
ANNOUNCE = "Maintenance 10/20 14:00 ~ 18:00 KST"


def tweet(tid, text, ts):
    return r.feedparser.FeedParserDict(link=f"https://x.com/AION2Official/status/{tid}", summary=text,
                                       published_parsed=datetime.fromtimestamp(ts, UTC).timetuple())


def run_at(h, ts):
    h.clock["now"] = ts
    r.main()


def announce(h, text=ANNOUNCE, ts=T0):
    h.feed.append(tweet(1, "hi", T0 - 3600))
    run_at(h, T0 - 3600)  # baseline
    h.feed.append(tweet(2, text, ts))
    run_at(h, ts)


def test_reminder_once_about_an_hour_before(harness):
    h = harness
    announce(h)
    assert len(h.sent) == 1
    assert json.loads(h.state.read_text())["upcoming"]["maint:202610200500"]["start"] == START
    for minutes_before in (90, 75):
        run_at(h, START - minutes_before * 60)
    assert len(h.sent) == 1                                     # not yet
    run_at(h, START - 55 * 60)
    assert len(h.sent) == 2
    reminder = h.sent[1]["embeds"][0]
    assert reminder["title"] == "⏰ Maintenance starts in ~55 min"
    assert "Brisbane: Tue 20 Oct 3:00 – 7:00 pm AEST" in reminder["fields"][0]["value"]
    assert reminder["url"].endswith("/status/2") and "content" not in h.sent[1]
    for minutes_before in (40, 10, -5):
        run_at(h, START - minutes_before * 60)
    assert len(h.sent) == 2                                     # once only, never after the start
    run_at(h, START + 25 * 3600)
    assert json.loads(h.state.read_text())["upcoming"] == {}    # pruned a day after the start


def test_reminder_pings_role_when_set(harness, monkeypatch):
    monkeypatch.setattr(r, "ROLE_ID", "123")
    announce(harness)
    run_at(harness, START - 55 * 60)
    assert harness.sent[1]["content"] == "<@&123> maintenance starts in ~55 min"
    assert harness.sent[1]["allowed_mentions"] == {"roles": ["123"]}


def test_no_reminder_when_announced_close_to_start(harness):
    announce(harness, "Maintenance 10/20 10:10 KST")          # 70 min ahead: the post is the warning
    run_at(harness, T0 + 15 * 60)
    assert len(harness.sent) == 1


def test_reminder_tweet_does_not_double_up(harness):
    h = harness
    announce(h)
    h.feed.append(tweet(3, "Reminder: maintenance starts 10/20 14:00 KST", T0 + 3600))
    run_at(h, T0 + 3600)
    run_at(h, START - 55 * 60)
    assert len(h.sent) == 2 and len(json.loads(h.state.read_text())["upcoming"]) == 1


def test_cancellation_with_a_time_cancels_the_reminder(harness):
    h = harness
    announce(h)
    h.feed.append(tweet(3, "Today's maintenance (10/20 14:00 KST) is cancelled", T0 + 3600))
    run_at(h, T0 + 3600)
    run_at(h, START - 55 * 60)
    assert [p["embeds"][0]["title"] for p in h.sent] == ["🚨 MAINTENANCE — servers going down", "✅ Maintenance cancelled"]


def test_cancellation_without_a_time_cancels_the_only_pending_window(harness):
    h = harness
    announce(h)
    h.feed.append(tweet(3, "Today's maintenance has been cancelled.", T0 + 3600))
    run_at(h, T0 + 3600)
    run_at(h, START - 55 * 60)
    assert len(h.sent) == 2 and "cancelled" in h.sent[1]["embeds"][0]["title"]


def test_reschedule_moves_the_reminder(harness):
    h = harness
    announce(h)
    h.feed.append(tweet(3, "Maintenance has been postponed to 10/21 14:00 KST", T0 + 3600))
    run_at(h, T0 + 3600)
    run_at(h, START - 55 * 60)
    assert len(h.sent) == 2                                     # old time: no reminder
    run_at(h, START + 86400 - 55 * 60)
    assert h.sent[2]["embeds"][0]["title"] == "⏰ Maintenance starts in ~55 min"
    assert h.sent[2]["embeds"][0]["url"].endswith("/status/3")


def test_failed_reminder_is_retried(harness):
    h = harness
    announce(h)
    h.fail["on"] = True
    with pytest.raises(SystemExit):
        run_at(h, START - 55 * 60)
    h.fail["on"] = False
    run_at(h, START - 50 * 60)
    assert h.sent[-1]["embeds"][0]["title"] == "⏰ Maintenance starts in ~50 min"


def test_preview_shows_due_reminders(harness, monkeypatch, capsys):
    h = harness
    announce(h)
    before = h.state.read_bytes()
    monkeypatch.setattr(r, "PREVIEW", True)
    h.clock["now"] = START - 55 * 60
    capsys.readouterr()
    r.main()
    assert "⏰ Maintenance starts in ~55 min" in capsys.readouterr().out
    assert len(h.sent) == 1 and h.state.read_bytes() == before


# --- step G: problem alerts ---------------------------------------------------

def alerts(h):
    return [p["content"] for p in h.sent if "embeds" not in p]


def test_feed_down_alert_after_an_hour_then_recovery(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(r, "FEED_URLS", [FEED_A, FEED_B])
    h.titles[FEED_B] = "AION2 (@AION2_JP) / Twitter"
    h.feed.append(tweet(1, "hi", T0 - 7200))
    h.feeds[FEED_B] = [tweet(2, "hi", T0 - 7200)]
    run_at(h, T0)                                           # both load (baseline)
    del h.feeds[FEED_B]
    for minutes in (0, 30):
        with pytest.raises(SystemExit):                     # every failing run still shows red
            run_at(h, T0 + 900 + minutes * 60)
    assert alerts(h) == []                                  # under an hour: quiet
    with pytest.raises(SystemExit):
        run_at(h, T0 + 900 + 65 * 60)
    assert alerts(h) == ["⚠️ Feed 2 (@AION2_JP) hasn't loaded for over an hour (HTTP 404) — check rss.app"]
    after_alert = h.state.read_bytes()
    with pytest.raises(SystemExit):
        run_at(h, T0 + 900 + 80 * 60)
    assert len(alerts(h)) == 1 and h.state.read_bytes() == after_alert  # no repeat, nothing to commit
    h.feeds[FEED_B] = [tweet(2, "hi", T0 - 7200)]
    run_at(h, T0 + 900 + 95 * 60)
    assert alerts(h)[-1] == "✅ Feed 2 (@AION2_JP) is working again (was down ~2 h)"
    assert "secret" not in "".join(alerts(h))


def test_short_outage_is_silent(harness):
    h = harness
    h.feed.append(tweet(1, "hi", T0 - 7200))
    run_at(h, T0)
    saved = list(h.feed)
    h.feed.clear()
    with pytest.raises(SystemExit):
        run_at(h, T0 + 900)
    h.feed.extend(saved)
    run_at(h, T0 + 1800)
    assert alerts(h) == []
    assert "fail_since" not in json.loads(h.state.read_text())["feed_health"][r.feed_key(FEED_A)]


def test_all_feeds_down_still_alerts_and_reminds(harness):
    h = harness
    announce(h)
    h.feed.clear()
    with pytest.raises(SystemExit):
        run_at(h, START - 120 * 60)
    with pytest.raises(SystemExit):
        run_at(h, START - 55 * 60)
    assert [p["embeds"][0]["title"] for p in h.sent if "embeds" in p][-1] == "⏰ Maintenance starts in ~55 min"
    assert alerts(h) == ["⚠️ Feed 1 hasn't loaded for over an hour (HTTP 404) — check rss.app"]


def test_stale_feed_warns_once_and_resets(harness, monkeypatch):
    h = harness
    monkeypatch.setattr(r, "STALE_DAYS", 3)
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 3600)
    run_at(h, T0 + 2 * 86400)
    assert alerts(h) == []
    run_at(h, T0 + 4 * 86400)
    run_at(h, T0 + 4 * 86400 + 900)
    assert alerts(h) == ["⚠️ Feed 1: no new tweets for 4 days — check the rss.app feed is still updating"]
    h.feed.append(tweet(2, "hello", T0 + 5 * 86400))       # tweets again: the warning resets
    run_at(h, T0 + 5 * 86400 + 600)
    run_at(h, T0 + 9 * 86400)
    assert len(alerts(h)) == 2


def test_quiet_runs_do_not_change_state(harness):
    h = harness
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    first = h.state.read_bytes()
    for i in range(1, 4):
        run_at(h, T0 + 60 + i * 900)
    assert h.state.read_bytes() == first


def test_failed_alert_is_retried(harness):
    h = harness
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    h.feed.clear()
    with pytest.raises(SystemExit):
        run_at(h, T0 + 900)
    h.fail["on"] = True
    with pytest.raises(SystemExit):
        run_at(h, T0 + 900 + 65 * 60)
    h.fail["on"] = False
    with pytest.raises(SystemExit):
        run_at(h, T0 + 900 + 80 * 60)
    assert len(alerts(h)) == 1


def test_trigger_watchdog(harness, monkeypatch):
    h = harness
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)

    def iso(ts):
        return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    monkeypatch.setenv("LAST_DISPATCH", iso(T0 + 60))
    run_at(h, T0 + 600)                                     # recent dispatch: quiet
    run_at(h, T0 + 3 * 3600)                                # ~3 h without one
    run_at(h, T0 + 4 * 3600)
    assert alerts(h) == ["⚠️ The 15-minute trigger (cron-job.org) hasn't run for ~3 h — check the cron-job.org "
                         "job and its GitHub token. GitHub's own backup timer still runs a few times a day."]
    monkeypatch.setenv("LAST_DISPATCH", iso(T0 + 4 * 3600 + 300))
    run_at(h, T0 + 4 * 3600 + 600)
    assert alerts(h)[-1] == "✅ The 15-minute trigger (cron-job.org) is running again"
    monkeypatch.delenv("LAST_DISPATCH")
    run_at(h, T0 + 9 * 3600)                                # not a scheduled run: nothing checked
    assert len(alerts(h)) == 2


def test_preview_shows_due_alerts_without_saving(harness, monkeypatch, capsys):
    h = harness
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    h.feed.clear()
    with pytest.raises(SystemExit):
        run_at(h, T0 + 900)
    before = h.state.read_bytes()
    monkeypatch.setattr(r, "PREVIEW", True)
    h.clock["now"] = T0 + 900 + 65 * 60
    capsys.readouterr()
    r.main()
    assert "hasn't loaded for over an hour" in capsys.readouterr().out
    assert alerts(h) == [] and h.state.read_bytes() == before


# --- stale feeds: checked against X before warning ----------------------------

def x_result(tid, ts, author="AION2_JP", **extra):
    return {"id": str(tid), "created_timestamp": ts, "author": {"screen_name": author}, **extra}


def test_account_latest_is_the_newest_original_post(monkeypatch):
    results = [
        x_result(1, 100),                                                        # pinned, old
        x_result(2, 900, author="fan", reposted_by={"screen_name": "AION2_JP"}),  # repost: the fan's time
        x_result(3, 800, replying_to={"screen_name": "AION2_JP"}),               # self-reply (rss.app omits)
        x_result(4, 700, quote={"id": "9"}),                                     # quote tweet: an original
        x_result(5, 600),
    ]
    calls = scripted(monkeypatch, "get", Resp(200, json_data={"code": 200, "results": results}))
    assert r.account_latest("AION2_JP") == 700
    args, kwargs = calls[0]
    assert args[0] == "https://api.fxtwitter.com/2/profile/AION2_JP/statuses"
    assert kwargs["timeout"] == (5, 15) and "aion2-relay" in kwargs["headers"]["User-Agent"]


@pytest.mark.parametrize("outcome", [
    requests.ConnectionError("down"), requests.Timeout("slow"),
    Resp(404, json_data={"code": 404, "results": []}), Resp(500), Resp(200),  # Resp(200): not JSON
    Resp(200, json_data={"results": []}),
    Resp(200, json_data={"results": [x_result(1, 5, replying_to={"screen_name": "AION2_JP"}),
                                     x_result(2, 6, reposted_by={"screen_name": "AION2_JP"})]}),
    Resp(200, json_data=[1, 2]), Resp(200, json_data={"results": [None]}),
])
def test_account_latest_none_when_x_cannot_tell(monkeypatch, outcome):
    scripted(monkeypatch, "get", outcome)
    assert r.account_latest("AION2_JP") is None


def test_x_check_due_four_windows_a_day():
    assert r.x_check_due(T0) and r.x_check_due(T0 + 899) and r.x_check_due(T0 + 6 * 3600)
    assert not r.x_check_due(T0 + 900) and not r.x_check_due(T0 + 3600)


def gl_feed(h):
    h.titles[FEED_A] = GL_SOURCE  # feed A is @AION2Official's (tweet() links are its posts)


def test_x_agrees_quiet_account_is_not_stale(harness):
    h = harness
    gl_feed(h)
    h.feed.append(tweet(1, "hi", T0))
    h.x["AION2Official"] = T0 + 23  # X's newest original post is the feed's (rss.app drops the seconds)
    run_at(h, T0 + 60)
    after_first = h.state.read_bytes()
    for k in range(1, 73):          # every 3 h for 9 days
        run_at(h, T0 + k * 3 * 3600)
    assert alerts(h) == [] and h.state.read_bytes() == after_first
    # X is asked only in the 6-hourly windows, and only once the feed's newest post is a day old
    assert h.x_calls == ["AION2Official"] * len([k for k in range(1, 73) if k % 2 == 0 and k * 3 > 24])


def test_missed_post_alerts_once_then_resets(harness):
    h = harness
    gl_feed(h)
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    h.x["AION2Official"] = T0 + 30 * 3600  # Wed 21 Oct 06:00 UTC: a post the feed never got
    run_at(h, T0 + 33 * 3600)              # 09:00 UTC: not a check window
    assert h.x_calls == [] and alerts(h) == []
    run_at(h, T0 + 36 * 3600)              # 12:00 UTC: checks X
    assert alerts(h) == ["⚠️ Feed 1 (@AION2Official): @AION2Official posted at Wed 21 Oct 4:00 pm AEST "
                         "but the rss.app feed hasn't picked it up — check rss.app"]
    run_at(h, T0 + 42 * 3600)
    assert len(alerts(h)) == 1             # once only
    h.feed.append(tweet(2, "hi again", T0 + 30 * 3600))  # rss.app catches up
    run_at(h, T0 + 43 * 3600)
    assert "stale_alerted" not in json.loads(h.state.read_text())["feed_health"][r.feed_key(FEED_A)]


def test_recent_x_post_gets_the_rss_app_lag(harness):
    h = harness
    gl_feed(h)
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    h.x["AION2Official"] = T0 + 35 * 3600  # posted an hour before the next check window
    run_at(h, T0 + 36 * 3600)
    assert alerts(h) == []                 # rss.app gets 2 h to pick it up
    run_at(h, T0 + 42 * 3600)
    assert len(alerts(h)) == 1


def test_x_post_within_lag_of_the_feed_is_the_same_post(harness):
    h = harness
    gl_feed(h)
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    h.x["AION2Official"] = T0 + 90 * 60
    for days in range(1, 10):
        run_at(h, T0 + days * 86400)
    assert alerts(h) == []


def test_x_unavailable_falls_back_to_7_days(harness, monkeypatch):
    h = harness
    gl_feed(h)
    monkeypatch.setattr(r, "STALE_DAYS", 7)
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    run_at(h, T0 + 6 * 86400)              # X can't tell; only 6 days quiet
    run_at(h, T0 + 7 * 86400 + 6 * 3600)
    assert alerts(h) == ["⚠️ Feed 1 (@AION2Official): no new tweets for 7 days — check the rss.app feed is still updating"]
    assert h.x_calls == ["AION2Official", "AION2Official"]


def test_fan_reposts_do_not_make_a_feed_look_current(harness):
    h = harness
    gl_feed(h)
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    h.feed.append(r.feedparser.FeedParserDict(
        link="https://x.com/somefan/status/77", summary="my screenshot",
        published_parsed=datetime.fromtimestamp(T0 + 40 * 3600, UTC).timetuple()))
    h.x["AION2Official"] = T0 + 30 * 3600
    run_at(h, T0 + 42 * 3600)
    assert len(alerts(h)) == 1


def test_preview_asks_x_any_time_and_saves_nothing(harness, monkeypatch, capsys):
    h = harness
    gl_feed(h)
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    h.x["AION2Official"] = T0 + 30 * 3600
    run_at(h, T0 + 33 * 3600)              # a normal run outside the check windows: X not asked
    assert h.x_calls == []
    before = h.state.read_bytes()
    monkeypatch.setattr(r, "PREVIEW", True)
    capsys.readouterr()
    run_at(h, T0 + 33 * 3600)
    assert h.x_calls == ["AION2Official"] and "hasn't picked it up" in capsys.readouterr().out
    assert alerts(h) == [] and h.state.read_bytes() == before


def test_old_newest_field_is_replaced_once(harness):
    h = harness
    gl_feed(h)
    h.feed.append(tweet(1, "hi", T0))
    run_at(h, T0 + 60)
    state, key = json.loads(h.state.read_text()), r.feed_key(FEED_A)
    state["feed_health"][key] = {"handle": "AION2Official", "newest": T0, "stale_alerted": T0 + 3 * 86400}
    h.state.write_text(json.dumps(state))  # state as written by the previous version
    h.x["AION2Official"] = T0 + 23
    run_at(h, T0 + 4 * 86400)              # a check window: X says the account is just quiet
    assert json.loads(h.state.read_text())["feed_health"][key] == {"handle": "AION2Official", "newest_own": T0}
    assert alerts(h) == []


def test_festivities_are_an_event():
    assert r.classify("Join the Halloween festivities in Atreia!")["key"] == "event"
