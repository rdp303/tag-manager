import unittest

from gtm_manager import TagFilter, TagMutation, build_consent_settings


class TagManagerTests(unittest.TestCase):
    def setUp(self):
        self.tag = {
            "tagId": "42",
            "name": "Marketing - Meta Pixel",
            "type": "html",
            "paused": False,
            "parentFolderId": "9",
            "consentSettings": {"consentStatus": "notSet"},
        }

    def test_filter_matches_multiple_criteria(self):
        filt = TagFilter(
            name_regex="meta|facebook",
            tag_type="html",
            paused=False,
            folder_id="9",
        )
        self.assertTrue(filt.matches(self.tag))

    def test_filter_requires_all_criteria(self):
        filt = TagFilter(name_contains="marketing", tag_type="gaawe")
        self.assertFalse(filt.matches(self.tag))

    def test_needed_consent_shape(self):
        settings = build_consent_settings(
            "needed", ["ad_storage", "analytics_storage", "ad_storage"]
        )
        self.assertEqual(settings["consentStatus"], "needed")
        values = [item["value"] for item in settings["consentType"]["list"]]
        self.assertEqual(values, ["ad_storage", "analytics_storage"])

    def test_needed_consent_requires_type(self):
        with self.assertRaises(ValueError):
            build_consent_settings("needed", [])

    def test_mutation_does_not_modify_original(self):
        mutation = TagMutation(
            consent_status="needed", consent_types=("ad_storage",), paused=True
        )
        updated, changes = mutation.apply(self.tag)
        self.assertFalse(self.tag["paused"])
        self.assertTrue(updated["paused"])
        self.assertEqual(updated["consentSettings"]["consentStatus"], "needed")
        self.assertEqual(len(changes), 2)


if __name__ == "__main__":
    unittest.main()
