#!/usr/bin/env python3
"""Programmatic Google Tag Manager administration.

Bulk-filter GTM tags and safely update consent, pause state, firing options,
or notes. Dry-run is the default; pass --execute to write changes.
"""

from __future__ import annotations

import argparse
import copy
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

try:
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build
    from googleapiclient.errors import HttpError
except ModuleNotFoundError:  # Allows pure filtering/mutation tests without API deps installed.
    Request = None
    Credentials = None
    InstalledAppFlow = None
    build = None

    class HttpError(Exception):
        pass

EDIT_SCOPE = "https://www.googleapis.com/auth/tagmanager.edit.containers"
SCOPES = [EDIT_SCOPE]
VALID_CONSENT_STATUS = {"needed", "notNeeded", "notSet"}
VALID_FIRING_OPTIONS = {
    "tagFiringOptionUnspecified",
    "unlimited",
    "oncePerEvent",
    "oncePerLoad",
}


@dataclass(frozen=True)
class TagFilter:
    name_contains: str | None = None
    name_regex: str | None = None
    tag_type: str | None = None
    paused: bool | None = None
    consent_status: str | None = None
    folder_id: str | None = None
    tag_ids: frozenset[str] = frozenset()

    def matches(self, tag: dict[str, Any]) -> bool:
        """Return True when a tag satisfies every supplied criterion."""
        name = tag.get("name", "")

        if self.name_contains and self.name_contains.lower() not in name.lower():
            return False
        if self.name_regex and not re.search(self.name_regex, name, flags=re.IGNORECASE):
            return False
        if self.tag_type and tag.get("type") != self.tag_type:
            return False
        if self.paused is not None and bool(tag.get("paused", False)) != self.paused:
            return False
        if self.consent_status:
            status = tag.get("consentSettings", {}).get("consentStatus", "notSet")
            if status != self.consent_status:
                return False
        if self.folder_id and tag.get("parentFolderId") != self.folder_id:
            return False
        if self.tag_ids and str(tag.get("tagId")) not in self.tag_ids:
            return False
        return True


@dataclass(frozen=True)
class TagMutation:
    consent_status: str | None = None
    consent_types: tuple[str, ...] = ()
    paused: bool | None = None
    firing_option: str | None = None
    note: str | None = None
    append_note: bool = False

    def apply(self, tag: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
        """Return a mutated copy plus human-readable change descriptions."""
        updated = copy.deepcopy(tag)
        changes: list[str] = []

        if self.consent_status is not None:
            before = updated.get("consentSettings", {})
            after = build_consent_settings(self.consent_status, self.consent_types)
            if before != after:
                updated["consentSettings"] = after
                changes.append(f"consent: {format_consent(before)} -> {format_consent(after)}")

        if self.paused is not None:
            before_paused = bool(updated.get("paused", False))
            if before_paused != self.paused:
                updated["paused"] = self.paused
                changes.append(f"paused: {before_paused} -> {self.paused}")

        if self.firing_option is not None:
            before_option = updated.get("tagFiringOption", "tagFiringOptionUnspecified")
            if before_option != self.firing_option:
                updated["tagFiringOption"] = self.firing_option
                changes.append(f"firing option: {before_option} -> {self.firing_option}")

        if self.note is not None:
            before_note = updated.get("notes", "")
            if self.append_note and before_note:
                after_note = f"{before_note}\n{self.note}"
            else:
                after_note = self.note
            if before_note != after_note:
                updated["notes"] = after_note
                changes.append("notes updated")

        return updated, changes


def build_consent_settings(status: str, consent_types: Iterable[str]) -> dict[str, Any]:
    """Build GTM's ConsentSetting resource shape."""
    if status not in VALID_CONSENT_STATUS:
        raise ValueError(f"Invalid consent status: {status}")

    types = tuple(dict.fromkeys(t.strip() for t in consent_types if t.strip()))
    settings: dict[str, Any] = {"consentStatus": status}

    if status == "needed":
        if not types:
            raise ValueError("Consent status 'needed' requires at least one consent type.")
        settings["consentType"] = {
            "type": "list",
            "list": [{"type": "template", "value": consent_type} for consent_type in types],
        }
    return settings


def format_consent(settings: dict[str, Any] | None) -> str:
    if not settings:
        return "notSet"
    status = settings.get("consentStatus", "notSet")
    values = [
        item.get("value", "")
        for item in settings.get("consentType", {}).get("list", [])
        if item.get("value")
    ]
    return f"{status}({','.join(values)})" if values else status


def authenticate(client_secrets: Path, token_file: Path):
    """Authenticate with OAuth 2.0 and return a GTM v2 service."""
    if build is None or Credentials is None or InstalledAppFlow is None or Request is None:
        raise RuntimeError("Google API dependencies are not installed. Run: pip install -r requirements.txt")

    credentials: Credentials | None = None

    if token_file.exists():
        credentials = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        if not client_secrets.exists():
            raise FileNotFoundError(
                f"OAuth client secrets file not found: {client_secrets}. "
                "Create an OAuth Desktop App credential in Google Cloud and download its JSON file."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), SCOPES)
        credentials = flow.run_local_server(port=0)
        token_file.write_text(credentials.to_json(), encoding="utf-8")

    return build("tagmanager", "v2", credentials=credentials, cache_discovery=False)


def workspace_path(account_id: str, container_id: str, workspace_id: str) -> str:
    return f"accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}"


def iter_tags(service: Any, parent: str) -> Iterator[dict[str, Any]]:
    """Yield all tags in a workspace, following API pagination."""
    page_token: str | None = None
    while True:
        request = service.accounts().containers().workspaces().tags().list(
            parent=parent, pageToken=page_token
        )
        response = request.execute()
        yield from response.get("tag", [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break


def update_tag(service: Any, tag: dict[str, Any]) -> dict[str, Any]:
    """Update one tag using its current fingerprint for optimistic concurrency."""
    kwargs = {"path": tag["path"], "body": tag}
    if tag.get("fingerprint"):
        kwargs["fingerprint"] = tag["fingerprint"]
    return service.accounts().containers().workspaces().tags().update(**kwargs).execute()


def summarize_tag(tag: dict[str, Any]) -> str:
    return (
        f"[{tag.get('tagId', '?')}] {tag.get('name', '<unnamed>')} "
        f"type={tag.get('type', '?')} paused={bool(tag.get('paused', False))} "
        f"consent={format_consent(tag.get('consentSettings'))}"
    )


def select_tags(tags: Iterable[dict[str, Any]], tag_filter: TagFilter) -> list[dict[str, Any]]:
    return [tag for tag in tags if tag_filter.matches(tag)]


def parse_bool_filter(value: str | None) -> bool | None:
    if value is None:
        return None
    return value == "true"


def add_location_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--account-id", required=True, help="GTM numeric account ID")
    parser.add_argument("--container-id", required=True, help="GTM numeric container ID")
    parser.add_argument("--workspace-id", required=True, help="GTM numeric workspace ID")


def add_filter_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name-contains", help="Case-insensitive substring match on tag name")
    parser.add_argument("--name-regex", help="Case-insensitive regex match on tag name")
    parser.add_argument("--type", dest="tag_type", help="Exact GTM tag type")
    parser.add_argument("--paused", choices=["true", "false"], help="Filter by paused state")
    parser.add_argument(
        "--consent-status",
        choices=sorted(VALID_CONSENT_STATUS),
        help="Filter by existing manual consent status",
    )
    parser.add_argument("--folder-id", help="Filter by GTM parent folder ID")
    parser.add_argument(
        "--tag-id",
        action="append",
        default=[],
        help="Filter by exact tag ID; repeat to match multiple IDs",
    )


def add_auth_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--client-secrets",
        type=Path,
        default=Path(os.getenv("GTM_CLIENT_SECRETS", "client_secrets.json")),
        help="OAuth Desktop App JSON (default: client_secrets.json or GTM_CLIENT_SECRETS)",
    )
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path(os.getenv("GTM_TOKEN_FILE", "token.json")),
        help="OAuth token cache (default: token.json or GTM_TOKEN_FILE)",
    )


def make_filter(args: argparse.Namespace) -> TagFilter:
    return TagFilter(
        name_contains=args.name_contains,
        name_regex=args.name_regex,
        tag_type=args.tag_type,
        paused=parse_bool_filter(args.paused),
        consent_status=args.consent_status,
        folder_id=args.folder_id,
        tag_ids=frozenset(args.tag_id),
    )


def command_list(service: Any, args: argparse.Namespace) -> int:
    parent = workspace_path(args.account_id, args.container_id, args.workspace_id)
    matched = select_tags(iter_tags(service, parent), make_filter(args))
    for tag in matched:
        print(summarize_tag(tag))
    print(f"\nMatched {len(matched)} tag(s).")
    return 0


def command_update(service: Any, args: argparse.Namespace) -> int:
    parent = workspace_path(args.account_id, args.container_id, args.workspace_id)
    matched = select_tags(iter_tags(service, parent), make_filter(args))

    mutation = TagMutation(
        consent_status=args.set_consent_status,
        consent_types=tuple(args.consent_type),
        paused=parse_bool_filter(args.set_paused),
        firing_option=args.set_firing_option,
        note=args.set_note or args.append_note,
        append_note=args.append_note is not None,
    )

    planned: list[tuple[dict[str, Any], dict[str, Any], list[str]]] = []
    for tag in matched:
        updated, changes = mutation.apply(tag)
        if changes:
            planned.append((tag, updated, changes))

    if not planned:
        print(f"Matched {len(matched)} tag(s), but no changes are required.")
        return 0

    mode = "EXECUTE" if args.execute else "DRY RUN"
    print(f"{mode}: {len(planned)} of {len(matched)} matched tag(s) would change.\n")
    for before, _after, changes in planned:
        print(summarize_tag(before))
        for change in changes:
            print(f"  - {change}")

    if not args.execute:
        print("\nNo writes performed. Re-run with --execute to apply these changes.")
        return 0

    if args.max_updates is not None and len(planned) > args.max_updates:
        print(
            f"Refusing to update {len(planned)} tags because --max-updates={args.max_updates}.",
            file=sys.stderr,
        )
        return 2

    failures = 0
    print()
    for before, updated, _changes in planned:
        try:
            result = update_tag(service, updated)
            print(f"UPDATED: {result.get('name', before.get('name'))}")
        except HttpError as exc:
            failures += 1
            print(f"FAILED: {before.get('name')} - {exc}", file=sys.stderr)

    print(f"\nUpdated {len(planned) - failures} tag(s); {failures} failure(s).")
    return 1 if failures else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Filter and bulk-manage Google Tag Manager tags programmatically."
    )
    add_auth_args(parser)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List tags matching criteria")
    add_location_args(list_parser)
    add_filter_args(list_parser)

    update_parser = subparsers.add_parser("update", help="Bulk-update tags matching criteria")
    add_location_args(update_parser)
    add_filter_args(update_parser)
    update_parser.add_argument(
        "--set-consent-status",
        choices=sorted(VALID_CONSENT_STATUS),
        help="Set manual consent status",
    )
    update_parser.add_argument(
        "--consent-type",
        action="append",
        default=[],
        help="Consent type required by the tag; repeat for multiple types",
    )
    update_parser.add_argument(
        "--set-paused", choices=["true", "false"], help="Pause or unpause matching tags"
    )
    update_parser.add_argument(
        "--set-firing-option",
        choices=sorted(VALID_FIRING_OPTIONS),
        help="Set tag firing option",
    )
    note_group = update_parser.add_mutually_exclusive_group()
    note_group.add_argument("--set-note", help="Replace tag notes")
    note_group.add_argument("--append-note", help="Append a line to tag notes")
    update_parser.add_argument(
        "--max-updates",
        type=int,
        default=50,
        help="Safety cap for writes (default: 50; ignored during dry-run)",
    )
    update_parser.add_argument(
        "--execute",
        action="store_true",
        help="Actually write changes. Without this flag the command is a dry-run.",
    )
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    if args.command != "update":
        return

    if not any(
        [
            args.set_consent_status is not None,
            args.set_paused is not None,
            args.set_firing_option is not None,
            args.set_note is not None,
            args.append_note is not None,
        ]
    ):
        parser.error("update requires at least one --set-* or --append-note action")

    if args.consent_type and args.set_consent_status != "needed":
        parser.error("--consent-type can only be used with --set-consent-status needed")
    if args.set_consent_status == "needed" and not args.consent_type:
        parser.error("--set-consent-status needed requires at least one --consent-type")
    if args.max_updates is not None and args.max_updates < 1:
        parser.error("--max-updates must be at least 1")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    validate_args(parser, args)

    try:
        service = authenticate(args.client_secrets, args.token_file)
        if args.command == "list":
            return command_list(service, args)
        if args.command == "update":
            return command_update(service, args)
        parser.error(f"Unknown command: {args.command}")
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except HttpError as exc:
        print(f"GTM API ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
