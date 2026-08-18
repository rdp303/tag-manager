# Programmatic Google Tag Manager

Python tooling for managing and auditing Google Tag Manager containers at scale.

The project has two pieces:

- **`gtm_manager.py`** — filter tags and safely bulk-update consent settings, pause state, firing options, or notes.
- **`audit.py`** — audit tags and triggers against YAML governance policies, detect configuration drift, and optionally remediate safe tag-level violations.

Both tools use the official Google Tag Manager API v2. Writes are **dry-run by default**.

## Why this exists

Large GTM containers accumulate dozens or hundreds of tags and triggers. Changes such as adding consent requirements, standardizing firing behavior, or checking whether every advertising tag uses the same trigger logic are repetitive and easy to apply inconsistently by hand.

This toolkit turns that work into a reviewable workflow:

1. Pull the current workspace configuration from GTM.
2. Select tags using explicit criteria or policy rules.
3. Show exact changes or audit findings.
4. Require an explicit flag before writing anything.
5. Leave versioning and publishing in GTM for human review.

## Features

### Bulk tag management

- OAuth 2.0 authentication against the GTM API v2
- Filters for tag name, regex, type, pause state, consent status, folder, and tag ID
- Bulk updates for GTM `consentSettings`
- Pause/unpause tags
- Change tag firing options
- Replace or append notes
- Pagination across large workspaces
- Dry-run by default
- Write caps with `--max-updates`
- Fingerprint-based optimistic concurrency protection

### Policy auditing

- YAML-based governance policies
- Identify advertising/marketing tags using naming rules
- Require standard consent configuration
- Require consistent firing options and pause state
- Validate allowed firing-trigger types
- Require a specific blocking trigger or blocking-trigger naming pattern
- Validate firing-trigger naming conventions
- Compare trigger definitions and flag **configuration drift**
- Detect behaviorally equivalent duplicate triggers
- Optionally identify orphan triggers
- JSON output for CI/reporting
- Configurable exit codes with `--fail-on`
- Safe tag-level remediation with `--fix`

## Setup

### 1. Create Google Cloud credentials

Enable the **Google Tag Manager API** in a Google Cloud project and create an OAuth 2.0 **Desktop app** credential. Download it as:

```text
client_secrets.json
```

Do not commit it. The file is ignored by `.gitignore`.

The toolkit requests:

```text
https://www.googleapis.com/auth/tagmanager.edit.containers
```

The Google account completing OAuth must have access to the GTM account/container being managed.

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Use GTM API IDs

Commands use GTM's numeric API IDs:

```text
accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}
```

The first authenticated run opens a browser for OAuth and stores the refreshable token locally in `token.json`.

---

# Bulk tag management

## List matching tags

```bash
python gtm_manager.py list \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --name-regex 'meta|facebook|tiktok|linkedin'
```

## Require advertising consent

Dry-run:

```bash
python gtm_manager.py update \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --name-regex 'meta|facebook|tiktok|linkedin' \
  --set-consent-status needed \
  --consent-type ad_storage \
  --consent-type ad_user_data
```

Apply after reviewing the output:

```bash
python gtm_manager.py update \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --name-regex 'meta|facebook|tiktok|linkedin' \
  --set-consent-status needed \
  --consent-type ad_storage \
  --consent-type ad_user_data \
  --execute
```

Other supported update actions include:

```text
--set-paused true|false
--set-firing-option unlimited|oncePerEvent|oncePerLoad|tagFiringOptionUnspecified
--set-note TEXT
--append-note TEXT
```

---

# GTM policy auditor

The auditor compares workspace tags and triggers against rules defined in YAML.

The included `policies/advertising.yaml` demonstrates two useful policy groups:

- all advertising tags must have standard consent/firing settings
- advertising purchase tags should use equivalent purchase-trigger logic

## Example policy

```yaml
policies:
  advertising:
    match:
      tag_name_regex: "(google ads|meta|facebook|tiktok|linkedin)"
    require:
      consent_status: needed
      consent_types:
        - ad_storage
        - ad_user_data
      paused: false
      firing_option: oncePerEvent
    triggers:
      allowed_types:
        - pageview
        - customEvent
        - click
        - linkClick
        - formSubmission

  advertising_purchase:
    match:
      tag_name_regex: "(google ads|meta|facebook|tiktok|linkedin).*(purchase|transaction)"
    triggers:
      firing_trigger_name_regex: "(purchase|transaction)"
      require_equivalent_firing_triggers: true
```

## Run an audit

```bash
python audit.py \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --policy policies/advertising.yaml
```

Example output:

```text
GTM POLICY AUDIT
========================================================================
advertising: 18 tag(s) inspected
advertising_purchase: 4 tag(s) inspected

ERROR CONSENT_TYPES             LinkedIn - Purchase [fixable]
      policy=advertising | consent types differ from policy (missing=ad_user_data)
WARN  TRIGGER_DRIFT             advertising_purchase
      policy=advertising_purchase | matched tags use 2 distinct firing-trigger configurations
WARN  DUPLICATE_TRIGGER         Lead Submit / Lead Submit v2
      policy=global | behaviorally equivalent trigger definitions detected
```

## Trigger drift detection

GTM tags reference firing and blocking triggers by ID. The auditor retrieves the full trigger objects and builds a normalized signature from behaviorally relevant fields such as:

- trigger event type
- regular filters
- custom-event filters
- auto-event filters
- wait-for-tags settings
- validation settings
- timeout/timer/scroll parameters when present

Trigger names and IDs are intentionally excluded from the signature. That means two differently named triggers with the same logic are recognized as equivalent, while a subtle filter difference is flagged as drift.

This is useful for questions such as:

> Do all advertising purchase tags actually fire on the same purchase logic, or has one platform quietly drifted to a slightly different trigger?

## Safe remediation

The auditor can build a remediation plan for tag-level violations:

```bash
python audit.py \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --policy policies/advertising.yaml \
  --fix
```

That is still a dry-run. Apply safe fixes with:

```bash
python audit.py \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --policy policies/advertising.yaml \
  --fix \
  --execute
```

Automatic remediation is intentionally limited to safer tag-level changes:

- consent settings
- pause state
- firing option
- adding one uniquely resolved required blocking trigger

The tool **does not automatically rewrite trigger conditions** when drift is found. Trigger logic is surfaced for review rather than silently changed.

## CI / machine-readable audits

Emit JSON:

```bash
python audit.py ... --json
```

Control exit behavior:

```text
--fail-on error   # default
--fail-on warn
--fail-on never
```

This makes it possible to run a GTM governance audit as part of a scheduled job or CI workflow.

## Repository structure

```text
.
├── gtm_manager.py
├── audit.py
├── policies/
│   └── advertising.yaml
├── examples/
├── tests/
│   ├── test_gtm_manager.py
│   └── test_audit.py
├── .github/workflows/test.yml
├── requirements.txt
└── README.md
```

## Tests

```bash
python -m unittest discover -s tests -v
```

The core filtering, mutation, policy, and trigger-signature logic is testable without live GTM credentials.

## Safety model

- Reads and audits do not modify GTM.
- Bulk updates are dry-run unless `--execute` is supplied.
- Audit remediation requires both `--fix` and `--execute`.
- Writes are capped at 50 tags by default.
- Tag updates use GTM fingerprints for optimistic concurrency protection.
- OAuth client secrets and token caches are excluded from Git.
- The tools modify the selected **workspace only**; they do not automatically create a container version or publish it.

Review changes in GTM Preview and the workspace UI before publishing production changes.

## Important

This project manages GTM configuration. It does not replace a consent management platform or determine legal consent requirements. `consentSettings` controls additional consent checks configured for GTM tags; your Consent Mode and CMP implementation should be validated separately.
