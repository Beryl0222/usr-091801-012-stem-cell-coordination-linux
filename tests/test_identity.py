"""身份保险库：别名不可反查、双人批准、自动到期。"""

from __future__ import annotations

import unittest
from datetime import timedelta

from coordination import IdentityError, UnsealDenied

from tests.helpers import World


class IdentityVaultTest(unittest.TestCase):
    def setUp(self):
        self.world = World()
        self.vault = self.world.vault
        self.donor_id = self.vault.enroll("donor", "志愿者乙", "13900000001")
        self.alias = self.vault.alias_for(self.donor_id)

    def test_alias_is_opaque_and_stable(self):
        self.assertTrue(self.alias.startswith("D-"))
        self.assertNotIn("志愿者乙", self.alias)
        self.assertNotIn("13900000001", self.alias)
        self.assertEqual(self.alias, self.vault.alias_for(self.donor_id))

    def test_alias_prefix_reflects_domain_only(self):
        patient_id = self.vault.enroll("patient", "患者甲", "contact")
        self.assertTrue(self.vault.alias_for(patient_id).startswith("P-"))

    def test_enroll_rejects_unknown_domain(self):
        with self.assertRaises(IdentityError):
            self.vault.enroll("visitor", "路人", "x")

    def test_resolve_requires_dual_approval(self):
        request_id = self.vault.request_unseal("coord-1", self.alias, "运输途中身份核对")
        self.assertIsNone(self.vault.approve_unseal(request_id, "sec-a"))
        grant = self.vault.approve_unseal(request_id, "sec-b")
        self.assertIsNotNone(grant)
        record = self.vault.resolve(grant.token)
        self.assertEqual(record.real_name, "志愿者乙")
        self.assertEqual(record.contact, "13900000001")

    def test_same_approver_twice_does_not_count(self):
        request_id = self.vault.request_unseal("coord-1", self.alias, "重复批准")
        self.vault.approve_unseal(request_id, "sec-a")
        self.assertIsNone(self.vault.approve_unseal(request_id, "sec-a"))

    def test_requester_cannot_approve_own_request(self):
        request_id = self.vault.request_unseal("sec-a", self.alias, "自我批准")
        with self.assertRaises(UnsealDenied):
            self.vault.approve_unseal(request_id, "sec-a")

    def test_outsider_cannot_approve(self):
        request_id = self.vault.request_unseal("coord-1", self.alias, "越权批准")
        with self.assertRaises(UnsealDenied):
            self.vault.approve_unseal(request_id, "coord-9")

    def test_grant_auto_expires(self):
        request_id = self.vault.request_unseal("coord-1", self.alias, "到期验证")
        self.vault.approve_unseal(request_id, "sec-a")
        grant = self.vault.approve_unseal(request_id, "sec-b")
        self.world.clock.advance(timedelta(minutes=21))
        with self.assertRaises(UnsealDenied):
            self.vault.resolve(grant.token)
        denied = self.world.audit.find("identity.unseal.denied")
        self.assertEqual(len(denied), 1)
        self.assertEqual(denied[0].basis["reason"], "grant_expired")

    def test_unknown_token_and_alias_rejected(self):
        with self.assertRaises(UnsealDenied):
            self.vault.resolve("no-such-token")
        with self.assertRaises(IdentityError):
            self.vault.request_unseal("coord-1", "D-ffffffffffff", "不存在")

    def test_unseal_trail_is_audited(self):
        request_id = self.vault.request_unseal("coord-1", self.alias, "审计留痕")
        self.vault.approve_unseal(request_id, "sec-a")
        grant = self.vault.approve_unseal(request_id, "sec-b")
        self.vault.resolve(grant.token)
        actions = [event.action for event in self.world.audit.find()]
        self.assertEqual(
            [a for a in actions if a.startswith("identity.")],
            [
                "identity.unseal.requested",
                "identity.unseal.approved",
                "identity.unseal.approved",
                "identity.unseal.granted",
                "identity.unseal.resolved",
            ],
        )
        granted = self.world.audit.find("identity.unseal.granted")[0]
        self.assertEqual(granted.basis["approvers"], ["sec-a", "sec-b"])
        self.assertIn("expires_at", granted.basis)


if __name__ == "__main__":
    unittest.main()
