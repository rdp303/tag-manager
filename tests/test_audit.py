import unittest

from audit import audit_workspace, build_tag_fixes, consent_types, trigger_signature


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.purchase_trigger = {
            "triggerId": "1",
            "name": "Purchase Event",
            "type": "customEvent",
            "customEventFilter": [
                {
                    "type": "equals",
                    "parameter": [
                        {"type": "template", "key": "arg0", "value": "{{_event}}"},
                        {"type": "template", "key": "arg1", "value": "purchase"},
                    ],
                }
            ],
        }
        self.purchase_trigger_copy = {
            **self.purchase_trigger,
            "triggerId": "2",
            "name": "Purchase Event Copy",
        }
        self.consent_block = {
            "triggerId": "9",
            "name": "Consent - Advertising",
            "type": "customEvent",
            "customEventFilter": [],
        }
        self.tag = {
            "tagId": "42",
            "name": "Meta - Purchase",
            "type": "html",
            "paused": False,
            "tagFiringOption": "unlimited",
            "firingTriggerId": ["1"],
            "blockingTriggerId": [],
            "consentSettings": {"consentStatus": "notSet"},
        }
        self.config = {
            "policies": {
                "advertising": {
                    "match": {"tag_name_regex": "meta|google ads"},
                    "require": {
                        "consent_status": "needed",
                        "consent_types": ["ad_storage", "ad_user_data"],
                        "paused": False,
                        "firing_option": "oncePerEvent",
                    },
                    "triggers": {
                        "required_blocking_trigger": "Consent - Advertising",
                        "allowed_types": ["customEvent"],
                    },
                }
            },
            "global_checks": {"duplicate_triggers": False},
        }

    def test_equivalent_trigger_signature_ignores_name_and_id(self):
        self.assertEqual(
            trigger_signature(self.purchase_trigger),
            trigger_signature(self.purchase_trigger_copy),
        )

    def test_audit_flags_consent_firing_option_and_blocking_trigger(self):
        findings, selections = audit_workspace(
            [self.tag],
            [self.purchase_trigger, self.consent_block],
            self.config,
        )
        codes = {finding.code for finding in findings}
        self.assertEqual(len(selections["advertising"]), 1)
        self.assertIn("CONSENT_STATUS", codes)
        self.assertIn("CONSENT_TYPES", codes)
        self.assertIn("FIRING_OPTION", codes)
        self.assertIn("BLOCKING_TRIGGER", codes)

    def test_fix_plan_updates_safe_tag_fields(self):
        plan = build_tag_fixes(
            [self.tag],
            [self.purchase_trigger, self.consent_block],
            self.config,
        )
        self.assertEqual(len(plan), 1)
        _before, updated, changes = plan[0]
        self.assertEqual(updated["tagFiringOption"], "oncePerEvent")
        self.assertEqual(consent_types(updated), {"ad_storage", "ad_user_data"})
        self.assertEqual(updated["blockingTriggerId"], ["9"])
        self.assertTrue(changes)

    def test_trigger_drift_is_flagged(self):
        second_trigger = {
            "triggerId": "3",
            "name": "Different Purchase Event",
            "type": "pageview",
            "filter": [],
        }
        second_tag = {
            **self.tag,
            "tagId": "43",
            "name": "Google Ads - Purchase",
            "firingTriggerId": ["3"],
            "consentSettings": {
                "consentStatus": "needed",
                "consentType": {
                    "type": "list",
                    "list": [
                        {"type": "template", "value": "ad_storage"},
                        {"type": "template", "value": "ad_user_data"},
                    ],
                },
            },
            "tagFiringOption": "oncePerEvent",
            "blockingTriggerId": ["9"],
        }
        cfg = {
            "policies": {
                "purchase": {
                    "match": {"tag_name_regex": "purchase"},
                    "triggers": {"require_equivalent_firing_triggers": True},
                }
            },
            "global_checks": {"duplicate_triggers": False},
        }
        findings, _ = audit_workspace(
            [self.tag, second_tag],
            [self.purchase_trigger, second_trigger, self.consent_block],
            cfg,
        )
        self.assertIn("TRIGGER_DRIFT", {f.code for f in findings})


if __name__ == "__main__":
    unittest.main()
