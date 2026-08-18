#!/usr/bin/env python3
"""Policy-driven Google Tag Manager auditor.

Audits GTM tags and their firing/blocking triggers for configuration drift.
Optional --fix mode can remediate safe tag-level policy violations. Writes are
still dry-run by default; pass --execute to apply them.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

import yaml

from gtm_manager import HttpError, TagMutation, authenticate, update_tag, workspace_path


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    policy: str
    entity_type: str
    entity_id: str
    entity_name: str
    message: str
    fixable: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "severity": self.severity,
            "code": self.code,
            "policy": self.policy,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "entity_name": self.entity_name,
            "message": self.message,
            "fixable": self.fixable,
        }


def iter_triggers(service: Any, parent: str) -> Iterator[dict[str, Any]]:
    page_token: str | None = None
    while True:
        request = service.accounts().containers().workspaces().triggers().list(
            parent=parent, pageToken=page_token
        )
        response = request.execute()
        yield from response.get("trigger", [])
        page_token = response.get("nextPageToken")
        if not page_token:
            break


def load_policy(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("Policy file must contain a YAML object.")
    policies = data.get("policies")
    if not isinstance(policies, dict) or not policies:
        raise ValueError("Policy file must define a non-empty 'policies' mapping.")
    return data


def consent_types(tag: dict[str, Any]) -> set[str]:
    settings = tag.get("consentSettings") or {}
    return {
        item.get("value")
        for item in settings.get("consentType", {}).get("list", [])
        if item.get("value")
    }


def tag_matches(tag: dict[str, Any], match: dict[str, Any]) -> bool:
    name = tag.get("name", "")
    if regex := match.get("tag_name_regex"):
        if not re.search(regex, name, flags=re.IGNORECASE):
            return False
    if contains := match.get("tag_name_contains"):
        if contains.lower() not in name.lower():
            return False
    if tag_type := match.get("tag_type"):
        allowed = {tag_type} if isinstance(tag_type, str) else set(tag_type)
        if tag.get("type") not in allowed:
            return False
    if "paused" in match and bool(tag.get("paused", False)) != bool(match["paused"]):
        return False
    if folder_id := match.get("folder_id"):
        if str(tag.get("parentFolderId", "")) != str(folder_id):
            return False
    return True


def _parameter_value(parameter: Any) -> Any:
    if not isinstance(parameter, dict):
        return parameter
    if "list" in parameter:
        return [_parameter_value(item) for item in parameter.get("list", [])]
    if "map" in parameter:
        return {
            str(item.get("key", "")): _parameter_value(item)
            for item in parameter.get("map", [])
        }
    return {
        key: value
        for key, value in parameter.items()
        if key != "type" and value not in (None, "", [], {})
    }


def canonical_condition(condition: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": condition.get("type"),
        "parameter": [_parameter_value(param) for param in condition.get("parameter", [])],
    }


def trigger_signature(trigger: dict[str, Any]) -> str:
    normalized = {
        "type": trigger.get("type"),
        "filter": [canonical_condition(c) for c in trigger.get("filter", [])],
        "customEventFilter": [
            canonical_condition(c) for c in trigger.get("customEventFilter", [])
        ],
        "autoEventFilter": [
            canonical_condition(c) for c in trigger.get("autoEventFilter", [])
        ],
        "waitForTags": _parameter_value(trigger.get("waitForTags")),
        "checkValidation": _parameter_value(trigger.get("checkValidation")),
        "waitForTagsTimeout": _parameter_value(trigger.get("waitForTagsTimeout")),
        "uniqueTriggerId": _parameter_value(trigger.get("uniqueTriggerId")),
        "eventName": _parameter_value(trigger.get("eventName")),
        "interval": _parameter_value(trigger.get("interval")),
        "limit": _parameter_value(trigger.get("limit")),
        "selector": _parameter_value(trigger.get("selector")),
        "intervalSeconds": _parameter_value(trigger.get("intervalSeconds")),
        "maxTimerLengthSeconds": _parameter_value(trigger.get("maxTimerLengthSeconds")),
        "verticalScrollPercentageList": _parameter_value(trigger.get("verticalScrollPercentageList")),
        "horizontalScrollPercentageList": _parameter_value(trigger.get("horizontalScrollPercentageList")),
    }
    raw = json.dumps(normalized, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def find_trigger_ids(
    triggers: Iterable[dict[str, Any]],
    *,
    exact_name: str | None = None,
    name_regex: str | None = None,
) -> list[str]:
    matches = []
    for trigger in triggers:
        name = trigger.get("name", "")
        if exact_name is not None and name != exact_name:
            continue
        if name_regex is not None and not re.search(name_regex, name, re.IGNORECASE):
            continue
        matches.append(str(trigger.get("triggerId")))
    return matches


def _finding(
    severity: str,
    code: str,
    policy: str,
    tag: dict[str, Any],
    message: str,
    fixable: bool = False,
) -> Finding:
    return Finding(
        severity=severity,
        code=code,
        policy=policy,
        entity_type="tag",
        entity_id=str(tag.get("tagId", "?")),
        entity_name=tag.get("name", "<unnamed>"),
        message=message,
        fixable=fixable,
    )


def audit_tag(
    tag: dict[str, Any],
    *,
    policy_name: str,
    rules: dict[str, Any],
    trigger_by_id: dict[str, dict[str, Any]],
    all_triggers: list[dict[str, Any]],
) -> list[Finding]:
    findings: list[Finding] = []
    required = rules.get("require", {})
    trigger_rules = rules.get("triggers", {})

    expected_status = required.get("consent_status")
    if expected_status:
        actual_status = (tag.get("consentSettings") or {}).get("consentStatus", "notSet")
        if actual_status != expected_status:
            findings.append(_finding("ERROR", "CONSENT_STATUS", policy_name, tag, f"consent status is {actual_status}; expected {expected_status}", True))

    expected_types = set(required.get("consent_types", []))
    if expected_types:
        actual_types = consent_types(tag)
        missing = sorted(expected_types - actual_types)
        extra = sorted(actual_types - expected_types)
        if missing or extra:
            detail = []
            if missing:
                detail.append(f"missing={','.join(missing)}")
            if extra:
                detail.append(f"extra={','.join(extra)}")
            findings.append(_finding("ERROR", "CONSENT_TYPES", policy_name, tag, "consent types differ from policy (" + "; ".join(detail) + ")", True))

    if "paused" in required:
        expected_paused = bool(required["paused"])
        actual_paused = bool(tag.get("paused", False))
        if actual_paused != expected_paused:
            findings.append(_finding("ERROR", "PAUSED_STATE", policy_name, tag, f"paused={actual_paused}; expected {expected_paused}", True))

    if expected_option := required.get("firing_option"):
        actual_option = tag.get("tagFiringOption", "tagFiringOptionUnspecified")
        if actual_option != expected_option:
            findings.append(_finding("ERROR", "FIRING_OPTION", policy_name, tag, f"firing option is {actual_option}; expected {expected_option}", True))

    firing_triggers = [
        trigger_by_id[trigger_id]
        for trigger_id in map(str, tag.get("firingTriggerId", []))
        if trigger_id in trigger_by_id
    ]
    missing_firing_ids = [
        trigger_id
        for trigger_id in map(str, tag.get("firingTriggerId", []))
        if trigger_id not in trigger_by_id
    ]
    if missing_firing_ids:
        findings.append(_finding("ERROR", "MISSING_FIRING_TRIGGER", policy_name, tag, "references unknown firing trigger ID(s): " + ", ".join(missing_firing_ids)))

    allowed_types = set(trigger_rules.get("allowed_types", []))
    if allowed_types:
        for trigger in firing_triggers:
            if trigger.get("type") not in allowed_types:
                findings.append(_finding("ERROR", "TRIGGER_TYPE", policy_name, tag, f"firing trigger '{trigger.get('name')}' uses type {trigger.get('type')}; allowed={sorted(allowed_types)}"))

    if trigger_name_regex := trigger_rules.get("firing_trigger_name_regex"):
        if not firing_triggers:
            findings.append(_finding("ERROR", "NO_FIRING_TRIGGER", policy_name, tag, "tag has no firing trigger"))
        for trigger in firing_triggers:
            if not re.search(trigger_name_regex, trigger.get("name", ""), re.IGNORECASE):
                findings.append(_finding("WARN", "FIRING_TRIGGER_NAME", policy_name, tag, f"trigger '{trigger.get('name')}' does not match /{trigger_name_regex}/"))

    required_block_name = trigger_rules.get("required_blocking_trigger")
    required_block_regex = trigger_rules.get("required_blocking_trigger_regex")
    if required_block_name or required_block_regex:
        candidate_ids = find_trigger_ids(all_triggers, exact_name=required_block_name, name_regex=required_block_regex)
        current = {str(x) for x in tag.get("blockingTriggerId", [])}
        if len(candidate_ids) == 1:
            expected_id = candidate_ids[0]
            if expected_id not in current:
                label = required_block_name or required_block_regex
                findings.append(_finding("ERROR", "BLOCKING_TRIGGER", policy_name, tag, f"missing required blocking trigger '{label}'", True))
        elif len(candidate_ids) == 0:
            label = required_block_name or required_block_regex
            findings.append(_finding("ERROR", "POLICY_TRIGGER_NOT_FOUND", policy_name, tag, f"policy blocking trigger '{label}' was not found in workspace"))
        else:
            label = required_block_name or required_block_regex
            findings.append(_finding("ERROR", "POLICY_TRIGGER_AMBIGUOUS", policy_name, tag, f"policy blocking trigger '{label}' matched {len(candidate_ids)} triggers"))

    return findings


def audit_trigger_consistency(selected_tags: list[dict[str, Any]], *, policy_name: str, trigger_by_id: dict[str, dict[str, Any]]) -> list[Finding]:
    trigger_usage: dict[str, dict[str, Any]] = {}
    for tag in selected_tags:
        for trigger_id in map(str, tag.get("firingTriggerId", [])):
            if trigger_id in trigger_by_id:
                trigger_usage[trigger_id] = trigger_by_id[trigger_id]
    if len(trigger_usage) <= 1:
        return []
    signature_groups: dict[str, list[dict[str, Any]]] = {}
    for trigger in trigger_usage.values():
        signature_groups.setdefault(trigger_signature(trigger), []).append(trigger)
    if len(signature_groups) <= 1:
        return []
    summary = "; ".join(
        f"{sig}=[{', '.join(t.get('name', '?') for t in items)}]"
        for sig, items in sorted(signature_groups.items())
    )
    return [Finding("WARN", "TRIGGER_DRIFT", policy_name, "policy", policy_name, policy_name, f"matched tags use {len(signature_groups)} distinct firing-trigger configurations: {summary}", False)]


def audit_duplicate_triggers(triggers: list[dict[str, Any]]) -> list[Finding]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for trigger in triggers:
        groups.setdefault(trigger_signature(trigger), []).append(trigger)
    findings = []
    for sig, items in groups.items():
        if len(items) < 2:
            continue
        findings.append(Finding("WARN", "DUPLICATE_TRIGGER", "global", "trigger", sig, " / ".join(t.get("name", "?") for t in items), "behaviorally equivalent trigger definitions detected: " + ", ".join(f"{t.get('name')} (id={t.get('triggerId')})" for t in items), False))
    return findings


def audit_orphan_triggers(tags: list[dict[str, Any]], triggers: list[dict[str, Any]]) -> list[Finding]:
    used = set()
    for tag in tags:
        used.update(map(str, tag.get("firingTriggerId", [])))
        used.update(map(str, tag.get("blockingTriggerId", [])))
    return [
        Finding("INFO", "ORPHAN_TRIGGER", "global", "trigger", str(trigger.get("triggerId")), trigger.get("name", "<unnamed>"), "trigger is not referenced by any tag as firing or blocking", False)
        for trigger in triggers
        if str(trigger.get("triggerId")) not in used
    ]


def audit_workspace(tags: list[dict[str, Any]], triggers: list[dict[str, Any]], config: dict[str, Any]) -> tuple[list[Finding], dict[str, list[dict[str, Any]]]]:
    trigger_by_id = {str(t.get("triggerId")): t for t in triggers}
    findings: list[Finding] = []
    selections: dict[str, list[dict[str, Any]]] = {}
    for policy_name, policy in config["policies"].items():
        if not isinstance(policy, dict):
            raise ValueError(f"Policy '{policy_name}' must be a YAML object.")
        selected = [tag for tag in tags if tag_matches(tag, policy.get("match", {}))]
        selections[policy_name] = selected
        for tag in selected:
            findings.extend(audit_tag(tag, policy_name=policy_name, rules=policy, trigger_by_id=trigger_by_id, all_triggers=triggers))
        if policy.get("triggers", {}).get("require_equivalent_firing_triggers"):
            findings.extend(audit_trigger_consistency(selected, policy_name=policy_name, trigger_by_id=trigger_by_id))
    global_checks = config.get("global_checks", {})
    if global_checks.get("duplicate_triggers", True):
        findings.extend(audit_duplicate_triggers(triggers))
    if global_checks.get("orphan_triggers", False):
        findings.extend(audit_orphan_triggers(tags, triggers))
    severity_order = {"ERROR": 0, "WARN": 1, "INFO": 2}
    findings.sort(key=lambda f: (severity_order.get(f.severity, 99), f.policy, f.entity_name.lower(), f.code))
    return findings, selections


def build_tag_fixes(tags: list[dict[str, Any]], triggers: list[dict[str, Any]], config: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any], list[str]]]:
    planned_by_id: dict[str, tuple[dict[str, Any], dict[str, Any], list[str]]] = {}
    for policy_name, policy in config["policies"].items():
        selected = [tag for tag in tags if tag_matches(tag, policy.get("match", {}))]
        required = policy.get("require", {})
        trigger_rules = policy.get("triggers", {})
        for original in selected:
            tag_id = str(original.get("tagId"))
            before, updated, changes = planned_by_id.get(tag_id, (original, copy.deepcopy(original), []))
            mutation = TagMutation(
                consent_status=required.get("consent_status"),
                consent_types=tuple(required.get("consent_types", [])),
                paused=required.get("paused") if "paused" in required else None,
                firing_option=required.get("firing_option"),
            )
            updated, mutation_changes = mutation.apply(updated)
            changes.extend(f"{policy_name}: {change}" for change in mutation_changes)
            block_name = trigger_rules.get("required_blocking_trigger")
            block_regex = trigger_rules.get("required_blocking_trigger_regex")
            if block_name or block_regex:
                candidate_ids = find_trigger_ids(triggers, exact_name=block_name, name_regex=block_regex)
                if len(candidate_ids) == 1:
                    current = list(map(str, updated.get("blockingTriggerId", [])))
                    if candidate_ids[0] not in current:
                        updated["blockingTriggerId"] = current + [candidate_ids[0]]
                        changes.append(f"{policy_name}: add blocking trigger {block_name or block_regex}")
            planned_by_id[tag_id] = (before, updated, changes)
    return [item for item in planned_by_id.values() if item[2]]


def print_findings(findings: list[Finding], selections: dict[str, list[dict[str, Any]]]) -> None:
    print("GTM POLICY AUDIT")
    print("=" * 72)
    for policy, tags in selections.items():
        print(f"{policy}: {len(tags)} tag(s) inspected")
    print()
    if not findings:
        print("PASS: no policy violations or requested global findings.")
        return
    for finding in findings:
        fix = " [fixable]" if finding.fixable else ""
        print(f"{finding.severity:<5} {finding.code:<25} {finding.entity_name}{fix}")
        print(f"      policy={finding.policy} | {finding.message}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit GTM tags and triggers against a YAML governance policy.")
    parser.add_argument("--account-id", required=True)
    parser.add_argument("--container-id", required=True)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--policy", type=Path, default=Path("policies/advertising.yaml"))
    parser.add_argument("--client-secrets", type=Path, default=Path("client_secrets.json"))
    parser.add_argument("--token-file", type=Path, default=Path("token.json"))
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on", choices=["error", "warn", "never"], default="error")
    parser.add_argument("--fix", action="store_true", help="Plan safe tag-level remediation")
    parser.add_argument("--execute", action="store_true", help="Apply --fix changes")
    parser.add_argument("--max-updates", type=int, default=50)
    return parser


def should_fail(findings: list[Finding], threshold: str) -> bool:
    if threshold == "never":
        return False
    if threshold == "warn":
        return any(f.severity in {"ERROR", "WARN"} for f in findings)
    return any(f.severity == "ERROR" for f in findings)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.execute and not args.fix:
        parser.error("--execute requires --fix")
    if args.max_updates < 1:
        parser.error("--max-updates must be at least 1")
    try:
        config = load_policy(args.policy)
        service = authenticate(args.client_secrets, args.token_file)
        parent = workspace_path(args.account_id, args.container_id, args.workspace_id)
        from gtm_manager import iter_tags
        tags = list(iter_tags(service, parent))
        triggers = list(iter_triggers(service, parent))
        findings, selections = audit_workspace(tags, triggers, config)
        if args.json:
            print(json.dumps({"summary": {"tags": len(tags), "triggers": len(triggers), "policies": {name: len(items) for name, items in selections.items()}, "findings": len(findings)}, "findings": [f.to_dict() for f in findings]}, indent=2))
        else:
            print_findings(findings, selections)
        if args.fix:
            planned = build_tag_fixes(tags, triggers, config)
            if not args.json:
                print("\nREMEDIATION PLAN")
                print("=" * 72)
                if not planned:
                    print("No safe tag-level fixes are required.")
                for before, _after, changes in planned:
                    print(f"[{before.get('tagId')}] {before.get('name')}")
                    for change in changes:
                        print(f"  - {change}")
            if args.execute and planned:
                if len(planned) > args.max_updates:
                    print(f"Refusing to update {len(planned)} tags because --max-updates={args.max_updates}.", file=sys.stderr)
                    return 2
                failures = 0
                for before, updated, _changes in planned:
                    try:
                        update_tag(service, updated)
                        if not args.json:
                            print(f"UPDATED: {before.get('name')}")
                    except HttpError as exc:
                        failures += 1
                        print(f"FAILED: {before.get('name')} - {exc}", file=sys.stderr)
                if failures:
                    return 1
            elif planned and not args.execute and not args.json:
                print("\nDry-run only. Re-run with --fix --execute to apply.")
        return 1 if should_fail(findings, args.fail_on) else 0
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except HttpError as exc:
        print(f"GTM API ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
