# BookOrbit Request Bot

A Telegram bot that files book requests in your [BookOrbit](https://github.com/bookorbit/bookorbit) instance and lets BookOrbit's request automation find, download and import them.

## Commands

| Command | What it does |
| --- | --- |
| `/request <title>` | Searches BookOrbit's metadata providers for the title, files a request for the best match, and reports each status change (searching, downloading, available, failed…). |
| `/email <title>` | Same as `/request`, then waits for the book to become available and emails it to your first recipient. |
| `/help` | Shows help. |

A plain message behaves like `/request`. Put one title per line to request several books in a single message (works with `/request` and `/email` too).

## Setup

### 1. BookOrbit

Create (or pick) a BookOrbit user for the bot and give it these permissions:

- **Request books** (`book_request_access`)
- **Auto-approve own requests** (`book_request_auto_approve`) so requests skip the approval queue
- **Send by email** (`email_send`), only needed for `/email`

Then in BookOrbit:

- **Settings → Server → Requests → Automation**: enable automatic downloads (auto-grab). Without it an approved request just waits for a person to pick a release.
- **Settings → Server → Requests**: set a default destination library for e-books. Auto-approved requests are refused without one.
- **Settings → Email** (for `/email`): configure an email provider and at least one recipient. The bot emails the default recipient, or the first listed one if no default is set.

### 2. Telegram

Create a bot with [@BotFather](https://t.me/BotFather) and copy the token.

### 3. Run

A prebuilt image is published to GHCR on every push to `main`/`master`. Fill in `docker-compose.yml` and run `docker compose up -d`:

```yaml
services:
  bookorbit-request-bot:
    image: ghcr.io/vjfalk/bookorbit-request-bot:latest
    restart: unless-stopped
    environment:
      TELEGRAM_BOT_TOKEN: ""
      BOOKORBIT_URL: ""          # e.g. http://192.168.0.232:3000
      BOOKORBIT_USERNAME: ""
      BOOKORBIT_PASSWORD: ""
      ALLOWED_USER_IDS: ""       # optional, comma-separated Telegram user IDs
      REQUEST_WAIT_MINUTES: "30" # optional, how long to follow a request
      LOG_LEVEL: "INFO"          # optional
```

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | Token from BotFather |
| `BOOKORBIT_URL` | Yes | Base URL of your BookOrbit instance |
| `BOOKORBIT_USERNAME` | Yes | BookOrbit user the bot signs in as |
| `BOOKORBIT_PASSWORD` | Yes | Its password |
| `ALLOWED_USER_IDS` | No | Restrict the bot to these Telegram user IDs; empty allows everyone |
| `REQUEST_WAIT_MINUTES` | No | How long the bot follows a request and reports status changes (default 30) |
| `LOG_LEVEL` | No | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |

Without Docker:

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... BOOKORBIT_URL=... BOOKORBIT_USERNAME=... BOOKORBIT_PASSWORD=... python -m bot.main
```

## How it works

1. `GET /api/v1/metadata-fetch/stream?title=…&mediaKind=ebook` streams metadata candidates; the closest title match wins, ISBN-bearing candidates break ties.
2. `POST /api/v1/book-requests` files the request with that candidate's title, authors, ISBNs and provider IDs, so BookOrbit's release scoring has something to match against.
3. For `/email`, the bot polls `GET /api/v1/book-requests/:id` until the status settles, then `POST /api/v1/email/send` with the matched book and recipient.

Requests are always e-books. If the book is already in the library the request comes back `available` immediately and `/email` sends it right away. If someone already requested it you are added to their request instead.

## Test

```bash
python test_bot.py
```
