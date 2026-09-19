"""聚合存储与快照。

业务数据与身份数据保持分离的顶层键（真实部署中 identity_vault 落在
独立加密存储，业务库永远拿不到真实身份）。快照只用于整体校验与联调重置。
"""

import json

from .audit import AuditLog
from .clock import iso
from .comms import ConsentRegistry, ContactLedger
from .identity import BreakGlassManager, IdentityVault
from .workflow import CommitmentLedger


class Store:
    def __init__(self):
        self.audit = AuditLog()
        self.vault = IdentityVault()

        def _grant_expired(req):
            self.audit.record(
                None, "unseal_expired", actor="system", case_id=req["case_id"],
                details={"grant_id": req["grant_id"], "expires_at": req["expires_at"]},
                at=req["expires_at"],
            )

        self.breakglass = BreakGlassManager(on_expire=_grant_expired)
        self.consents = ConsentRegistry()
        self.contacts = ContactLedger()
        self.commitments = CommitmentLedger()
        # donor_id -> 业务画像（不含任何真实身份字段）
        self.donors = {}
        # patient_id -> {patient_id}
        self.patients = {}
        # case_id -> 病例聚合
        self.cases = {}
        self._case_seq = 0

    def new_case_id(self):
        self._case_seq += 1
        return f"CASE-{self._case_seq:04d}"

    # -- 病例对象上的别名映射 ------------------------------------------------

    def donor_alias_in_case(self, case_id, donor_id):
        case = self.cases[case_id]
        for ref in case["donor_refs"]:
            if ref["donor_id"] == donor_id:
                return ref["alias"]
        raise KeyError(f"候选 {donor_id} 不在病例 {case_id}")

    def resolve_alias(self, case_id, alias):
        case = self.cases[case_id]
        for ref in case["donor_refs"]:
            if ref["alias"] == alias:
                return ref["donor_id"]
        raise KeyError(f"别名 {alias} 不属于病例 {case_id}")

    # -- 整体快照（分键分离） ------------------------------------------------

    def snapshot(self):
        return {
            "business": {
                "donors": {k: dict(v) for k, v in self.donors.items()},
                "patients": {k: dict(v) for k, v in self.patients.items()},
                "cases": _jsonable(self.cases),
                "case_seq": self._case_seq,
            },
            "commitments": self.commitments.snapshot(),
            "contacts": self.contacts.snapshot(),
            "consents": self.consents.snapshot(),
            "identity_vault": self.vault.snapshot(),
            "breakglass": self.breakglass.snapshot(),
            "audit": self.audit.export(),
        }

    def save(self, path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(self.snapshot(), fh, ensure_ascii=False, indent=2, sort_keys=True)


def _jsonable(value):
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return iso(value)
    return value
