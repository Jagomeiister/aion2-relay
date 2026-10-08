# aion2-relay

Checks the official AION 2 X account (via an RSS feed) every 15 minutes and posts
maintenance, event and update tweets to Discord, with times converted to Brisbane
and New Zealand time. Runs on GitHub Actions.

Required repo secrets: `FEED_URL`, `DISCORD_WEBHOOK_URL`. Optional: `DISCORD_ROLE_ID`
(pinged on maintenance posts).

`aion2_seen.json` is created by the first run; don't create it yourself.

Tests: `pip install -r requirements.txt pytest && python -m pytest -q`
