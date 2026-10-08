"""Regression tests for aion2_maint_relay.py. Run: python -m pytest -q"""
import json
import os
from datetime import date, datetime
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


# --- time parsing ---------------------------------------------------------

def test_basic_kst_range_and_targets():
    tz, ts = r.parse_times("Maintenance 10/9 10:00 ~ 14:00 (KST)", PUB)
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
    _, ts = r.parse_times(text, PUB)
    assert len(ts) == 1


def test_utc_offset_is_not_plain_utc():
    tz, ts = r.parse_times("Maintenance 10/9 10:00 (UTC+9)", PUB)
    assert tz == "UTC+9"
    assert hm(ts[0]) == "2026-10-09 10:00"
    assert len(ts) == 1  # the "+09" must not be read as a time


def test_korean_and_japanese_suffixes():
    _, ts = r.parse_times("10월 9일 10:00부터 14:00까지 점검", PUB)
    assert [hm(t) for t in ts] == ["2026-10-09 10:00", "2026-10-09 14:00"]
    _, ts = r.parse_times("10月9日 10:00から14:00までメンテナンス", PUB)
    assert [hm(t) for t in ts] == ["2026-10-09 10:00", "2026-10-09 14:00"]


def test_year_rollover():
    _, ts = r.parse_times("Maintenance 1/5 10:00 KST", datetime(2026, 12, 28, tzinfo=UTC))
    assert hm(ts[0]) == "2027-01-05 10:00"


@pytest.mark.parametrize("text", [
    "Maintenance on Oct 15, 10:00 KST",
    "Maintenance on October 15th 10:00 KST",
    "Maintenance on 15 Oct 10:00 KST",
])
def test_english_month_names(text):
    _, ts = r.parse_times(text, PUB)
    assert hm(ts[0]) == "2026-10-15 10:00"


def test_am_pm():
    _, ts = r.parse_times("Maintenance Oct 9 10:00 AM - 2:00 PM PDT", PUB)
    assert [hm(t, "America/Los_Angeles") for t in ts] == ["2026-10-09 10:00", "2026-10-09 14:00"]


def test_overnight_rolls_to_next_day():
    _, ts = r.parse_times("Maintenance 10/9 22:00 ~ 02:00 KST", PUB)
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
    # feedparser returns newest first
    monkeypatch.setattr(r.feedparser, "parse", lambda url: SimpleNamespace(
        entries=list(reversed(feed)), bozo=0, get=lambda k, d=None: 200))
    return SimpleNamespace(state=state, sent=sent, feed=feed, fail=fail)


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
