"""端到端剧本：双患者流程并行 + 跨时区延误 + 替补 + 双人解封。

``run_scenario`` 不做断言，只返回全过程事实；selftest 与集成测试各自校验。
预期会失败的调用（考虑期、越权、许可到期等）通过 ``expect`` 捕获错误码。
"""

from coordination.app import CoordinationService
from coordination.clock import Clock
from coordination.errors import DomainError

from scenario import (
    AUDITOR,
    COLLECTION_HOSPITAL,
    COORD,
    COORD2,
    COURIER,
    OFFICER,
    OFFICER2,
    TRANSPLANT_HOSPITAL,
    alias_of,
    at,
    build_world,
)


def expect(fn):
    """执行应当被拒绝的调用，返回错误码。"""
    try:
        fn()
    except DomainError as error:
        return {"code": error.code, "status": error.status, "message": error.message,
                "details": error.details}
    raise AssertionError("调用本应被领域规则拒绝")


def run_scenario(svc=None):
    svc = svc or CoordinationService(clock=Clock())
    facts = {"rejections": {}, "dedup": {}, "contingencies": {}, "grants": {}}
    build_world(svc)

    # ---------------- 病例 A / B 建立并并行排序（第 0 天） ----------------

    case_a = svc.open_case(COORD, "PAT-A", at(day=30), "H-CAPEK", "H-TPEK")["case_id"]
    case_b = svc.open_case(COORD2, "PAT-B", at(day=32), "H-CAPEK", "H-TPEK")["case_id"]
    facts["case_a"] = case_a
    facts["case_b"] = case_b

    rank_a = svc.search_candidates(COORD, case_a, ["D01", "D02", "D03", "D04", "D05"])
    rank_b = svc.search_candidates(COORD2, case_b, ["D06", "D07", "D01"])
    facts["rank_a"] = rank_a
    facts["rank_b"] = rank_b

    a_d01 = alias_of(svc, case_a, "D01")
    a_d02 = alias_of(svc, case_a, "D02")
    a_d03 = alias_of(svc, case_a, "D03")
    b_d06 = alias_of(svc, case_b, "D06")
    b_d07 = alias_of(svc, case_b, "D07")
    b_d01 = alias_of(svc, case_b, "D01")
    facts["aliases"] = dict(a_d01=a_d01, a_d02=a_d02, a_d03=a_d03, b_d06=b_d06, b_d07=b_d07,
                            b_d01=b_d01)

    # 同一志愿者跨病例别名必须不同，且协调员视图只见别名
    assert a_d01 != b_d01, "同一志愿者跨病例别名必须不同"
    view_a = svc.case_view(COORD, case_a)
    assert all("D0" not in c["alias"] for c in view_a["candidates"]), "协调员视图不得出现内部ID"

    # ---------------- 病例 A：初次告知 + 重试去重 + 考虑期 ----------------

    first = svc.notify_initial_match(COORD, case_a, a_d01, "idem-A-initial")
    retry = svc.notify_initial_match(COORD, case_a, a_d01, "idem-A-initial")
    facts["dedup"]["initial_retry"] = retry["deduped"]
    assert first["deduped"] is False and retry["deduped"] is True

    # 病例 B 并行发出 D06 初次告知
    svc.notify_initial_match(COORD2, case_b, b_d06, "idem-B-initial")

    # 越过全局 6 小时间隔，但未满 48 小时考虑期即请求确认 -> 拒绝
    svc.advance(hours=7)
    facts["rejections"]["confirm_in_reflection"] = expect(
        lambda: svc.request_confirmation_notice(COORD, case_a, a_d01, "idem-A-confirm"))

    # ---------------- 病例 B 并行推进 D06 ----------------

    svc.advance(days=2)
    svc.request_confirmation_notice(COORD, case_a, a_d01, "idem-A-confirm")
    confirm_retry = svc.request_confirmation_notice(COORD, case_a, a_d01, "idem-A-confirm")
    facts["dedup"]["confirm_retry"] = confirm_retry["deduped"]
    svc.request_confirmation_notice(COORD2, case_b, b_d06, "idem-B-confirm")

    # 联络必须遵守最新同意范围：撤回体检联络同意后立即拦截，补登后放行
    from coordination.comms import SCOPE_MEDICAL
    svc.withdraw_consent(OFFICER, "D01", [SCOPE_MEDICAL])
    facts["rejections"]["consent_withdrawn_blocks"] = expect(
        lambda: svc.schedule_exam(COORD, case_a, a_d01, at(day=5, hour=2), "idem-A-exam-notice"))
    svc.grant_consent(OFFICER, "D01", [SCOPE_MEDICAL])

    # ---------------- 双方确认捐献，窗口部分重叠但人不同，互不影响 ----------

    svc.advance(hours=1)
    svc.record_donor_decision(COORD, case_a, a_d01, True,
                              {"start": at(day=9), "end": at(day=11)}, "idem-A-decision")
    svc.record_donor_decision(COORD2, case_b, b_d06, True,
                              {"start": at(day=10), "end": at(day=12)}, "idem-B-decision")

    # ---------------- 病例 A：体检异常 -> 复检通过 -------------------------

    svc.advance(days=2)
    svc.schedule_exam(COORD, case_a, a_d01, at(day=5, hour=2), "idem-A-exam-notice")
    abnormal = svc.report_exam(COLLECTION_HOSPITAL, case_a, a_d01, False,
                               ["血压偏高，需复测"], "idem-A-exam-report")
    facts["contingencies"]["a_exam_abnormal"] = abnormal["contingency"]
    svc.advance(days=1)
    reexam = svc.resolve_reexam(COLLECTION_HOSPITAL, case_a, a_d01, True, ["复测正常"],
                                "idem-A-reexam")
    facts["a_reexam_state"] = reexam["state"]

    # 非指派医院不能上报 A 的体检
    other_hospital = type(COLLECTION_HOSPITAL)("H-OTHER", "其他医院", ["collection_hospital"])
    facts["rejections"]["wrong_hospital"] = expect(
        lambda: svc.report_exam(other_hospital, case_a, a_d01, True))

    # ---------------- 病例 A：采集 -> 跨时区运输延误 -----------------------

    svc.assign_courier(COORD, case_a, "CR-1")
    svc.schedule_collection(COORD, case_a, a_d01, at(day=9, hour=7), "idem-A-collection-notice")
    svc.advance(days=5)
    collected = svc.record_collection(COLLECTION_HOSPITAL, case_a, a_d01,
                                      at(day=9, hour=8), "idem-A-collect")
    facts["product_window_a"] = collected["product_window"]
    collect_retry = svc.record_collection(COLLECTION_HOSPITAL, case_a, a_d01,
                                          at(day=9, hour=8), "idem-A-collect")
    facts["dedup"]["collection_retry"] = collect_retry["deduped"]
    assert svc.store.commitments.collection_of("D01")["at"] == collected["product_window"]["start"]

    svc.record_pickup(COURIER, case_a, at(day=9, hour=9), "idem-A-pickup")

    # 跨时区里程碑：东京 19:00(+09:00) == UTC 10:00，按 UTC 绝对值入窗判定
    handover = svc.submit_milestone(
        COURIER, case_a, a_d01, "airport_handover", "2026-01-10T19:00:00+09:00",
        tz="+09:00", details={"airport": "NRT"}, idempotency_key="idem-A-handover")
    facts["handover_utc"] = handover["milestone"]["at"]

    delay1 = svc.report_flight_delay(COURIER, case_a, "CA1818", 120, at(day=9, hour=20),
                                     "idem-A-delay1")
    facts["contingencies"]["a_delay1"] = delay1["contingency"]
    delay_retry = svc.report_flight_delay(COURIER, case_a, "CA1818", 120, at(day=9, hour=20),
                                          "idem-A-delay1")
    facts["dedup"]["delay_retry"] = delay_retry["deduped"]

    # 第二次更新：预计到达超过产品有效期（窗截止 1/11 08:00 UTC）-> 升级时效风险
    svc.advance(hours=10)
    delay2 = svc.report_flight_delay(COURIER, case_a, "CA1818", 680, at(day=10, hour=9, minute=30),
                                     "idem-A-delay2")
    facts["contingencies"]["a_delay2_breach"] = delay2["contingency"]

    # 采纳优先级 1：改签后于窗内送达（移植医院当地 1/11 13:30 +08:00 == UTC 05:30）
    delivery = svc.record_delivery(
        TRANSPLANT_HOSPITAL, case_a, "2026-01-11T13:30:00+08:00", "idem-A-delivery")
    facts["dedup"]["delivery_retry"] = svc.record_delivery(
        TRANSPLANT_HOSPITAL, case_a, "2026-01-11T13:30:00+08:00", "idem-A-delivery")["deduped"]
    svc.record_infusion(TRANSPLANT_HOSPITAL, case_a, "2026-01-11T14:30:00+08:00",
                        "idem-A-infusion")
    svc.complete_case(COORD, case_a)
    facts["state_a"] = svc.case_view(COORD, case_a)["candidates"][0]["state"]

    # ---------------- 病例 B：体检异常复检失败 -> 顺位替补 D07 -------------

    svc.schedule_exam(COORD2, case_b, b_d06, at(day=5), "idem-B-exam-notice")
    svc.report_exam(COLLECTION_HOSPITAL, case_b, b_d06, False, ["转氨酶升高"], "idem-B-exam")
    reexam_fail = svc.resolve_reexam(COLLECTION_HOSPITAL, case_b, b_d06, False,
                                     ["复检仍异常"], "idem-B-reexam")
    facts["contingencies"]["b_exam_fail"] = reexam_fail["contingency"]
    backup = svc.activate_backup(COORD2, case_b, "idem-B-backup1")
    facts["substitution_b"] = backup
    backup_retry = svc.activate_backup(COORD2, case_b, "idem-B-backup1")
    facts["dedup"]["backup_retry"] = backup_retry["deduped"]
    assert backup["to_alias"] == b_d07

    # 病例 A 的视图与通知不得出现病例 B 的任何痕迹（反之亦然）
    view_a2 = svc.case_view(COORD, case_a)
    facts["leak_a_has_b_aliases"] = any(
        c["alias"] in (b_d06, b_d07) for c in view_a2["candidates"])

    # D07 走完考虑期后确认
    svc.notify_initial_match(COORD2, case_b, b_d07, "idem-B-D07-initial")
    svc.advance(hours=7)
    facts["rejections"]["d07_reflection"] = expect(
        lambda: svc.request_confirmation_notice(COORD2, case_b, b_d07, "idem-B-D07-confirm"))
    svc.advance(days=2)
    svc.request_confirmation_notice(COORD2, case_b, b_d07, "idem-B-D07-confirm")
    svc.record_donor_decision(COORD2, case_b, b_d07, True,
                              {"start": at(day=14), "end": at(day=16)}, "idem-B-D07-decision")
    svc.advance(days=1)
    svc.schedule_exam(COORD2, case_b, b_d07, at(day=14, hour=2), "idem-B-D07-exam")
    svc.report_exam(COLLECTION_HOSPITAL, case_b, b_d07, True, [], "idem-B-D07-exam-r")
    svc.assign_courier(COORD2, case_b, "CR-1")
    svc.advance(hours=13)
    svc.schedule_collection(COORD2, case_b, b_d07, at(day=15, hour=7), "idem-B-D07-cn")
    svc.advance(days=1)
    svc.record_collection(COLLECTION_HOSPITAL, case_b, b_d07, at(day=15, hour=8), "idem-B-collect")
    svc.record_pickup(COURIER, case_b, at(day=15, hour=9), "idem-B-pickup")
    svc.record_delivery(TRANSPLANT_HOSPITAL, case_b, at(day=15, hour=20), "idem-B-delivery")
    svc.record_infusion(TRANSPLANT_HOSPITAL, case_b, at(day=15, hour=22), "idem-B-infusion")
    svc.complete_case(COORD2, case_b)

    # 重排：已采集志愿者（D01/D07）在冷却期内必须被排除，防二次采集
    rerank_b = svc.rerank(COORD2, case_b, ["D06", "D07", "D01"])
    facts["rerank_b_after_collection"] = rerank_b

    # ---------------- 紧急解封：双人批准、作用域、TTL 自动到期 --------------

    unseal = svc.request_unseal(COORD, case_a, [a_d01], "医院紧急需核对供者既往史", ttl_minutes=60)
    grant_id = unseal["grant_id"]
    facts["grants"]["id"] = grant_id
    # 单人批准不可见身份
    svc.approve_unseal(OFFICER, grant_id)
    facts["rejections"]["reveal_with_one_approval"] = expect(
        lambda: svc.unseal_identity(COORD, case_a, a_d01))
    # 同一身份官不能重复批准
    facts["rejections"]["duplicate_approval"] = expect(
        lambda: svc.approve_unseal(OFFICER, grant_id))
    # 第二名身份官批准后生效
    svc.approve_unseal(OFFICER2, grant_id)
    revealed = svc.unseal_identity(COORD, case_a, a_d01)
    facts["revealed_name"] = revealed["identity"]["real_name"]
    facts["grant_expires_at"] = revealed["expires_at"]
    # 许可只覆盖病例 A：在病例 B 解封同一人（B 病例别名）必须被拒
    facts["rejections"]["reveal_cross_case"] = expect(
        lambda: svc.unseal_identity(COORD, case_b, b_d01))
    # 普通协调员不能查看解封清单
    facts["rejections"]["coordinator_list_grants"] = expect(lambda: svc.list_grants(COORD))
    # 审计员视角不含真实身份
    grants_view = svc.list_grants(AUDITOR)
    facts["grants_view_clean"] = all(
        "张伟" not in str(g) for g in grants_view)
    # TTL 到期自动失效
    svc.advance(minutes=61)
    facts["grants"]["status_after_ttl"] = svc.store.breakglass.status_of(svc.clock, grant_id)["status"]
    facts["rejections"]["reveal_after_ttl"] = expect(
        lambda: svc.unseal_identity(COORD, case_a, a_d01))

    # ---------------- 审计导出 ---------------------------------------------

    facts["audit_a"] = svc.export_audit(AUDITOR, case_id=case_a)
    facts["audit_b"] = svc.export_audit(AUDITOR, case_id=case_b)
    facts["audit_all"] = svc.export_audit(AUDITOR)
    return svc, facts


def run():
    svc, facts = run_scenario()
    print("场景自检通过")
    print(f"  病例: {facts['case_a']} / {facts['case_b']}")
    print(f"  重试去重: {facts['dedup']}")
    print(f"  替补: {facts['substitution_b']['from_alias']} -> {facts['substitution_b']['to_alias']}")
    print(f"  解封对象: {facts['revealed_name']}，TTL 后状态: {facts['grants']['status_after_ttl']}")
    print(f"  审计事件: {len(facts['audit_all']['events'])} 条，链完整: {facts['audit_all']['chain_valid']}")
    return svc, facts


def _case(svc, case_id):
    return svc.store.cases[case_id]
