#!/usr/bin/env python3
import argparse
import base64
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, Iterable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urljoin
from urllib.request import Request, urlopen


STAR_BLOCK_RE = re.compile(r"\s[\u2605\u2606]{5}\s*")


def parse_date_yyyy_mm_dd(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"Invalid date '{value}'. Use YYYY-MM-DD.") from exc


def extract_review_text(description: Optional[str]) -> Optional[str]:
    if not description:
        return None

    text = description.strip()
    if not text:
        return None

    marker = STAR_BLOCK_RE.search(text)
    if not marker:
        return None

    candidate = text[: marker.start()].strip()
    return candidate or None


@dataclass
class ZendeskConfig:
    subdomain: str
    email: str
    api_token: str
    timeout_seconds: int = 30
    max_retries: int = 5

    @property
    def base_url(self) -> str:
        return f"https://{self.subdomain}.zendesk.com"

    @property
    def auth_header_value(self) -> str:
        token = f"{self.email}/token:{self.api_token}".encode("utf-8")
        return "Basic " + base64.b64encode(token).decode("ascii")


class ZendeskClient:
    def __init__(self, config: ZendeskConfig):
        self.config = config

    def request(
        self,
        method: str,
        path_or_url: str,
        params: Optional[Dict[str, str]] = None,
        json_body: Optional[Dict] = None,
    ) -> Dict:
        if path_or_url.startswith("http://") or path_or_url.startswith("https://"):
            url = path_or_url
        else:
            url = urljoin(self.config.base_url, path_or_url)

        if params:
            url = f"{url}{'&' if '?' in url else '?'}{urlencode(params)}"

        body = None
        headers = {
            "Authorization": self.config.auth_header_value,
            "Accept": "application/json",
        }

        if json_body is not None:
            body = json.dumps(json_body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        attempt = 0
        while True:
            attempt += 1
            request = Request(url=url, data=body, method=method, headers=headers)
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    payload = response.read().decode("utf-8")
                    if not payload:
                        return {}
                    return json.loads(payload)
            except HTTPError as err:
                status = err.code
                retry_after = err.headers.get("Retry-After")
                error_body = err.read().decode("utf-8", errors="replace")

                if attempt < self.config.max_retries and (status == 429 or 500 <= status <= 599):
                    sleep_seconds = int(retry_after) if retry_after and retry_after.isdigit() else min(30, 2**attempt)
                    print(
                        f"[WARN] {method} {url} failed with HTTP {status}. Retrying in {sleep_seconds}s (attempt {attempt}/{self.config.max_retries}).",
                        flush=True,
                    )
                    time.sleep(sleep_seconds)
                    continue

                raise RuntimeError(
                    f"Zendesk request failed: {method} {url} -> HTTP {status}. Response: {error_body}"
                ) from err
            except URLError as err:
                if attempt < self.config.max_retries:
                    sleep_seconds = min(30, 2**attempt)
                    print(
                        f"[WARN] {method} {url} network error: {err}. Retrying in {sleep_seconds}s (attempt {attempt}/{self.config.max_retries}).",
                        flush=True,
                    )
                    time.sleep(sleep_seconds)
                    continue
                raise RuntimeError(f"Network error during request to {url}: {err}") from err


def resolve_window(from_date: Optional[date], to_date: Optional[date]) -> Tuple[date, date]:
    if (from_date is None) ^ (to_date is None):
        raise ValueError("Both --from-date and --to-date must be supplied together.")

    if from_date and to_date:
        if to_date < from_date:
            raise ValueError("--to-date must be on or after --from-date.")
        return from_date, to_date + timedelta(days=1)

    today_utc = datetime.now(timezone.utc).date()
    return today_utc - timedelta(days=1), today_utc


def iter_ticket_ids(
    client: ZendeskClient,
    query: str,
    page_size: int,
) -> Iterable[int]:
    next_url = "/api/v2/search/export"
    params = {
        "query": query,
        "filter[type]": "ticket",
        "page[size]": str(page_size),
    }

    while True:
        page = client.request("GET", next_url, params=params)
        params = None

        results = page.get("results", [])
        for row in results:
            ticket_id = row.get("id")
            if isinstance(ticket_id, int):
                yield ticket_id

        meta = page.get("meta") or {}
        links = page.get("links") or {}
        has_more = bool(meta.get("has_more"))
        next_link = links.get("next")

        if not has_more or not next_link:
            break

        next_url = next_link


def get_current_custom_field_value(ticket: Dict, field_id: int) -> Optional[str]:
    for field in ticket.get("custom_fields", []):
        if field.get("id") == field_id:
            return field.get("value")
    return None


def update_ticket_review_field(
    client: ZendeskClient,
    ticket: Dict,
    field_id: int,
    value: Optional[str],
    dry_run: bool,
) -> str:
    ticket_id = ticket["id"]
    current_value = get_current_custom_field_value(ticket, field_id)
    normalized_current = (current_value or "").strip()
    normalized_target = (value or "").strip()

    if normalized_current == normalized_target:
        return "unchanged"

    if dry_run:
        return "would_update"

    payload = {
        "ticket": {
            "custom_fields": [{"id": field_id, "value": value}],
            "safe_update": True,
            "updated_stamp": ticket.get("updated_at"),
        }
    }

    try:
        client.request("PUT", f"/api/v2/tickets/{ticket_id}.json", json_body=payload)
        return "updated"
    except RuntimeError as err:
        if "HTTP 409" not in str(err):
            raise

    refreshed = client.request("GET", f"/api/v2/tickets/{ticket_id}.json").get("ticket", {})
    payload["ticket"]["updated_stamp"] = refreshed.get("updated_at")
    client.request("PUT", f"/api/v2/tickets/{ticket_id}.json", json_body=payload)
    return "updated_after_retry"


def build_query(via_value: str, created_start_inclusive: date, created_end_exclusive: date) -> str:
    return (
        f'via:"{via_value}" '
        f"created>={created_start_inclusive.isoformat()} "
        f"created<{created_end_exclusive.isoformat()}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch Zendesk tickets created via a specific channel and copy extracted review "
            "text from ticket.description into a target custom ticket field."
        )
    )
    parser.add_argument("--from-date", type=parse_date_yyyy_mm_dd, default=None, help="Start date (YYYY-MM-DD), inclusive.")
    parser.add_argument("--to-date", type=parse_date_yyyy_mm_dd, default=None, help="End date (YYYY-MM-DD), inclusive.")
    parser.add_argument("--via", default=os.getenv("ZENDESK_VIA_VALUE", "google_my_business"), help="Value used for via:\"...\" search filter.")
    parser.add_argument(
        "--field-id",
        type=int,
        default=int(os.getenv("TARGET_CUSTOM_FIELD_ID", "34603570445085")),
        help="Destination custom ticket field id.",
    )
    parser.add_argument(
        "--page-size",
        type=int,
        default=int(os.getenv("ZENDESK_SEARCH_EXPORT_PAGE_SIZE", "100")),
        help="Page size for search export (1-1000, Zendesk recommends 100).",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not update tickets; print intended actions only.")
    return parser.parse_args()


def required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def main() -> int:
    args = parse_args()

    if args.page_size < 1 or args.page_size > 1000:
        raise RuntimeError("--page-size must be between 1 and 1000.")

    start_date, end_date_exclusive = resolve_window(args.from_date, args.to_date)
    query = build_query(args.via, start_date, end_date_exclusive)

    print(f"[INFO] Window: {start_date.isoformat()} to {(end_date_exclusive - timedelta(days=1)).isoformat()} (inclusive)")
    print(f"[INFO] Query: {query}")
    print(f"[INFO] Target custom field: {args.field_id}")
    print(f"[INFO] Dry run: {args.dry_run}")

    config = ZendeskConfig(
        subdomain=required_env("ZENDESK_SUBDOMAIN"),
        email=required_env("ZENDESK_EMAIL"),
        api_token=required_env("ZENDESK_API_TOKEN"),
    )
    client = ZendeskClient(config)

    processed = 0
    updated = 0
    unchanged = 0
    cleared = 0

    for ticket_id in iter_ticket_ids(client, query=query, page_size=args.page_size):
        processed += 1
        ticket = client.request("GET", f"/api/v2/tickets/{ticket_id}.json").get("ticket", {})
        description = ticket.get("description")
        extracted_review = extract_review_text(description)

        if extracted_review is None:
            cleared += 1

        status = update_ticket_review_field(
            client=client,
            ticket=ticket,
            field_id=args.field_id,
            value=extracted_review,
            dry_run=args.dry_run,
        )

        if status in {"updated", "updated_after_retry", "would_update"}:
            updated += 1
        else:
            unchanged += 1

        print(f"[INFO] Ticket {ticket_id}: {status}")

    print("[INFO] Run completed.")
    print(f"[INFO] Tickets processed: {processed}")
    print(f"[INFO] Tickets updated: {updated}")
    print(f"[INFO] Tickets unchanged: {unchanged}")
    print(f"[INFO] Tickets where extracted review was empty: {cleared}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
