# Programmatic Google Tag Manager

A Python CLI for safely managing Google Tag Manager tags at scale. Filter tags by name, type, pause state, consent status, folder, or tag ID, then bulk-update matching tags without clicking through the GTM UI one-by-one.

The main use case is **programmatic consent management**, but the same tool can also pause/unpause tags, change firing options, and update notes.

## Why this exists

Large GTM containers often contain dozens or hundreds of tags. Changes such as adding a new consent requirement to every advertising tag are repetitive and easy to apply inconsistently by hand.

This tool turns that work into a reviewable workflow:

1. Select a GTM account/container/workspace.
2. Filter tags using multiple criteria.
3. Preview the exact tags and fields that would change.
4. Re-run with `--execute` only after validating the dry-run.

## Features

- OAuth 2.0 authentication against the official Google Tag Manager API v2
- Case-insensitive name substring and regex filters
- Filters for tag type, pause state, consent status, parent folder, and exact tag IDs
- Bulk consent updates using GTM's native `consentSettings`
- Bulk pause/unpause
- Bulk firing-option updates
- Replace or append tag notes
- Pagination across large workspaces
- **Dry-run by default**
- `--max-updates` safety cap for write operations
- Fingerprint-based updates for optimistic concurrency protection
- Unit tests for filtering and mutation logic

## Supported consent settings

The GTM API exposes three manual consent states:

- `notSet` — no manual additional-consent setting
- `notNeeded` — no additional consent is required
- `needed` — require one or more consent types before the tag fires

When using `needed`, pass one or more consent types such as `ad_storage`, `analytics_storage`, `ad_user_data`, or `ad_personalization` according to your implementation.

## Setup

### 1. Create Google Cloud credentials

Enable the **Google Tag Manager API** in a Google Cloud project and create an OAuth 2.0 **Desktop app** credential. Download the JSON credential and save it locally as:

```text
client_secrets.json
```

Do not commit this file. It is ignored by `.gitignore`.

The tool requests this GTM OAuth scope:

```text
https://www.googleapis.com/auth/tagmanager.edit.containers
```

The Google account completing OAuth must also have sufficient access to the GTM account/container you want to edit.

### 2. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Find your IDs

The CLI expects GTM's numeric API IDs, not the public `GTM-XXXXXXX` container ID:

```text
accounts/{account_id}/containers/{container_id}/workspaces/{workspace_id}
```

You can see these IDs in GTM API responses/URLs. The first authenticated run opens a browser for Google OAuth and stores the refreshable token locally in `token.json`.

## Examples

### Inspect tags before touching anything

```bash
python gtm_manager.py list \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12
```

### Find all Custom HTML advertising tags

```bash
python gtm_manager.py list \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --type html \
  --name-regex 'meta|facebook|tiktok|linkedin'
```

### Require ad consent for every matching advertising tag

This is a **dry-run**. It prints the exact planned changes but writes nothing:

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

After reviewing the output, execute the same change:

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

### Only change tags that currently have no manual consent setting

```bash
python gtm_manager.py update \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --consent-status notSet \
  --name-contains pixel \
  --set-consent-status needed \
  --consent-type ad_storage
```

### Pause every tag in a folder

```bash
python gtm_manager.py update \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --folder-id 55 \
  --set-paused true
```

### Update only specific tag IDs

```bash
python gtm_manager.py update \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --tag-id 14 \
  --tag-id 18 \
  --append-note 'Consent reviewed programmatically' \
  --execute
```

## Safety model

The script intentionally makes bulk writes harder than reads:

- `update` is a dry-run unless `--execute` is supplied.
- Writes are capped at 50 tags by default. Change the cap with `--max-updates`.
- Each update sends the tag's current fingerprint so GTM can reject a write if the entity changed after it was read.
- OAuth client secrets and cached tokens are ignored by Git.
- The tool updates the selected **workspace**; it does not automatically create a container version or publish it.

That last point is intentional: review the workspace changes in GTM before versioning/publishing.

## CLI reference

```bash
python gtm_manager.py --help
python gtm_manager.py list --help
python gtm_manager.py update --help
```

Filters can be combined. A tag must satisfy **all** supplied filters.

### Filter options

```text
--name-contains TEXT
--name-regex REGEX
--type TYPE
--paused true|false
--consent-status needed|notNeeded|notSet
--folder-id ID
--tag-id ID                 # repeatable
```

### Update options

```text
--set-consent-status needed|notNeeded|notSet
--consent-type TYPE         # repeatable; used with needed
--set-paused true|false
--set-firing-option unlimited|oncePerEvent|oncePerLoad|tagFiringOptionUnspecified
--set-note TEXT
--append-note TEXT
--max-updates N
--execute
```

## Tests

The filtering/mutation logic is separated from the Google API calls so it can be tested without GTM credentials:

```bash
python -m unittest discover -s tests -v
```

## Architecture

```text
OAuth 2.0
   |
   v
Google Tag Manager API v2
   |
   +--> list workspace tags (paginated)
   |
   +--> apply AND-based filters
   |
   +--> build mutation plan
   |
   +--> dry-run output
   |
   +--> tags.update(path, body, fingerprint)  [only with --execute]
```

## Important

This tool changes GTM configuration, not a website's consent state directly. `consentSettings` controls the additional consent checks configured for individual GTM tags. Validate your Consent Mode/CMP implementation and preview the GTM workspace before publishing production changes.
