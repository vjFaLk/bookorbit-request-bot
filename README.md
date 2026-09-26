# BookOrbit Request Bot

A Telegram bot that files book requests in your [BookOrbit](https://github.com/bookorbit/bookorbit) instance and lets BookOrbit's request automation find, download and import them.

## Commands

| Command | What it does |
| --- | --- |
| `/request <title>` | Admins only. Searches BookOrbit's metadata providers for the title, files a request for the best match, and reports each status change (searching, downloading, available, failed…). |
| `/email <title>` | Same as `/request`, then waits for the book to become available and emails it to the address you bound with `/bind`. |
| `/download <title>` | Same as `/request`, then sends the e-book file to you in Telegram (files up to 50 MB, Telegram's bot limit). |
| `/bind <email>` | Binds your Kindle/email address to your Telegram account for `/email`. `/bind` alone shows the current binding. |
| `/help` | Shows help. |

In a DM a plain message behaves like `/request` (so admins only). Put one title per line to request several books in a single message (works with every request command).

Each request is a single message that updates in place as the status changes (searching, downloading, available…). A request that lands in `needs_review` tells you to reach out to Wangla.

### Groups

Add the bot to a group and list the group's ID in `ALLOWED_GROUP_IDS`. Group members can use `/download`, `/bind` and `/email`; `/request` and plain-text requests are reserved for the admins in `ALLOWED_USER_IDS`. Commands typed in the group get a 👀 reaction and the answer arrives in the user's DM, so the group stays quiet. Members of an allowed group can also DM the bot directly. Telegram only lets a bot message people who have started it, so each person has to open the bot and press Start once; the bot says so in the group if it can't reach them.

### Email binding

Recipients live in BookOrbit under the bot's user. `/bind you@kindle.com` creates (or updates) a recipient named `tg:<telegram id> <first name>`; `/email` looks that recipient up by the `tg:<id>` prefix, so no local storage is needed. Addresses ending in `@kindle.com` are created as Kindle devices. Emails are unique per account, so one address can only be bound to one Telegram user.

## Setup

### 1. BookOrbit

Create (or pick) a BookOrbit user for the bot and give it these permissions:

- **Request books** (`book_request_access`)
- **Auto-approve own requests** (`book_request_auto_approve`) so requests skip the approval queue
- **Send by email** (`email_send`), only needed for `/email` and `/bind`
- **Download from library** (`library_download`) plus access to the destination library, only needed for `/download`

Then in BookOrbit:

- **Settings → Server → Requests → Automation**: enable automatic downloads (auto-grab). Without it an approved request just waits for a person to pick a release.
- **Settings → Server → Requests**: set a default destination library for e-books. Auto-approved requests are refused without one.
- **Settings → Email** (for `/email`): configure an email provider. Recipients are created by `/bind`.

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
      ALLOWED_GROUP_IDS: ""      # optional, comma-separated Telegram group IDs
      REQUEST_WAIT_MINUTES: "30" # optional, how long to follow a request
      LOG_LEVEL: "INFO"          # optional
```

| Variable | Required | Description |
| --- | --- | --- |
| `TELEGRAM_BOT_TOKEN` | Yes | Token from BotFather |
| `BOOKORBIT_URL` | Yes | Base URL of your BookOrbit instance |
| `BOOKORBIT_USERNAME` | Yes | BookOrbit user the bot signs in as |
| `BOOKORBIT_PASSWORD` | Yes | Its password |
| `ALLOWED_USER_IDS` | No | Admins: these Telegram user IDs may use every command anywhere |
| `ALLOWED_GROUP_IDS` | No | Members of these groups may use `/download`, `/bind` and `/email`, in the group or by DM (membership checked live via `getChatMember`). If both allow-lists are empty, everyone is an admin |
| `REQUEST_WAIT_MINUTES` | No | How long the bot follows a request and reports status changes (default 30) |
| `LOG_LEVEL` | No | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |

Without Docker:

```bash
pip install -r requirements.txt
TELEGRAM_BOT_TOKEN=... BOOKORBIT_URL=... BOOKORBIT_USERNAME=... BOOKORBIT_PASSWORD=... python -m bot.main
```

## How it works

1. `GET /api/v1/metadata-fetch/stream?title=…&mediaKind=ebook` streams metadata candidates; the closest title match wins, ISBN-bearing candidates break ties.
2. `POST /api/v1/book-requests/availability` checks whether that book is already in the library (ISBN13, then exact title + author). If that finds nothing, `GET /api/v1/books/search` runs on the candidate's title with author names and punctuation stripped, and a near-identical title with an agreeing author counts as owned. If the book is owned, no request is filed: `/request` says so, `/email` and `/download` deliver the existing book straight away. BookOrbit's own check on request creation only runs when the user has "Let this user see books they requested" ticked, which is why the bot asks first.
3. `POST /api/v1/book-requests` files the request with that candidate's title, authors, ISBNs and provider IDs, so BookOrbit's release scoring has something to match against.
4. The bot polls `GET /api/v1/book-requests/:id` until the status settles, editing its message on each change.
5. For `/email` it then calls `POST /api/v1/email/send` with the matched book and the user's bound recipient. For `/download` it reads `GET /api/v1/books/:id`, picks the best e-book file (epub first) and streams `GET /api/v1/books/files/:fileId/download` to Telegram.

Requests are always e-books. If someone already requested the book you are added to their request instead.

## Test

```bash
python test_bot.py
```
