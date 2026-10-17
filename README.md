# aion2-relay

Relays the important posts of the official AION 2 X accounts (@AION2Official, @AION2_JP) to a
Discord channel, with times converted to Brisbane and New Zealand time. Runs on GitHub Actions.

## What gets posted

| Post | When |
| --- | --- |
| 🚨 Maintenance (red; pings the alert role) | maintenance, downtime, emergency, hotfix notices |
| ✅ / ⏩ / ⏳ / 📅 Maintenance cancelled, complete, ending early, extended, rescheduled | follow-ups to a maintenance; never ping |
| ⏰ Maintenance starts in ~N min | 45–60 min before each announced maintenance (pings the alert role) |
| 🎉 Event (green) | events, campaigns, giveaways, login rewards, free items, livestreams |
| 📜 Game update (blue) | patch notes, updates, new seasons, new servers, server transfers |
| ⚠️ Bot-health notes (plain text) | a feed down for an hour; a feed that missed a post (see below); the 15-minute trigger stopped |

A quiet account isn't a problem, so a feed is only flagged as stale when the account posted
something the feed never picked up. When a feed's newest post by its own account is over a day
old, the bot checks the account's public X profile (via the free fxtwitter API, at 00/06/12/18 UTC)
and warns if there's a newer original post (not a reply or repost) that rss.app hasn't caught up
on within 2 hours. If X can't be reached, it falls back to warning after 7 days without new posts.

Skipped: chatter, fan posts the accounts retweet, Japan-only promotions (prize draws, shop/café
tie-ins), duplicates and reminder tweets for a maintenance already posted, tweets over 48 hours old.

Each post shows the account, the tweet's image, clickable links, a quoted tweet as a block quote,
and converted times (12-hour; regional ranges like "ASIA: 4:30 – 10:00 pm" when the tweet gives
durations). "Your time" lines show in each reader's own time zone.

## Setup

Repo secrets (Settings → Secrets and variables → Actions):

- `FEED_URL` — rss.app feed URL(s) of the X accounts, **one per line**. "Feed 2" in a bot-health
  note means line 2 of this secret. A newly added feed records its existing tweets without posting.
- `DISCORD_WEBHOOK_URL` — the channel's webhook.
- `DISCORD_ROLE_ID` (optional) — role pinged by maintenance announcements and reminders.

Runs are started every 15 minutes (:00, :15, :30, :45) by a cron-job.org job that calls the
`workflow_dispatch` API with a fine-grained token (Actions: read and write, this repo only).
GitHub's own schedule in the workflow is a backup; on those runs the bot warns if the
cron-job.org trigger hasn't run for an hour (e.g. its token expired).

`aion2_seen.json` is the bot's memory (seen tweets, pending reminders, feed health). The first run
creates it; don't create it yourself.

Optional settings (env in `.github/workflows/aion2-relay.yml`): `CATEGORIES` (which of
maintenance,event,update to post), `SOURCE_TZ` (zone assumed when a tweet names none; default
KST), `REMINDER_MINUTES` (reminder lead time; default 60).

## Preview

Actions → **AION 2 maintenance relay** → **Run workflow**, tick **preview** (and optionally set
**count**). The log shows what each feed's newest tweets would post as — category or skip reason,
the Discord message, due reminders and alerts — without posting or saving anything. Secrets are
redacted. Any branch can be previewed; only `main` posts for real.

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

The tests include `test_data/rssapp_corpus.json`: 97 real rss.app tweets with reviewed verdicts
and Brisbane times, so a keyword change that flips a real tweet's verdict fails a test. The Tests
workflow runs them on every push and pull request; Dependabot keeps the pinned actions and
dependencies current.
