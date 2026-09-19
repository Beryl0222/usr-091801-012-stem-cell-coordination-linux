"""用例编排：把身份、匹配、联络、工作流组装成协调员可用的服务。

所有面向协调员的视图只含别名；真实身份只在 IdentityVault 中，
且必须持有效紧急解封许可才能读取。医院/运输方按病例指派关系上报里程碑。
"""

from .clock import Clock, as_utc, iso
from .comms import (
    ContactPolicy,
    default_renderer,
)
from .errors import Conflict, DomainError, NotFound, PermissionDenied
from .identity import (
    ROLE_COLLECTION_HOSPITAL,
    ROLE_COORDINATOR,
    ROLE_COURIER,
    ROLE_IDENTITY_OFFICER,
    ROLE_TRANSPLANT_HOSPITAL,
    ROLE_AUDITOR,
    donor_alias,
    patient_alias,
)
from .matching import ALGORITHM_VERSION, rank_candidates
from .store import Store
from .workflow import (
    CaseWorkflow,
)


class CoordinationService:
    def __init__(self, secret="dev-secret-change-me", clock=None):
        self.secret = secret
        self.clock = clock or Clock()
        self.store = Store()
        self.policy = ContactPolicy(
            self.clock, self.store.consents, self.store.contacts, self.store.audit, default_renderer
        )
        self.workflow = CaseWorkflow(self.clock, self.store.audit, self.store.commitments)

    def advance(self, **kwargs):
        return self.clock.advance(**kwargs)

    # ============================== 身份录入 ==============================

    def enroll_donor(self, actor, donor_id, real_name, id_number, phone, profile, scopes):
        actor.require(ROLE_IDENTITY_OFFICER)
        if donor_id in self.store.donors:
            raise Conflict("donor_exists", f"志愿者已登记: {donor_id}")
        self.store.vault.put(donor_id, "donor", real_name, id_number, phone)
        business = {
            "donor_id": donor_id,
            "age": profile["age"],
            "typing": profile["typing"],
            "typing_complete": profile.get("typing_complete", True),
            "medical_deferral": profile.get("medical_deferral", False),
            "availability_confidence": profile.get("availability_confidence", 0),
            "registry_ref": profile.get("registry_ref"),
        }
        self.store.donors[donor_id] = business
        self.store.consents.register(donor_id, scopes, granted_at=iso(self.clock.now()))
        if profile.get("blackout_windows"):
            for start, end in profile["blackout_windows"]:
                self.store.commitments.add_blackout(donor_id, start, end)
        self.store.audit.record(self.clock, "donor_enrolled", actor=actor.id, subject=donor_id,
                                details={"age": business["age"], "scopes": sorted(scopes)})
        return {"donor_id": donor_id}

    def enroll_patient(self, actor, patient_id, real_name, id_number, phone, typing):
        actor.require(ROLE_IDENTITY_OFFICER)
        if patient_id in self.store.patients:
            raise Conflict("patient_exists", f"患者已登记: {patient_id}")
        self.store.vault.put(patient_id, "patient", real_name, id_number, phone)
        self.store.patients[patient_id] = {"patient_id": patient_id, "typing": typing}
        self.store.audit.record(self.clock, "patient_enrolled", actor=actor.id, subject=patient_id)
        return {"patient_id": patient_id}

    def update_donor_profile(self, actor, donor_id, changes):
        """登记资料更新（如暂缓捐献标记）。仅身份官可改，留痕。"""
        actor.require(ROLE_IDENTITY_OFFICER)
        donor = self._donor(donor_id)
        allowed = {"age", "typing", "typing_complete", "medical_deferral", "availability_confidence"}
        applied = {}
        for key, value in changes.items():
            if key not in allowed:
                raise DomainError("bad_field", f"不可更新字段: {key}")
            donor[key] = value
            applied[key] = value
        self.store.audit.record(self.clock, "donor_profile_updated", actor=actor.id,
                                subject=donor_id, details=applied)
        return applied

    def grant_consent(self, actor, donor_id, scopes):
        """身份官代为登记志愿者新增的同意范围。"""
        actor.require(ROLE_IDENTITY_OFFICER)
        added = self.store.consents.grant(self.clock, donor_id, scopes)
        self.store.audit.record(self.clock, "consent_granted", actor=actor.id, subject=donor_id,
                                details={"scopes": added})
        return {"added": added}

    def withdraw_consent(self, actor, donor_id, scopes):
        """登记志愿者撤回同意：立即对未来联络生效。"""
        actor.require(ROLE_IDENTITY_OFFICER)
        removed = self.store.consents.withdraw(self.clock, donor_id, scopes)
        self.store.audit.record(self.clock, "consent_withdrawn", actor=actor.id, subject=donor_id,
                                details={"scopes": removed})
        return {"removed": removed}

    # ============================== 病例与检索 ==============================

    def open_case(self, actor, patient_id, clinical_deadline,
                  collection_hospital_id, transplant_hospital_id, algorithm_version=None):
        actor.require(ROLE_COORDINATOR)
        patient = self.store.patients.get(patient_id)
        if patient is None:
            raise NotFound("患者", patient_id)
        case_id = self.store.new_case_id()
        case = {
            "case_id": case_id,
            "patient_id": patient_id,
            "patient_alias": patient_alias(self.secret, patient_id),
            "patient_typing": patient["typing"],
            "clinical_deadline": iso(as_utc(clinical_deadline)),
            "collection_hospital_id": collection_hospital_id,
            "transplant_hospital_id": transplant_hospital_id,
            "courier_id": None,
            "algorithm_version": algorithm_version or ALGORITHM_VERSION,
            "opened_at": iso(self.clock.now()),
            "donor_refs": [],
            "last_ranking": None,
        }
        self.store.cases[case_id] = case
        self.store.audit.record(self.clock, "case_opened", actor=actor.id, case_id=case_id,
                                subject=patient_id,
                                details={"clinical_deadline": case["clinical_deadline"],
                                         "algorithm_version": case["algorithm_version"]})
        return {"case_id": case_id, "patient_alias": case["patient_alias"]}

    def search_candidates(self, actor, case_id, donor_ids=None, algorithm_version=None):
        """患者侧只有分型与临床时限；排序保留算法版本、分级与排除理由。"""
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        if case["last_ranking"] is not None:
            raise Conflict("case_already_ranked", "病例已完成排序，扩大检索请使用重排入口")
        pool = [self._donor(d) for d in donor_ids] if donor_ids else list(self.store.donors.values())
        version = algorithm_version or case["algorithm_version"]
        ranking = rank_candidates(self.clock, case, pool, self.store.commitments, version,
                                  consents=self.store.consents)
        case["last_ranking"] = ranking
        case["algorithm_version"] = version
        # 为所有进入本次排序的志愿者（含被排除者）生成病例作用域别名
        for row in ranking["candidates"] + ranking["excluded"]:
            case["donor_refs"].append(
                {"donor_id": row["donor_id"], "alias": donor_alias(self.secret, case_id, row["donor_id"]),
                 "included": not row.get("reasons")}
            )
        self.workflow.seed_candidates(case, ranking)
        self.store.audit.record(
            self.clock, "candidates_ranked", actor=actor.id, case_id=case_id,
            details={"algorithm_version": version,
                     "ranked": [r["donor_id"] for r in ranking["candidates"]],
                     "excluded": {r["donor_id"]: r["reasons"] for r in ranking["excluded"]},
                     "basis": ranking["basis"]},
        )
        return self.ranking_view(actor, case_id)

    def rerank(self, actor, case_id, donor_ids=None, algorithm_version=None):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        pool = [self._donor(d) for d in donor_ids] if donor_ids else list(self.store.donors.values())
        ranking = rank_candidates(self.clock, case, pool, self.store.commitments,
                                  algorithm_version or ALGORITHM_VERSION,
                                  consents=self.store.consents)
        self.workflow.rerank(case, actor, ranking)
        for row in ranking["candidates"] + ranking["excluded"]:
            if not any(ref["donor_id"] == row["donor_id"] for ref in case["donor_refs"]):
                case["donor_refs"].append(
                    {"donor_id": row["donor_id"],
                     "alias": donor_alias(self.secret, case_id, row["donor_id"]),
                     "included": not row.get("reasons")})
        return self.ranking_view(actor, case_id)

    # ============================== 联络与决定 ==============================

    def notify_initial_match(self, actor, case_id, alias, idempotency_key):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        self._require_active(case, donor_id)
        dispatch = self.policy.send(
            actor, donor_id, case_id, "initial_match_notice", idempotency_key,
            context={"donor_alias": alias},
        )
        action = self.workflow.mark_contacted(case, actor, donor_id, idempotency_key)
        return {"dispatch": self._dispatch_view(dispatch), "candidate": self._candidate_view(case, donor_id),
                "deduped": dispatch["deduped"] or action["deduped"]}

    def request_confirmation_notice(self, actor, case_id, alias, idempotency_key):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        self._require_active(case, donor_id)
        dispatch = self.policy.send(
            actor, donor_id, case_id, "confirm_request", idempotency_key,
            context={"donor_alias": alias},
        )
        action = self.workflow.request_confirmation(case, actor, donor_id, idempotency_key)
        return {"dispatch": self._dispatch_view(dispatch), "candidate": self._candidate_view(case, donor_id),
                "deduped": dispatch["deduped"] or action["deduped"]}

    def record_donor_decision(self, actor, case_id, alias, confirmed, collection_window=None,
                              idempotency_key=None, decline_reason="志愿者婉拒"):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        self._require_active(case, donor_id)
        key = idempotency_key or f"decision:{case_id}:{donor_id}"
        if not confirmed:
            result = self.workflow.withdraw(case, actor, donor_id, decline_reason, key)
            self.store.contacts.clear_decision_pending(donor_id, case_id)
            return self._withdraw_view(case, result)
        if not collection_window:
            raise DomainError("window_required", "确认捐献必须给出采集窗口")
        window = {"start": as_utc(collection_window["start"]), "end": as_utc(collection_window["end"])}
        # 跨病例占用互斥：错误信息不含其他病例的任何内容
        if self.store.commitments.is_in_post_donation_cooldown(donor_id, window["start"]):
            raise Conflict("donor_in_cooldown", "志愿者处于捐献后恢复期，不能安排采集")
        if self.store.commitments.has_overlapping_commitment(donor_id, case_id, window["start"]):
            raise Conflict("committed_elsewhere", "志愿者在该时段已有不可并行的捐献安排")
        result = self.workflow.confirm_donation(case, actor, donor_id, window, key)
        self.store.contacts.clear_decision_pending(donor_id, case_id)
        return {"candidate": self._candidate_view(case, donor_id), "window": result["window"],
                "deduped": result["deduped"]}

    def withdraw_donor(self, actor, case_id, alias, reason, idempotency_key=None):
        """志愿者在流程中主动撤回（协调员代为登记）。"""
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        key = idempotency_key or f"withdraw:{case_id}:{donor_id}"
        result = self.workflow.withdraw(case, actor, donor_id, reason, key)
        self.store.contacts.clear_decision_pending(donor_id, case_id)
        return self._withdraw_view(case, result)

    def schedule_exam(self, actor, case_id, alias, scheduled_at, idempotency_key):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        self._require_active(case, donor_id)
        notice_key = f"{idempotency_key}:notice"
        dispatch = self.policy.send(actor, donor_id, case_id, "exam_schedule", notice_key,
                                    context={"donor_alias": alias})
        action = self.workflow.schedule_exam(case, actor, donor_id, scheduled_at, idempotency_key)
        return {"dispatch": self._dispatch_view(dispatch), "candidate": self._candidate_view(case, donor_id),
                "scheduled_at": action["scheduled_at"], "deduped": action["deduped"]}

    def schedule_collection(self, actor, case_id, alias, scheduled_at, idempotency_key):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        self._require_active(case, donor_id)
        notice_key = f"{idempotency_key}:notice"
        dispatch = self.policy.send(actor, donor_id, case_id, "collection_schedule", notice_key,
                                    context={"donor_alias": alias})
        action = self.workflow.schedule_collection(case, actor, donor_id, scheduled_at, idempotency_key)
        return {"dispatch": self._dispatch_view(dispatch), "candidate": self._candidate_view(case, donor_id),
                "scheduled_at": action["scheduled_at"], "deduped": action["deduped"]}

    def assign_courier(self, actor, case_id, courier_id):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        case["courier_id"] = courier_id
        self.store.audit.record(self.clock, "courier_assigned", actor=actor.id, case_id=case_id,
                                details={"courier_id": courier_id})
        return {"courier_id": courier_id}

    # ============================== 体检 / 采集 / 运输 ==============================

    def report_exam(self, actor, case_id, alias, passed, findings=None, idempotency_key=None):
        self._require_party(actor, case_id, ROLE_COLLECTION_HOSPITAL, "collection_hospital_id")
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        self._require_active(case, donor_id)
        result = self.workflow.report_exam(case, actor, donor_id, passed, findings, idempotency_key)
        return self._hospital_result_view(case, result)

    def resolve_reexam(self, actor, case_id, alias, passed, findings=None, idempotency_key=None):
        self._require_party(actor, case_id, ROLE_COLLECTION_HOSPITAL, "collection_hospital_id")
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        result = self.workflow.resolve_reexam(case, actor, donor_id, passed, findings, idempotency_key)
        return self._hospital_result_view(case, result)

    def record_collection(self, actor, case_id, alias=None, collected_at=None, idempotency_key=None):
        self._require_party(actor, case_id, ROLE_COLLECTION_HOSPITAL, "collection_hospital_id")
        case = self._case(case_id)
        donor_id = self._resolve_active_alias(case, alias)
        result = self.workflow.record_collection(case, actor, donor_id, collected_at, idempotency_key)
        return {"candidate": self._candidate_view(case, donor_id),
                "product_window": result["product_window"], "deduped": result["deduped"]}

    def record_pickup(self, actor, case_id, at=None, idempotency_key=None):
        self._require_party(actor, case_id, ROLE_COURIER, "courier_id")
        case = self._case(case_id)
        donor_id = case["active_donor_id"]
        result = self.workflow.record_pickup(case, actor, donor_id, at, idempotency_key)
        return {"candidate": self._candidate_view(case, donor_id), "deduped": result["deduped"]}

    def report_flight_delay(self, actor, case_id, flight_no, delay_minutes, projected_arrival_at,
                            idempotency_key=None):
        self._require_party(actor, case_id, ROLE_COURIER, "courier_id")
        case = self._case(case_id)
        result = self.workflow.report_flight_delay(
            case, actor, flight_no, delay_minutes, projected_arrival_at, idempotency_key
        )
        return {"delay": result["delay"], "contingency": self._contingency_view(result["contingency"]),
                "deduped": result["deduped"]}

    def record_delivery(self, actor, case_id, at=None, idempotency_key=None):
        if actor.has(ROLE_COURIER):
            self._require_party(actor, case_id, ROLE_COURIER, "courier_id")
        else:
            self._require_party(actor, case_id, ROLE_TRANSPLANT_HOSPITAL, "transplant_hospital_id")
        case = self._case(case_id)
        result = self.workflow.record_delivery(case, actor, at, idempotency_key)
        donor_id = case["active_donor_id"]
        return {"candidate": self._candidate_view(case, donor_id), "deduped": result["deduped"]}

    def record_infusion(self, actor, case_id, at=None, idempotency_key=None):
        self._require_party(actor, case_id, ROLE_TRANSPLANT_HOSPITAL, "transplant_hospital_id")
        case = self._case(case_id)
        result = self.workflow.record_infusion(case, actor, at, idempotency_key)
        donor_id = case["active_donor_id"]
        return {"candidate": self._candidate_view(case, donor_id), "deduped": result["deduped"]}

    def submit_milestone(self, actor, case_id, alias, milestone_type, at, tz=None, details=None,
                         idempotency_key=None):
        """采集医院/移植医院/运输方提交的通用里程碑（保留提交时区）。"""
        case = self._case(case_id)
        role_field = None
        if actor.has(ROLE_COLLECTION_HOSPITAL):
            self._require_party(actor, case_id, ROLE_COLLECTION_HOSPITAL, "collection_hospital_id")
        elif actor.has(ROLE_TRANSPLANT_HOSPITAL):
            self._require_party(actor, case_id, ROLE_TRANSPLANT_HOSPITAL, "transplant_hospital_id")
        elif actor.has(ROLE_COURIER):
            self._require_party(actor, case_id, ROLE_COURIER, "courier_id")
        else:
            raise PermissionDenied("只有病例参与方可以提交里程碑")
        donor_id = self._resolve_alias(case, alias)
        result = self.workflow.submit_milestone(
            case, actor, donor_id, milestone_type, at, tz=tz, details=details,
            idempotency_key=idempotency_key)
        return {"milestone": self._milestone_view(case, result), "deduped": result["deduped"]}

    def complete_case(self, actor, case_id, idempotency_key=None):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        result = self.workflow.complete(case, actor, idempotency_key)
        return {"state": result["state"]}

    # ============================== 替补与方案 ==============================

    def activate_backup(self, actor, case_id, idempotency_key=None):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        result = self.workflow.activate_backup(case, actor, idempotency_key=idempotency_key)
        view = dict(result)
        view["from_alias"] = self._alias_of(case, result["from_donor"]) if result.get("from_donor") else None
        view["to_alias"] = self._alias_of(case, result["to_donor"])
        return view

    def case_contingencies(self, actor, case_id):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        return [self._contingency_view(c) for c in case.get("contingencies", [])]

    def release_active_candidate(self, actor, case_id, reason, idempotency_key=None):
        """活跃候选因不可并行原因（跨病例窗口互斥等）退出，开启替补方案。"""
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        result = self.workflow.release_active(case, actor, reason, idempotency_key=idempotency_key)
        return {"candidate": self._candidate_view(case, result["donor_id"]),
                "contingency": self._contingency_view(result["contingency"]),
                "deduped": result["deduped"]}

    # ============================== 紧急解封 ==============================

    def request_unseal(self, actor, case_id, aliases, reason, ttl_minutes=60):
        actor.require(ROLE_COORDINATOR)
        # 校验别名确实属于该病例
        case = self._case(case_id)
        subjects = []
        for alias in aliases:
            self._resolve_alias(case, alias)
            subjects.append(alias)
        grant_id, req = self.store.breakglass.request(
            self.clock, actor, case_id, subjects, reason, ttl_minutes
        )
        self.store.audit.record(self.clock, "unseal_request", actor=actor.id, case_id=case_id,
                                details={"grant_id": grant_id, "subjects": subjects, "reason": reason,
                                         "ttl_minutes": ttl_minutes})
        return {"grant_id": grant_id, "request": req}

    def approve_unseal(self, actor, grant_id):
        actor.require(ROLE_IDENTITY_OFFICER)
        before = self.store.breakglass.status_of(self.clock, grant_id)
        req = self.store.breakglass.approve(self.clock, actor, grant_id)
        self.store.audit.record(self.clock, "unseal_approved", actor=actor.id,
                                case_id=req["case_id"],
                                details={"grant_id": grant_id,
                                         "approvals": len(req["approvals"]),
                                         "status": req["status"],
                                         "expires_at": req["expires_at"]})
        return {"grant_id": grant_id, "request": req, "was_already_approved": len(before["approvals"]) > 0}

    def revoke_unseal(self, actor, grant_id):
        actor.require(ROLE_IDENTITY_OFFICER)
        req = self.store.breakglass.revoke(self.clock, actor, grant_id)
        self.store.audit.record(self.clock, "unseal_revoked", actor=actor.id,
                                case_id=req["case_id"], details={"grant_id": grant_id})
        return {"grant_id": grant_id, "request": req}

    def unseal_identity(self, actor, case_id, alias):
        """持有效双人许可读取真实身份；每次读取都写审计，许可到期自动拒绝。"""
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        donor_id = self._resolve_alias(case, alias)
        grant = self.store.breakglass.authorize(self.clock, actor, case_id, alias)
        record = self.store.vault.resolve(donor_id)
        self.store.audit.record(
            self.clock, "identity_revealed", actor=actor.id, case_id=case_id, subject=alias,
            details={"grant_id": grant.request["grant_id"], "reason": grant.request["reason"],
                     "internal_kind": record["kind"]},
        )
        # 返回真实身份（仅此一条路径），并附许可到期时间
        return {"identity": record, "grant_id": grant.request["grant_id"],
                "expires_at": grant.request["expires_at"]}

    def list_grants(self, actor):
        if not (actor.has(ROLE_IDENTITY_OFFICER) or actor.has(ROLE_AUDITOR)):
            raise PermissionDenied("只有身份官或审计员可查看解封请求")
        return self.store.breakglass.list(self.clock)

    # ============================== 审计与视图 ==============================

    def export_audit(self, actor, case_id=None, action=None):
        actor.require(ROLE_AUDITOR)
        events = self.store.audit.export(case_id=case_id, action=action)
        return {"events": events, "chain_valid": self.store.audit.verify_chain()}

    def case_view(self, actor, case_id):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        return {
            "case_id": case_id,
            "patient_alias": case["patient_alias"],
            "clinical_deadline": case["clinical_deadline"],
            "algorithm_version": case["algorithm_version"],
            "active_alias": self._alias_of(case, case["active_donor_id"]) if case.get("active_donor_id") else None,
            "candidates": [self._candidate_view(case, d) for d in
                           sorted(case.get("candidates", {}), key=lambda d: case["candidates"][d]["rank_position"])],
            "product_window": case.get("product_window"),
            "milestones": [self._milestone_view(case, m) for m in case.get("milestones", [])],
        }

    def _milestone_view(self, case, milestone):
        """对外里程碑视图：内部志愿者 ID 一律替换为病例作用域别名。"""
        view = dict(milestone)
        view["donor_alias"] = self._alias_of(case, view.pop("donor_id", None))
        return view

    def ranking_view(self, actor, case_id):
        actor.require(ROLE_COORDINATOR)
        case = self._case(case_id)
        ranking = case["last_ranking"]
        if ranking is None:
            raise DomainError("ranking_missing", "病例尚未排序")
        return {
            "case_id": case_id,
            "algorithm_version": ranking["algorithm_version"],
            "ranked_at": ranking["ranked_at"],
            "basis": ranking["basis"],
            "candidates": [
                {
                    "alias": self._alias_of(case, row["donor_id"]),
                    "rank_position": row["rank_position"],
                    "grade": row["grade"],
                    "score": row["score"],
                }
                for row in ranking["candidates"]
            ],
            "excluded": [
                {"alias": self._alias_of(case, row["donor_id"]), "reasons": row["reasons"]}
                for row in ranking["excluded"]
            ],
        }

    # ============================== 内部辅助 ==============================

    def _case(self, case_id):
        case = self.store.cases.get(case_id)
        if case is None:
            raise NotFound("病例", case_id)
        return case

    def _donor(self, donor_id):
        donor = self.store.donors.get(donor_id)
        if donor is None:
            raise NotFound("志愿者", donor_id)
        return donor

    def _resolve_alias(self, case, alias):
        for ref in case["donor_refs"]:
            if ref["alias"] == alias:
                return ref["donor_id"]
        raise NotFound("病例别名", f"{case['case_id']}/{alias}")

    def _alias_of(self, case, donor_id):
        if not donor_id:
            return None
        for ref in case["donor_refs"]:
            if ref["donor_id"] == donor_id:
                return ref["alias"]
        return donor_id

    def _resolve_active_alias(self, case, alias):
        if alias is None:
            if not case.get("active_donor_id"):
                raise DomainError("no_active_candidate", "该病例没有活跃候选人")
            return case["active_donor_id"]
        donor_id = self._resolve_alias(case, alias)
        self._require_active(case, donor_id)
        return donor_id

    def _require_active(self, case, donor_id):
        if case.get("active_donor_id") != donor_id:
            raise Conflict("not_active_candidate", "该候选不是当前活跃候选人")

    def _require_party(self, actor, case_id, role, assignment_field):
        actor.require(role)
        case = self._case(case_id)
        # 病例必须已指派该参与方，且指派对象就是调用方
        if case.get(assignment_field) != actor.id:
            raise PermissionDenied(
                "该病例未指派给当前参与方",
                {"assigned_to": case.get(assignment_field)},
            )

    def _candidate_view(self, case, donor_id):
        cand = case["candidates"][donor_id]
        return {
            "alias": self._alias_of(case, donor_id),
            "state": cand["state"],
            "grade": cand.get("grade"),
            "rank_position": cand["rank_position"],
        }

    def _dispatch_view(self, dispatch):
        return {
            "template": dispatch["template"],
            "at": dispatch["at"],
            "channel": dispatch["channel"],
            "idempotency_key": dispatch["idempotency_key"],
            "deduped": dispatch["deduped"],
        }

    def _contingency_view(self, record):
        return {
            "trigger": record["trigger"],
            "reason": record["reason"],
            "opened_at": record["opened_at"],
            "plans": [dict(p) for p in record["plans"]],
            "delay": record.get("delay"),
            "findings": record.get("findings", []),
        }

    def _withdraw_view(self, case, result):
        view = {"candidate": self._candidate_view(case, result["donor_id"]),
                "deduped": result["deduped"]}
        if result.get("contingency"):
            view["contingency"] = self._contingency_view(result["contingency"])
        return view

    def _hospital_result_view(self, case, result):
        view = {"candidate": self._candidate_view(case, result["donor_id"]),
                "state": result["state"], "deduped": result["deduped"]}
        if result.get("contingency"):
            view["contingency"] = self._contingency_view(result["contingency"])
        return view
