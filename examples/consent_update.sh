#!/usr/bin/env bash
# Dry-run: require analytics_storage consent for every Custom HTML tag
# whose name contains "analytics".
python gtm_manager.py update \
  --account-id 123456 \
  --container-id 987654 \
  --workspace-id 12 \
  --name-contains analytics \
  --type html \
  --set-consent-status needed \
  --consent-type analytics_storage

# After reviewing the planned changes, add --execute to write them.
