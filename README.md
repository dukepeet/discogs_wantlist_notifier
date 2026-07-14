# Discogs Wantlist Notifier

Watches your [Discogs](https://www.discogs.com) wantlist and emails you when a
**new version** (reissue, repress, different pressing, etc.) of a release you
want shows up on Discogs — useful for out-of-print records where you're
waiting for a reissue.

Runs automatically via a scheduled GitHub Actions workflow, no server needed.
This repo (code) is meant to be public; your actual wantlist data lives in a
separate **private** repo — see [Keeping your data private](#keeping-your-data-private).

## How it works

1. Once a week (configurable), a GitHub Actions job fetches your wantlist
   from the Discogs API.
2. For each wanted item that belongs to a "master release" (i.e. it has other
   pressings), it fetches all currently known versions of that master and
   keeps only the ones matching a format you actually have wantlisted for
   that title (e.g. if you only wantlisted the Vinyl pressing, new CD or
   digital versions of the same master are ignored entirely). If you've
   wantlisted the same title in more than one format, all of those formats
   are tracked.
3. It compares that list against `state.json`, which records what was seen
   last time. Anything new triggers an email.
4. The first run just records a baseline for each master (no email) so you
   don't get spammed with pressings that already existed.
5. `state.json` is committed back to your private data repo by the workflow
   after each run.

Wantlist items with **no master release yet** (a standalone pressing Discogs
hasn't grouped with any other version) are tracked too: each run remembers
which items are currently masterless. If Discogs later creates a master
release for one of them — meaning a reissue/repress finally got added and
grouped with it — that's reported as a new finding with the full list of
versions, since it's the first time any alternate version has existed for
that release.

`state_readable.md` is regenerated on every run and lists, in plain text,
every tracked master with its known versions, every wantlist item that has
no master release yet, every version ever discovered by the notifier with
its discovery date and whether it's currently on your wantlist, and (see
below) every wantlist item currently listed for sale under your price limit.
`state.json` remains the machine-readable file the script actually diffs
against; the readable file is just a mirror for humans and any local edits
to it are overwritten on the next run. Both files live only in your private
data repo, never in this (public) code repo.

### Marketplace price check

Every run also checks each wantlist item's marketplace availability via
Discogs's `marketplace/stats` endpoint, which reports the lowest currently
listed price for that specific release — already converted to EUR by
Discogs itself, regardless of what currency the seller listed it in. If it's
at or under a price limit (default **€80**, set `MARKETPLACE_PRICE_LIMIT_EUR`
to change it), it's flagged.

Three things worth knowing about this check:

- **The price excludes shipping and fees**, and that's deliberate rather than
  estimated. Discogs's public API doesn't expose per-listing shipping cost or
  seller location for buyers (only the aggregate lowest price) — and,
  somewhat surprisingly, there's also no API endpoint for a buyer's own
  purchase history (`marketplace/orders` only works "authenticated as the
  seller"; a long-standing, acknowledged gap in Discogs's API), so there's no
  way to derive a real shipping figure from any actual data. The marketplace
  listing pages themselves are also behind a Cloudflare bot challenge, so
  scraping them isn't something this project does either. The €80 default
  is set with that missing shipping headroom already in mind, rather than
  trying to add a guessed number on top.
- **The one thing that *is* estimated is import VAT**, since it's the one
  origin-dependent cost that can be large and predictable: EU sellers charge
  none, non-EU sellers trigger import VAT (default **27%**, Hungary's rate —
  set `NON_EU_VAT_PCT` to change it) on the full price. A flagged listing
  shows "if non-EU seller, incl. VAT: ~€X" alongside the raw price. There's no
  way to know which case actually applies to a *new* listing automatically
  (the marketplace/stats endpoint gives no seller info), so you still need to
  open the listing to check the seller's real location.
- **You're only emailed when a release newly drops under the limit**, not
  every week for a listing you've already seen and decided not to buy. The
  full current snapshot (not just new ones) is always in `state_readable.md`
  if you want to browse everything currently under the limit.

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

### 4. Set up your private data repo

This code repo is meant to be public (or at least shareable) — it contains no
personal information. Your actual wantlist contents (`state.json` /
`state_readable.md`) are written to a **separate private repo** instead, so
making this repo public never exposes what's on your wantlist.

1. Create a new **private** GitHub repo, e.g. `discogs-wantlist-notifier-data`.
   Initialize it with a README (or any first commit) so it has a default
   branch — an empty repo with zero commits can't be checked out.
2. Create a fine-grained personal access token scoped to just that repo:
   [github.com/settings/personal-access-tokens/new](https://github.com/settings/personal-access-tokens/new)
   - **Repository access**: "Only select repositories" → pick your new data
     repo.
   - **Permissions**: Repository → **Contents** → **Read and write**.
   - Copy the generated token.

### 5. Add repository secrets

In this repo: **Settings → Secrets and variables → Actions → New repository
secret**. Add each of these:

| Secret name         | Value                                      |
|----------------------|---------------------------------------------|
| `DISCOGS_TOKEN`      | Your Discogs personal access token          |
| `DISCOGS_USERNAME`   | Your Discogs username                       |
| `STATE_REPO`          | Your private data repo, as `owner/repo` (e.g. `dukepeet/discogs-wantlist-notifier-data`) |
| `STATE_REPO_TOKEN`    | The fine-grained PAT from step 4            |
| `SMTP_HOST`           | e.g. `smtp.gmail.com`                       |
| `SMTP_PORT`           | e.g. `587`                                  |
| `SMTP_USER`           | SMTP login (e.g. your Gmail address)        |
| `SMTP_PASS`           | SMTP password / app password                |
| `EMAIL_FROM`          | *(optional)* defaults to `SMTP_USER`        |
| `EMAIL_TO`            | *(optional)* where to send notifications, defaults to `EMAIL_FROM` (i.e. `SMTP_USER` if that's also unset) |
| `MARKETPLACE_PRICE_LIMIT_EUR` | *(optional)* price limit in EUR for the marketplace check, defaults to `80` |
| `NON_EU_VAT_PCT` | *(optional)* import VAT % applied to price for non-EU sellers, defaults to `27` |

### 6. Enable and test the workflow

- Go to the **Actions** tab in this repo, select "Check Discogs wantlist for
  new versions", and click **Run workflow** to trigger it manually the first
  time.
- Check the run logs to confirm it fetched your wantlist. Then check your
  private data repo — it should now have a populated `state.json` and
  `state_readable.md` committed to it.
- After that, it runs automatically once a week (default: Monday 09:00 UTC —
  edit the `cron` line in
  [`.github/workflows/check-wantlist.yml`](.github/workflows/check-wantlist.yml)
  to change the schedule).

## Keeping your data private

Once the steps above are done, this repo's git history and files contain no
personal information — everything wantlist-related lives in your private
data repo instead. At that point it's safe to make this repo public via
**Settings → Danger Zone → Change visibility** (your data repo should stay
private).

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

`state.json` and `state_readable.md` are written to the current directory by
default. Set `STATE_DIR=/some/other/path` to write them elsewhere (this is
how the GitHub Actions workflow points them at the checked-out private data
repo).

## Notes on rate limits

The Discogs API allows 60 requests/minute for authenticated requests. The
script adds a small delay between calls and backs off automatically if it
gets rate-limited (HTTP 429), so large wantlists just take a bit longer
rather than failing.
