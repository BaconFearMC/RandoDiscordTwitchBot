# SMO Twitch Rando Alerts

A cloud-hosted Discord bot that scans Twitch's **Super Mario Odyssey** live category
and posts an alert when a stream title contains `Rando`, `Random`, or `Randomizers`
(case-insensitive substring matching; for example, `Randomizer` also matches).

## Files

- `bot.py` — Discord bot and Twitch Helix API scanner
- `requirements.txt` — Python dependencies
- `railway.toml` — Railway start/restart configuration

## Required environment variables

Set these in Railway's service Variables tab. Do not commit credentials to GitHub.

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Bot token from Discord Developer Portal |
| `DISCORD_CHANNEL_ID` | Numeric ID of the Discord alert channel |
| `TWITCH_CLIENT_ID` | Twitch application Client ID |
| `TWITCH_CLIENT_SECRET` | Twitch application Client Secret |
| `SCAN_INTERVAL` | Scan interval in seconds; defaults to 60 |
| `STATE_DIR` | Persistent state directory; set to `/data` on Railway |

## Railway persistent storage

Add a Railway Volume to the bot service and mount it at `/data`.
The SQLite database is stored at `/data/stream_state.sqlite3` so announcement
history survives redeployments and restarts.

## Discord permissions

Invite the bot with the `bot` scope and at least:
- View Channel
- Send Messages
- Embed Links

No Message Content privileged intent is required.

## Twitch application

Create an application in the Twitch Developer Console and use its Client ID and
Client Secret. The bot uses the Client Credentials flow and does not need a
Twitch user login.

## Behavior

- Only live streams returned under Twitch's Super Mario Odyssey game category are scanned.
- A stream is announced when its title first matches, or changes from non-matching
  to matching while it remains live.
- A stream that goes offline and later returns can be announced again.
- Existing matching streams are not announced again after a normal restart,
  provided the Railway volume remains attached.

## Deploy

Create a GitHub repository, upload these project files, connect the repository
to a Railway service, set the environment variables, attach a volume mounted at
`/data`, and deploy.
