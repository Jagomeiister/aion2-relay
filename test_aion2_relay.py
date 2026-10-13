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
    feeds = {FEED_A: feed}  # url -> entries (oldest first); missing url = feed down
    problems = {}  # url -> problem text reported while that feed is down
    monkeypatch.setattr(r, "FEED_URLS", [FEED_A])

    def fake_fetch(url):
        entries = feeds.get(url)
        if not entries:
            return None, problems.get(url, "HTTP 404")
        # feedparser returns newest first
        return SimpleNamespace(entries=list(reversed(entries)), feed={"title": f"src {url[-5:]}"}), None

    monkeypatch.setattr(r, "fetch_feed", fake_fetch)
    clock = {"now": None}  # None = real time
    monkeypatch.setattr(r, "now_ts", lambda: clock["now"] if clock["now"] is not None else time.time())
    return SimpleNamespace(state=state, sent=sent, feed=feed, fail=fail, feeds=feeds,
                           problems=problems, clock=clock)


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
    assert h.sent[0]["embeds"][0]["footer"]["text"] == f"src {FEED_B[-5:]}"
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
