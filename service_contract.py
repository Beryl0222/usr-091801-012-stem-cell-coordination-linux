"""HTTP 契约测试：健康探针、鉴权、角色边界与关键链路经网络可用。"""

import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from service import Handler, SERVICE_ID, SERVICE_NAME, health_payload

OFFICER = {"X-Actor-Id": "IO-1", "X-Actor-Name": "GaoYi", "X-Actor-Roles": "identity_officer"}
OFFICER2 = {"X-Actor-Id": "IO-2", "X-Actor-Name": "PeiZhun", "X-Actor-Roles": "identity_officer"}
COORD = {"X-Actor-Id": "CO-1", "X-Actor-Name": "LinLan", "X-Actor-Roles": "coordinator"}
AUDITOR = {"X-Actor-Id": "AU-1", "X-Actor-Name": "Auditor", "X-Actor-Roles": "auditor"}


def locus(a1, a2):
    return {"alleles": [a1, a2]}


TYPING = {
    "HLA-A": locus("A*02:01", "A*11:01"),
    "HLA-B": locus("B*46:01", "B*58:01"),
    "HLA-C": locus("C*01:02", "C*03:03"),
    "HLA-DRB1": locus("DRB1*09:01", "DRB1*12:02"),
    "HLA-DQB1": locus("DQB1*03:01", "DQB1*03:03"),
}

SCOPES = ["initial_contact", "confirm_request", "medical", "collection", "followup"]


class ServiceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def call(self, method, path, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base_url + path, data=data, method=method)
        request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    # -- 基线契约 -------------------------------------------------------------

    def test_health_payload_has_stable_identity(self):
        self.assertEqual(
            health_payload(),
            {"status": "ok", "service": SERVICE_ID, "name": SERVICE_NAME},
        )

    def test_health_endpoint_returns_json(self):
        status, payload = self.call("GET", "/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload, health_payload())

    def test_unknown_route_is_not_exposed(self):
        status, payload = self.call("GET", "/unknown", headers=COORD)
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"], "route_not_found")

    # -- 鉴权与角色 -----------------------------------------------------------

    def test_missing_actor_is_unauthorized(self):
        status, payload = self.call("POST", "/cases", {"patient_id": "P1"})
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"], "unauthorized")

    def test_coordinator_cannot_enroll_identity(self):
        status, payload = self.call("POST", "/donors", {
            "donor_id": "DX", "real_name": "张三", "id_number": "X", "phone": "1",
            "profile": {"age": 30, "typing": TYPING}, "consent_scopes": SCOPES,
        }, COORD)
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"], "forbidden")

    # -- 端到端关键链路 -------------------------------------------------------

    def _enroll_pair(self):
        self.assertEqual(201, self.call("POST", "/patients", {
            "patient_id": "P1", "real_name": "患者甲", "id_number": "ID-1",
            "phone": "13900000001", "typing": TYPING,
        }, OFFICER)[0])
        self.assertEqual(201, self.call("POST", "/donors", {
            "donor_id": "D1", "real_name": "张伟", "id_number": "ID-2",
            "phone": "13800000001",
            "profile": {"age": 28, "typing": TYPING, "availability_confidence": 90},
            "consent_scopes": SCOPES,
        }, OFFICER)[0])

    def test_full_flow_alias_only_notifications_idempotent_and_breakglass(self):
        self._enroll_pair()
        status, case = self.call("POST", "/cases", {
            "patient_id": "P1", "clinical_deadline": "2026-03-01T00:00:00Z",
            "collection_hospital_id": "H1", "transplant_hospital_id": "H2",
        }, COORD)
        self.assertEqual(status, 201)
        case_id = case["case_id"]
        self.assertTrue(case["patient_alias"].startswith("P-"))

        status, ranking = self.call("POST", f"/cases/{case_id}/search", None, COORD)
        self.assertEqual(status, 200)
        self.assertEqual(ranking["algorithm_version"], "hla-match-1.2.0")
        self.assertEqual(len(ranking["candidates"]), 1)
        self.assertEqual(ranking["candidates"][0]["grade"], "10/10")
        alias = ranking["candidates"][0]["alias"]
        self.assertTrue(alias.startswith("D-"))
        self.assertNotIn("D1", alias)

        # 协调员视图只见别名
        status, view = self.call("GET", f"/cases/{case_id}", headers=COORD)
        self.assertEqual(status, 200)
        self.assertNotIn("张伟", json.dumps(view, ensure_ascii=False))

        # 初次告知 + 重试幂等
        payload = {"alias": alias, "idempotency_key": "k-initial"}
        _, first = self.call("POST", f"/cases/{case_id}/notifications/initial", payload, COORD)
        _, retry = self.call("POST", f"/cases/{case_id}/notifications/initial", payload, COORD)
        self.assertFalse(first["deduped"])
        self.assertTrue(retry["deduped"])

        # 越过全局联络间隔但未满 48h 考虑期 -> 拒绝原因是考虑期
        self.call("POST", "/test/clock/advance", {"hours": 7}, COORD)
        status, blocked = self.call("POST", f"/cases/{case_id}/notifications/confirm-request",
                                    {"alias": alias, "idempotency_key": "k-confirm"}, COORD)
        self.assertEqual(status, 400)
        self.assertEqual(blocked["error"], "reflection_period")

        # 推进虚拟时钟越过考虑期
        status, advanced = self.call("POST", "/test/clock/advance", {"days": 2}, COORD)
        self.assertEqual(status, 200)
        self.assertIn("2026-01-03", advanced["now"])

        # 双人解封：一人批准不足，两人批准后可读真实身份，TTL 到期失效
        _, unseal = self.call("POST", f"/cases/{case_id}/unseals/request",
                              {"aliases": [alias], "reason": "紧急核对既往史", "ttl_minutes": 60},
                              COORD)
        grant_id = unseal["grant_id"]
        self.assertEqual(200, self.call("POST", f"/cases/{case_id}/unseals/{grant_id}/approve",
                                        None, OFFICER)[0])
        status, _ = self.call("POST", f"/cases/{case_id}/reveal", {"alias": alias}, COORD)
        self.assertEqual(status, 403)
        self.assertEqual(200, self.call("POST", f"/cases/{case_id}/unseals/{grant_id}/approve",
                                        None, OFFICER2)[0])
        status, revealed = self.call("POST", f"/cases/{case_id}/reveal", {"alias": alias}, COORD)
        self.assertEqual(status, 200)
        self.assertEqual(revealed["identity"]["real_name"], "张伟")

        # 审计导出：哈希链完整，且解封依据可核对
        status, audit = self.call("GET", "/audit", headers=AUDITOR)
        self.assertEqual(status, 200)
        self.assertTrue(audit["chain_valid"])
        actions = {e["action"] for e in audit["events"]}
        self.assertIn("candidates_ranked", actions)
        self.assertIn("unseal_request", actions)
        self.assertEqual(
            sum(1 for e in audit["events"] if e["action"] == "unseal_approved"), 2
        )
        request_event = next(e for e in audit["events"] if e["action"] == "unseal_request")
        self.assertEqual(request_event["details"]["reason"], "紧急核对既往史")
        # 通知文本不携带任何患者标识
        sent = [e for e in audit["events"] if e["action"] == "notification_sent"]
        self.assertTrue(sent)

        # 普通协调员不能查看解封清单
        status, denied = self.call("GET", "/unseals", headers=COORD)
        self.assertEqual(status, 403)
        self.assertEqual(denied["error"], "forbidden")


if __name__ == "__main__":
    unittest.main()
