"""同意范围与冷静期：最新同意为准，冷静期内禁止联络。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from coordination import ConsentError

from tests.helpers import T0, World


class ConsentBookTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.consent = self.world.consent
        self.alias = "D-testdonor01"

    def test_unknown_donor_is_not_permitted(self):
        ok, why = self.consent.permits(self.alias, "recontact")
        self.assertFalse(ok)
        self.assertEqual(why, "consent_missing")

    def test_latest_scope_wins(self):
        self.consent.update_scope(self.alias, {"search", "recontact"}, actor="registry")
        self.assertEqual(self.consent.permits(self.alias, "recontact"), (True, None))
        self.consent.update_scope(self.alias, {"search"}, actor="registry")
        ok, why = self.consent.permits(self.alias, "recontact")
        self.assertFalse(ok)
        self.assertEqual(why, "scope_lacks:recontact")

    def test_unknown_action_rejected(self):
        with self.assertRaises(ConsentError):
            self.consent.update_scope(self.alias, {"search", "teleport"}, actor="registry")

    def test_cooling_blocks_until_expiry(self):
        self.consent.update_scope(self.alias, {"search", "recontact"}, actor="registry")
        self.consent.set_cooling(self.alias, T0 + timedelta(hours=12), "contact_interval")
        ok, why = self.consent.permits(self.alias, "recontact")
        self.assertFalse(ok)
        self.assertEqual(why, "cooling_off")
        self.world.clock.advance(timedelta(hours=12))
        self.assertEqual(self.consent.permits(self.alias, "recontact"), (True, None))

    def test_cooling_cannot_be_shortened(self):
        self.consent.update_scope(self.alias, {"search", "recontact"}, actor="registry")
        later = T0 + timedelta(days=180)
        self.consent.set_cooling(self.alias, later, "withdrawal")
        self.consent.set_cooling(self.alias, T0 + timedelta(hours=1), "contact_interval")
        record = self.consent.record(self.alias)
        self.assertEqual(record.cooling_until, later)
        self.assertEqual(record.cooling_reason, "withdrawal")

    def test_granted_at_tracks_current_scope(self):
        self.consent.update_scope(self.alias, {"search", "exam"}, actor="registry")
        first = self.consent.granted_at(self.alias, "exam")
        self.assertEqual(first, T0)
        self.world.clock.advance(timedelta(days=1))
        # 保留的项授权时间不变；移除后重新加入的项重新计时
        self.consent.update_scope(self.alias, {"search", "exam", "collection"}, actor="registry")
        self.consent.update_scope(self.alias, {"search", "collection"}, actor="registry")
        self.assertIsNone(self.consent.granted_at(self.alias, "exam"))
        self.world.clock.advance(timedelta(days=1))
        self.consent.update_scope(self.alias, {"search", "exam", "collection"}, actor="registry")
        self.assertEqual(self.consent.granted_at(self.alias, "exam"), T0 + timedelta(days=2))
        self.assertEqual(self.consent.granted_at(self.alias, "collection"), T0 + timedelta(days=1))


if __name__ == "__main__":
    unittest.main()
