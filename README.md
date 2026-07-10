# Discogs Wantlist Notifier

Watches your [Discogs](https://www.discogs.com) wantlist and emails you when a
**new version** (reissue, repress, different pressing, etc.) of a release you
want shows up on Discogs — useful for out-of-print records where you're
waiting for a reissue.

Runs automatically via a scheduled GitHub Actions workflow, no server needed.

## How it works

1. Once a day (configurable), a GitHub Actions job fetches your wantlist from
   the Discogs API.
2. For each wanted item that belongs to a "master release" (i.e. it has other
   pressings), it fetches all currently known versions of that master.
3. It compares that list against `state.json`, which records what was seen
   last time. Anything new triggers an email.
4. The first run just records a baseline for each master (no email) so you
   don't get spammed with pressings that already existed.
5. `state.json` is committed back to the repo by the workflow after each run.

Wantlist items with **no master release yet** (a standalone pressing Discogs
hasn't grouped with any other version) are tracked too: each run remembers
which items are currently masterless. If Discogs later creates a master
release for one of them — meaning a reissue/repress finally got added and
grouped with it — that's reported as a new finding with the full list of
versions, since it's the first time any alternate version has existed for
that release.

## Setup

### 1. Get a Discogs personal access token

1. Log in to Discogs and go to
   [Settings → Developers](https://www.discogs.com/settings/developers).
2. Click **Generate new token**. Copy the token — you'll need it below.
3. Note your Discogs **username** too (shown in the top-right of the site).

### 2. Set up an SMTP sender for email

The easiest option is a Gmail account with an **App Password**:

1. Enable 2-Step Verification on the Google account you want to send from:
   https://myaccount.google.com/security
2. Create an app password: https://myaccount.google.com/apppasswords
   (choose "Mail" / "Other", name it e.g. "discogs-notifier"). Copy the
   16-character password.
3. Your SMTP settings will be:
   - `SMTP_HOST` = `smtp.gmail.com`
   - `SMTP_PORT` = `587`
   - `SMTP_USER` = your Gmail address
   - `SMTP_PASS` = the app password from step 2

Any other SMTP provider (Fastmail, Outlook, SendGrid SMTP relay, etc.) works
the same way — just use its host/port/credentials instead.

### 3. Push this repo to GitHub

```bash
git remote add origin <your-repo-url>
git push -u origin main
```

### 4. Add repository secrets

In your GitHub repo: **Settings → Secrets and variables → Actions → New
repository secret**. Add each of these:

| Secret name         | Value                                      |
|----------------------|---------------------------------------------|
| `DISCOGS_TOKEN`      | Your Discogs personal access token          |
| `DISCOGS_USERNAME`   | Your Discogs username                       |
| `SMTP_HOST`           | e.g. `smtp.gmail.com`                       |
| `SMTP_PORT`           | e.g. `587`                                  |
| `SMTP_USER`           | SMTP login (e.g. your Gmail address)        |
| `SMTP_PASS`           | SMTP password / app password                |
| `EMAIL_TO`            | Where to send notifications                 |
| `EMAIL_FROM`          | *(optional)* defaults to `SMTP_USER`        |

### 5. Enable and test the workflow

- Go to the **Actions** tab in your repo, select "Check Discogs wantlist for
  new versions", and click **Run workflow** to trigger it manually the first
  time.
- Check the run logs to confirm it fetched your wantlist and committed a
  populated `state.json`.
- After that, it runs automatically once a day (default: 09:00 UTC — edit the
  `cron` line in
  [`.github/workflows/check-wantlist.yml`](.github/workflows/check-wantlist.yml)
  to change the schedule).

## Running locally

```bash
pip install -r requirements.txt

export DISCOGS_TOKEN=...
export DISCOGS_USERNAME=...
export SMTP_HOST=...
export SMTP_PORT=587
export SMTP_USER=...
export SMTP_PASS=...
export EMAIL_TO=...

python scripts/check_wantlist.py
```

## Notes on rate limits

The Discogs API allows 60 requests/minute for authenticated requests. The
script adds a small delay between calls and backs off automatically if it
gets rate-limited (HTTP 429), so large wantlists just take a bit longer
rather than failing.
