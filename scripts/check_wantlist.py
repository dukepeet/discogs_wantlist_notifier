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
STATE_DIR = Path(os.environ.get("STATE_DIR", Path(__file__).resolve().parent.parent))
STATE_PATH = STATE_DIR / "state.json"
STATE_READABLE_PATH = STATE_DIR / "state_readable.md"
REQUEST_DELAY_SECONDS = 1.1  # keep well under Discogs' 60 req/min authenticated limit
PER_PAGE = 100


def env(name: str, required: bool = True, default: str | None = None) -> str | None:
    # An unset GitHub Actions secret still sets the env var, just to "" --
    # treat that the same as truly unset so defaults actually apply.
    value = os.environ.get(name) or default
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
    formats: frozenset[str]

    @property
    def url(self) -> str:
        return f"https://www.discogs.com/release/{self.release_id}"


@dataclass
class Version:
    release_id: int
    title: str
    format: str
    major_formats: frozenset[str]
    country: str
    released: str

    @property
    def url(self) -> str:
        return f"https://www.discogs.com/release/{self.release_id}"

    def matches_formats(self, wanted_formats: frozenset[str]) -> bool:
        if not wanted_formats:
            return True
        return bool({f.lower() for f in self.major_formats} & wanted_formats)


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
                formats = frozenset(
                    f.get("name", "") for f in info.get("formats", []) if f.get("name")
                )
                items.append(
                    WantlistItem(
                        release_id=info.get("id"),
                        master_id=info.get("master_id") or 0,
                        title=info.get("title", "Unknown title"),
                        artists=artists,
                        formats=formats,
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
                        major_formats=frozenset(v.get("major_formats", [])),
                        country=v.get("country", ""),
                        released=v.get("released", ""),
                    )
                )
            pagination = data.get("pagination", {})
            if page >= pagination.get("pages", 1):
                break
            page += 1
        return versions

    def get_marketplace_stats(self, release_id: int, currency: str = "EUR") -> dict:
        return self._get(
            f"{DISCOGS_API}/marketplace/stats/{release_id}",
            params={"curr_abbr": currency},
        )


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"known_versions": {}}


def save_state(state: dict) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def write_readable_state(
    username: str,
    master_entries: list[tuple[WantlistItem, list[Version]]],
    standalone_items: list[WantlistItem],
    discovered_versions: dict,
    current_release_ids: set[int],
    marketplace_flagged: dict[int, dict],
    price_limit: float,
    shipping_config: dict,
) -> None:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "# Discogs wantlist notifier - current record",
        "",
        f"Wantlist: https://www.discogs.com/wantlist?user={username}",
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
            details = " / ".join(
                p for p in [", ".join(sorted(v.major_formats)), v.format, v.country, v.released] if p
            )
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

    not_wantlisted = sum(1 for rid in discovered_versions if int(rid) not in current_release_ids)
    lines.append(
        f"## All versions ever discovered ({len(discovered_versions)}, "
        f"{not_wantlisted} not yet on your wantlist)"
    )
    lines.append("")
    if discovered_versions:
        records = sorted(
            discovered_versions.items(),
            key=lambda kv: kv[1].get("discovered_date", ""),
            reverse=True,
        )
        for release_id, rec in records:
            wantlisted = "Yes" if int(release_id) in current_release_ids else "No"
            details = " / ".join(
                p for p in [rec.get("format"), rec.get("country"), rec.get("released")] if p
            )
            url = f"https://www.discogs.com/release/{release_id}"
            lines.append(
                f"- [{rec.get('discovered_date', '?')}] {rec.get('artists')} - {rec.get('title')} "
                f"({details}) -> {url} | Wantlisted: {wantlisted}"
            )
    else:
        lines.append("(none found yet)")
    lines.append("")

    lines.append(
        f"## Marketplace listings currently under EUR {price_limit:.2f} ({len(marketplace_flagged)})"
    )
    lines.append(
        "Prices exclude shipping/fees and may be from sellers outside the EU "
        "(possible VAT/import charges) -- check the listing before buying."
    )
    lines.append(format_shipping_estimate_caveat(shipping_config))
    lines.append("")
    if marketplace_flagged:
        for release_id, info in sorted(marketplace_flagged.items(), key=lambda kv: kv[1]["price"]):
            item = info["item"]
            url = f"https://www.discogs.com/sell/release/{release_id}?ev=rb&currency=EUR"
            est = format_estimate_range(info)
            lines.append(
                f"- {item.artists} - {item.title}: EUR {info['price']:.2f}{est} "
                f"({info['num_for_sale']} for sale) -> {url}"
            )
    else:
        lines.append("(none right now)")
    lines.append("")

    STATE_READABLE_PATH.write_text("\n".join(lines), encoding="utf-8")


def compute_shipping_estimates(price: float, shipping_config: dict) -> tuple[float, float]:
    """Discogs exposes no per-listing shipping cost or seller location for
    buyers (and no buyer purchase-history endpoint to derive one from), so
    this applies flat, manually-configured shipping assumptions per seller
    region instead of anything computed from real data."""
    eu_total = price + shipping_config["eu_shipping_eur"]
    non_eu_total = (price + shipping_config["non_eu_shipping_eur"]) * (
        1 + shipping_config["non_eu_vat_pct"] / 100
    )
    return eu_total, non_eu_total


def format_estimate_range(info: dict) -> str:
    parts = []
    if info.get("estimated_total_eu") is not None:
        parts.append(f"EU seller ~EUR {info['estimated_total_eu']:.2f}")
    if info.get("estimated_total_non_eu") is not None:
        parts.append(f"non-EU seller ~EUR {info['estimated_total_non_eu']:.2f}")
    return f" (est. total: {' / '.join(parts)})" if parts else ""


def format_shipping_estimate_caveat(shipping_config: dict) -> str:
    return (
        f"'Est. total' assumes flat shipping of EUR {shipping_config['eu_shipping_eur']:.2f} "
        f"for EU sellers, or EUR {shipping_config['non_eu_shipping_eur']:.2f} shipping plus "
        f"{shipping_config['non_eu_vat_pct']:.0f}% import VAT (on item + shipping) for non-EU "
        "sellers -- manually configured assumptions, not per-listing quotes (Discogs's API "
        "exposes neither real shipping cost nor a buyer's purchase history to derive this "
        "from). You still need to check the actual listing's seller location."
    )


def record_discoveries(state: dict, item: WantlistItem, versions: list[Version], today: str) -> None:
    discovered: dict = state.setdefault("discovered_versions", {})
    for v in versions:
        discovered.setdefault(
            str(v.release_id),
            {
                "master_id": item.master_id,
                "artists": item.artists,
                "title": v.title or item.title,
                "format": ", ".join(sorted(v.major_formats)) or v.format,
                "country": v.country,
                "released": v.released,
                "discovered_date": today,
            },
        )


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

    masters_to_check: dict[int, WantlistItem] = {}
    masters_wanted_formats: dict[int, set[str]] = {}
    for item in wantlist:
        if not item.master_id:
            continue
        masters_to_check.setdefault(item.master_id, item)
        masters_wanted_formats.setdefault(item.master_id, set()).update(
            f.lower() for f in item.formats
        )
    standalone_items = [item for item in wantlist if not item.master_id]
    print(
        f"{len(masters_to_check)} item(s) belong to a master release, "
        f"{len(standalone_items)} item(s) currently have no master release."
    )

    new_findings: list[tuple[WantlistItem, list[Version]]] = []
    all_master_entries: list[tuple[WantlistItem, list[Version]]] = []

    for i, (master_id, item) in enumerate(masters_to_check.items(), start=1):
        print(f"[{i}/{len(masters_to_check)}] Checking master {master_id} ({item.artists} - {item.title})...")
        wanted_formats = frozenset(masters_wanted_formats.get(master_id, set()))
        all_versions = client.get_master_versions(master_id)
        versions = [v for v in all_versions if v.matches_formats(wanted_formats)]
        if wanted_formats and len(versions) < len(all_versions):
            print(
                f"    Filtering to wantlisted format(s) {sorted(wanted_formats)}; "
                f"ignoring {len(all_versions) - len(versions)} version(s) in other format(s)."
            )
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

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for item, versions in new_findings:
        record_discoveries(state, item, versions, today)

    shipping_config = {
        "eu_shipping_eur": float(env("SHIPPING_ESTIMATE_EU_EUR", required=False, default="20")),
        "non_eu_shipping_eur": float(env("SHIPPING_ESTIMATE_NON_EU_EUR", required=False, default="22")),
        "non_eu_vat_pct": float(env("NON_EU_VAT_PCT", required=False, default="27")),
    }

    price_limit = float(env("MARKETPLACE_PRICE_LIMIT_EUR", required=False, default="100"))
    previously_flagged = set(state.setdefault("marketplace_flagged_release_ids", []))
    currently_flagged: dict[int, dict] = {}

    print(f"Checking marketplace availability for {len(wantlist)} wantlist item(s) (limit: EUR {price_limit:.2f})...")
    for item in wantlist:
        try:
            stats = client.get_marketplace_stats(item.release_id)
        except requests.HTTPError as e:
            print(f"    Could not fetch marketplace stats for release {item.release_id}: {e}", file=sys.stderr)
            continue
        lowest = stats.get("lowest_price")
        num_for_sale = stats.get("num_for_sale") or 0
        if not lowest or num_for_sale == 0:
            continue
        price = lowest.get("value")
        if price is not None and price <= price_limit:
            estimated_total_eu, estimated_total_non_eu = compute_shipping_estimates(price, shipping_config)
            currently_flagged[item.release_id] = {
                "item": item,
                "price": price,
                "num_for_sale": num_for_sale,
                "estimated_total_eu": estimated_total_eu,
                "estimated_total_non_eu": estimated_total_non_eu,
            }

    new_marketplace_alerts = {
        rid: info for rid, info in currently_flagged.items() if rid not in previously_flagged
    }
    state["marketplace_flagged_release_ids"] = sorted(currently_flagged.keys())
    print(
        f"{len(currently_flagged)} item(s) currently listed under EUR {price_limit:.2f}, "
        f"{len(new_marketplace_alerts)} new since last run."
    )

    state["standalone_release_ids"] = sorted({item.release_id for item in standalone_items})
    save_state(state)
    current_release_ids = {item.release_id for item in wantlist}
    write_readable_state(
        username,
        all_master_entries,
        standalone_items,
        state.get("discovered_versions", {}),
        current_release_ids,
        currently_flagged,
        price_limit,
        shipping_config,
    )

    lines: list[str] = []
    if new_findings:
        lines.append("New versions/reissues found for items on your Discogs wantlist:")
        lines.append("")
        for item, versions in new_findings:
            lines.append(f"{item.artists} - {item.title}")
            for v in versions:
                details = " / ".join(
                    p for p in [", ".join(sorted(v.major_formats)), v.format, v.country, v.released] if p
                )
                lines.append(f"  - {v.title} ({details}) -> {v.url}")
            lines.append("")

    if new_marketplace_alerts:
        lines.append(f"New marketplace listings under EUR {price_limit:.2f} (excl. shipping/fees):")
        lines.append("")
        for release_id, info in sorted(
            new_marketplace_alerts.items(), key=lambda kv: (kv[1]["item"].artists, kv[1]["item"].title)
        ):
            item = info["item"]
            url = f"https://www.discogs.com/sell/release/{release_id}?ev=rb&currency=EUR"
            est = format_estimate_range(info)
            lines.append(
                f"  - {item.artists} - {item.title}: from EUR {info['price']:.2f}{est} "
                f"({info['num_for_sale']} for sale) -> {url}"
            )
        lines.append("")
        lines.append(format_shipping_estimate_caveat(shipping_config))
        lines.append("")

    if not lines:
        print("No new versions or marketplace alerts found.")
        return

    body = "\n".join(lines)
    print(body)

    subject_parts = []
    if new_findings:
        subject_parts.append(f"{sum(len(v) for _, v in new_findings)} new version(s)")
    if new_marketplace_alerts:
        subject_parts.append(f"{len(new_marketplace_alerts)} listing(s) under EUR {price_limit:.0f}")

    send_email(
        subject=f"Discogs wantlist: {', '.join(subject_parts)}",
        body=body,
    )


if __name__ == "__main__":
    main()
