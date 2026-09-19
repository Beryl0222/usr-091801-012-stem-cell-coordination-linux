"""HTTP JSON 适配层。

鉴权采用开发期简化方案：调用方通过 X-Actor-Id / X-Actor-Name / X-Actor-Roles
表明身份，真实部署中由网关注入并核验。业务侧的权限判定全部在领域层完成。
"""

import json
from urllib.parse import urlparse, parse_qs

from .app import CoordinationService
from .errors import DomainError, Unauthorized
from .identity import Actor

# 路由表：(方法, 路径模板) -> (处理器名, 参数字段名)
# 路径段以 {name} 占位


class ApiRouter:
    def __init__(self, service=None, clock_control=True):
        self.service = service or CoordinationService()
        self.clock_control = clock_control

    # -- 入口 ----------------------------------------------------------------

    def handle(self, method, path, headers, body_bytes):
        """返回 (status, payload)。"""
        parsed = urlparse(path)
        if method == "GET" and parsed.path == "/health":
            from service import health_payload

            return 200, health_payload()
        try:
            actor = self._authenticate(headers)
            payload = self._read_json(body_bytes)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            return self._route(method, parsed.path, actor, payload, query)
        except DomainError as error:
            return error.status, {"error": error.code, "message": error.message, "details": error.details}
        except (ValueError, KeyError, TypeError) as error:
            return 400, {"error": "bad_request", "message": str(error)}

    def _authenticate(self, headers):
        actor_id = headers.get("x-actor-id")
        if not actor_id:
            raise Unauthorized()
        roles = [r.strip() for r in (headers.get("x-actor-roles") or "").split(",") if r.strip()]
        return Actor(actor_id, headers.get("x-actor-name", actor_id), roles)

    @staticmethod
    def _read_json(body_bytes):
        if not body_bytes:
            return {}
        return json.loads(body_bytes.decode("utf-8"))

    # -- 路由 ----------------------------------------------------------------

    def _route(self, method, path, actor, payload, query):
        svc = self.service

        if method == "POST" and path == "/test/clock/advance":
            if not self.clock_control:
                raise DomainError("clock_control_disabled", "该环境未开放时钟控制", status=403)
            return 200, {"now": svc.advance(**payload).isoformat()}

        if method == "POST" and path == "/donors":
            return 201, svc.enroll_donor(
                actor, payload["donor_id"], payload["real_name"], payload["id_number"],
                payload["phone"], payload["profile"], payload["consent_scopes"],
            )

        # /donors/{donor_id}/consent:grant|withdraw
        if method == "POST":
            donor_segments = [s for s in path.split("/") if s]
            if (len(donor_segments) == 3 and donor_segments[0] == "donors"
                    and donor_segments[2] in ("consent:grant", "consent:withdraw")):
                donor_id = donor_segments[1]
                if donor_segments[2] == "consent:grant":
                    return 200, svc.grant_consent(actor, donor_id, payload["scopes"])
                return 200, svc.withdraw_consent(actor, donor_id, payload["scopes"])
        if method == "POST" and path == "/patients":
            return 201, svc.enroll_patient(
                actor, payload["patient_id"], payload["real_name"], payload["id_number"],
                payload["phone"], payload["typing"],
            )
        if method == "POST" and path == "/cases":
            return 201, svc.open_case(
                actor, payload["patient_id"], payload["clinical_deadline"],
                payload["collection_hospital_id"], payload["transplant_hospital_id"],
                payload.get("algorithm_version"),
            )

        # /cases/{case_id}/...
        segments = [s for s in path.split("/") if s]
        if len(segments) >= 2 and segments[0] == "cases":
            case_id = segments[1]
            sub = "/".join(segments[2:])
            return self._case_route(method, sub, actor, case_id, payload, query)

        if method == "GET" and path == "/unseals":
            return 200, {"grants": svc.list_grants(actor)}
        if method == "GET" and path == "/audit":
            return 200, svc.export_audit(
                actor, case_id=query.get("case_id"),
                action=query.get("action").split(",") if query.get("action") else None,
            )
        raise DomainError("route_not_found", f"无此路由: {method} {path}", status=404)

    def _case_route(self, method, sub, actor, case_id, p, query):
        svc = self.service

        if method == "GET" and sub == "":
            return 200, svc.case_view(actor, case_id)
        if method == "GET" and sub == "ranking":
            return 200, svc.ranking_view(actor, case_id)
        if method == "GET" and sub == "contingencies":
            return 200, {"contingencies": svc.case_contingencies(actor, case_id)}

        if method == "POST":
            if sub == "search":
                return 200, svc.search_candidates(actor, case_id, p.get("donor_ids"),
                                                  p.get("algorithm_version"))
            if sub == "rerank":
                return 200, svc.rerank(actor, case_id, p.get("donor_ids"),
                                       p.get("algorithm_version"))
            if sub == "notifications/initial":
                return 200, svc.notify_initial_match(actor, case_id, p["alias"], p["idempotency_key"])
            if sub == "notifications/confirm-request":
                return 200, svc.request_confirmation_notice(actor, case_id, p["alias"],
                                                            p["idempotency_key"])
            if sub == "notifications/exam":
                return 200, svc.schedule_exam(actor, case_id, p["alias"], p["scheduled_at"],
                                              p["idempotency_key"])
            if sub == "notifications/collection":
                return 200, svc.schedule_collection(actor, case_id, p["alias"], p["scheduled_at"],
                                                    p["idempotency_key"])
            if sub == "decisions":
                return 200, svc.record_donor_decision(
                    actor, case_id, p["alias"], p["confirmed"], p.get("collection_window"),
                    p.get("idempotency_key"), p.get("decline_reason", "志愿者婉拒"),
                )
            if sub == "withdrawals":
                return 200, svc.withdraw_donor(actor, case_id, p["alias"], p["reason"],
                                               p.get("idempotency_key"))
            if sub == "courier":
                return 200, svc.assign_courier(actor, case_id, p["courier_id"])
            if sub == "exams/report":
                return 200, svc.report_exam(actor, case_id, p["alias"], p["passed"],
                                            p.get("findings"), p.get("idempotency_key"))
            if sub == "exams/reexam":
                return 200, svc.resolve_reexam(actor, case_id, p["alias"], p["passed"],
                                               p.get("findings"), p.get("idempotency_key"))
            if sub == "collection":
                return 200, svc.record_collection(actor, case_id, p.get("alias"),
                                                  p.get("collected_at"), p.get("idempotency_key"))
            if sub == "pickup":
                return 200, svc.record_pickup(actor, case_id, p.get("at"), p.get("idempotency_key"))
            if sub == "delays":
                return 200, svc.report_flight_delay(
                    actor, case_id, p["flight_no"], p["delay_minutes"], p["projected_arrival_at"],
                    p.get("idempotency_key"),
                )
            if sub == "delivery":
                return 200, svc.record_delivery(actor, case_id, p.get("at"), p.get("idempotency_key"))
            if sub == "infusion":
                return 200, svc.record_infusion(actor, case_id, p.get("at"), p.get("idempotency_key"))
            if sub == "milestones":
                return 200, svc.submit_milestone(
                    actor, case_id, p["alias"], p["milestone_type"], p["at"], p.get("tz"),
                    p.get("details"), p.get("idempotency_key"),
                )
            if sub == "complete":
                return 200, svc.complete_case(actor, case_id, p.get("idempotency_key"))
            if sub == "backup/activate":
                return 200, svc.activate_backup(actor, case_id, p.get("idempotency_key"))
            if sub == "release-active":
                return 200, svc.release_active_candidate(actor, case_id, p["reason"],
                                                         p.get("idempotency_key"))
            if sub == "unseals/request":
                return 201, svc.request_unseal(actor, case_id, p["aliases"], p["reason"],
                                               p.get("ttl_minutes", 60))
            if sub == "reveal":
                return 200, svc.unseal_identity(actor, case_id, p["alias"])

        # /cases/{id}/unseals/{grantId}/approve|revoke
        if method == "POST" and len(segments := [s for s in sub.split("/") if s]) == 3 and segments[0] == "unseals":
            grant_id, action = segments[1], segments[2]
            if action == "approve":
                return 200, svc.approve_unseal(actor, grant_id)
            if action == "revoke":
                return 200, svc.revoke_unseal(actor, grant_id)

        raise DomainError("route_not_found", f"无此路由: {method} /cases/{case_id}/{sub}", status=404)
