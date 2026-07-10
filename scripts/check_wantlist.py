#!/usr/bin/env python3
"""
Checks a Discogs wantlist for newly released versions (reissues, repress, etc.)
of masters the user already wants, and emails a summary of anything new.

State (previously seen release versions per master) is persisted in state.json
so only *new* versions trigger a notification.
"""
from __future__ import annotations

import json
import os
import smtplib
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from email.mime.text import MIMEText
from pathlib import Path

import requests

DISCOGS_API = "https://api.discogs.com"
STATE_PATH = Path(__file__).resolve().parent.parent / "state.json"
STATE_READABLE_PATH = Path(__file__).resolve().parent.parent / "state_readable.md"
REQUEST_DELAY_SECONDS = 1.1  # keep well under Discogs' 60 req/min authenticated limit
PER_PAGE = 100


def env(name: str, required: bool = True, default: str | None = None) -> str | None:
    value = os.environ.get(name, default)
    if required and not value:
        print(f"ERROR: missing required environment variable {name}", file=sys.stderr)
        sys.exit(1)
    return value


@dataclass
class WantlistItem:
    release_id: int
    master_id: int
    title: str
    artists: str

    @property
    def url(self) -> str:
        return f"https://www.discogs.com/release/{self.release_id}"


@dataclass
class Version:
    release_id: int
    title: str
    format: str
    country: str
    released: str

    @property
    def url(self) -> str:
        return f"https://www.discogs.com/release/{self.release_id}"


class DiscogsClient:
    def __init__(self, token: str, user_agent: str):
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Discogs token={token}",
                "User-Agent": user_agent,
            }
        )

    def _get(self, url: str, params: dict | None = None) -> dict:
        for attempt in range(5):
            resp = self.session.get(url, params=params)
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", "10"))
                print(f"Rate limited, waiting {wait}s...", file=sys.stderr)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(REQUEST_DELAY_SECONDS)
            return resp.json()
        raise RuntimeError(f"Failed to fetch {url} after retries")

    def get_wantlist(self, username: str) -> list[WantlistItem]:
        items: list[WantlistItem] = []
        page = 1
        while True:
            data = self._get(
                f"{DISCOGS_API}/users/{username}/wants",
                params={"page": page, "per_page": PER_PAGE},
            )
            for want in data.get("wants", []):
                info = want.get("basic_information", {})
                artists = ", ".join(a.get("name", "") for a in info.get("artists", []))
                items.append(
                    WantlistItem(
                        release_id=info.get("id"),
                        master_id=info.get("master_id") or 0,
                        title=info.get("title", "Unknown title"),
                        artists=artists,
                    )
                )
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            page += 1
        return items

    def get_master_versions(self, master_id: int) -> list[Version]:
        versions: list[Version] = []
        page = 1
        while True:
            data = self._get(
                f"{DISCOGS_API}/masters/{master_id}/versions",
                params={"page": page, "per_page": PER_PAGE},
            )
            for v in data.get("versions", []):
                versions.append(
                    Version(
                        release_id=v.get("id"),
                        title=v.get("title", ""),
                        format=v.get("format", ""),
                        country=v.get("country", ""),
                        released=v.get("released", ""),
                    )
                )
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            page += 1
        return versions


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"known_versions": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def write_readable_state(
    master_entries: list[tuple[WantlistItem, list[Version]]],
    standalone_items: list[WantlistItem],
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Discogs wantlist notifier - current record",
        "",
        f"Generated {now}. Regenerated on every run; edits here are not preserved.",
        "",
        f"## Tracked masters ({len(master_entries)})",
        "",
    ]

    for item, versions in sorted(master_entries, key=lambda pair: (pair[0].artists, pair[0].title)):
        lines.append(f"### {item.artists} - {item.title}")
        lines.append(f"Master: https://www.discogs.com/master/{item.master_id}")
        lines.append("")
        for v in sorted(versions, key=lambda v: v.released or ""):
            details = " / ".join(p for p in [v.format, v.country, v.released] if p)
            lines.append(f"- {v.title} ({details}) -> {v.url}")
        lines.append("")

    lines.append(f"## Items with no master release yet ({len(standalone_items)})")
    lines.append("")
    if standalone_items:
        for item in sorted(standalone_items, key=lambda i: (i.artists, i.title)):
            lines.append(f"- {item.artists} - {item.title} -> {item.url}")
    else:
        lines.append("(none)")
    lines.append("")

    STATE_READABLE_PATH.write_text("\n".join(lines), encoding="utf-8")


def send_email(subject: str, body: str) -> None:
    smtp_host = env("SMTP_HOST")
    smtp_port = int(env("SMTP_PORT", default="587"))
    smtp_user = env("SMTP_USER")
    smtp_pass = env("SMTP_PASS")
    email_from = env("EMAIL_FROM", required=False, default=smtp_user)
    email_to = env("EMAIL_TO", required=False, default=email_from)

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(email_from, [email_to], msg.as_string())
    print("Notification email sent.")


def main() -> None:
    if os.environ.get("TEST_EMAIL") == "1":
        send_email(
            subject="Discogs wantlist notifier: test email",
            body="This is a test email to confirm SMTP delivery is working.",
        )
        return

    token = env("DISCOGS_TOKEN")
    username = env("DISCOGS_USERNAME")
    user_agent = env(
        "DISCOGS_USER_AGENT", required=False, default="DiscogsWantlistNotifier/1.0"
    )

    client = DiscogsClient(token, user_agent)
    state = load_state()
    known_versions: dict = state.setdefault("known_versions", {})
    previously_standalone = set(state.setdefault("standalone_release_ids", []))

    print(f"Fetching wantlist for {username}...")
    wantlist = client.get_wantlist(username)
    print(f"Found {len(wantlist)} wantlist items.")

    masters_to_check = {item.master_id: item for item in wantlist if item.master_id}
    standalone_items = [item for item in wantlist if not item.master_id]
    print(
        f"{len(masters_to_check)} item(s) belong to a master release, "
        f"{len(standalone_items)} item(s) currently have no master release."
    )

    new_findings: list[tuple[WantlistItem, list[Version]]] = []
    all_master_entries: list[tuple[WantlistItem, list[Version]]] = []

    for i, (master_id, item) in enumerate(masters_to_check.items(), start=1):
        print(f"[{i}/{len(masters_to_check)}] Checking master {master_id} ({item.artists} - {item.title})...")
        versions = client.get_master_versions(master_id)
        all_master_entries.append((item, versions))
        current_ids = {v.release_id for v in versions}

        key = str(master_id)
        previously_known = set(known_versions.get(key, []))

        # If this release had no master release last run, Discogs has just grouped
        # it with other version(s) for the first time -- that's news, so notify
        # with the full version list instead of silently baselining it.
        newly_gained_master = item.release_id in previously_standalone

        if key not in known_versions:
            if newly_gained_master:
                new_findings.append((item, versions))
            known_versions[key] = sorted(current_ids)
            continue

        new_ids = current_ids - previously_known
        if new_ids:
            new_versions = [v for v in versions if v.release_id in new_ids]
            new_findings.append((item, new_versions))
            known_versions[key] = sorted(current_ids | previously_known)

    state["standalone_release_ids"] = sorted({item.release_id for item in standalone_items})
    save_state(state)
    write_readable_state(all_master_entries, standalone_items)

    if not new_findings:
        print("No new versions found.")
        return

    lines = ["New versions/reissues found for items on your Discogs wantlist:", ""]
    for item, versions in new_findings:
        lines.append(f"{item.artists} - {item.title}")
        for v in versions:
            details = " / ".join(p for p in [v.format, v.country, v.released] if p)
            lines.append(f"  - {v.title} ({details}) -> {v.url}")
        lines.append("")

    body = "\n".join(lines)
    print(body)

    send_email(
        subject=f"Discogs wantlist: {sum(len(v) for _, v in new_findings)} new version(s) found",
        body=body,
    )


if __name__ == "__main__":
    main()
