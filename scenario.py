"""联调场景夹具：双患者流程、志愿者池、角色与跨时区延误。

测试与 ``python3 service.py --selftest`` 共用同一份剧本，保证自检与
联调断言描述的是同一个世界。
"""

from coordination.clock import TIME_ZERO, iso
from coordination.comms import (
    SCOPE_COLLECTION,
    SCOPE_CONFIRM_REQUEST,
    SCOPE_FOLLOWUP,
    SCOPE_INITIAL_CONTACT,
    SCOPE_MEDICAL,
)
from coordination.identity import Actor

ALL_CONTACT_SCOPES = [
    SCOPE_INITIAL_CONTACT,
    SCOPE_CONFIRM_REQUEST,
    SCOPE_MEDICAL,
    SCOPE_COLLECTION,
    SCOPE_FOLLOWUP,
]

# ---- 角色 ------------------------------------------------------------------

OFFICER = Actor("IO-1", "高怡(身份官)", ["identity_officer"])
OFFICER2 = Actor("IO-2", "裴准(身份官)", ["identity_officer"])
COORD = Actor("CO-1", "林岚(协调员)", ["coordinator"])
COORD2 = Actor("CO-2", "沈樾(协调员)", ["coordinator"])
COLLECTION_HOSPITAL = Actor("H-CAPEK", "北京采集医院", ["collection_hospital"])
TRANSPLANT_HOSPITAL = Actor("H-TPEK", "台北移植医院", ["transplant_hospital"])
COURIER = Actor("CR-1", "医疗运输方", ["courier"])
AUDITOR = Actor("AU-1", "审计员", ["auditor"])


def locus(a1, a2):
    return {"alleles": [a1, a2]}


# 患者 A 的分型
PATIENT_A_TYPING = {
    "HLA-A": locus("A*02:01", "A*11:01"),
    "HLA-B": locus("B*46:01", "B*58:01"),
    "HLA-C": locus("C*01:02", "C*03:03"),
    "HLA-DRB1": locus("DRB1*09:01", "DRB1*12:02"),
    "HLA-DQB1": locus("DQB1*03:01", "DQB1*03:03"),
}

# 患者 B 的分型（不同患者）
PATIENT_B_TYPING = {
    "HLA-A": locus("A*02:01", "A*24:02"),
    "HLA-B": locus("B*46:01", "B*51:01"),
    "HLA-C": locus("C*01:02", "C*15:02"),
    "HLA-DRB1": locus("DRB1*09:01", "DRB1*14:54"),
    "HLA-DQB1": locus("DQB1*03:01", "DQB1*05:03"),
}

# 与患者 A 10/10 高分辨全合
DONOR_D01_TYPING = {
    "HLA-A": locus("A*02:01", "A*11:01"),
    "HLA-B": locus("B*46:01", "B*58:01"),
    "HLA-C": locus("C*01:02", "C*03:03"),
    "HLA-DRB1": locus("DRB1*09:01", "DRB1*12:02"),
    "HLA-DQB1": locus("DQB1*03:01", "DQB1*03:03"),
}

# 9/10：A 位点一个等位基因不同
DONOR_D02_TYPING = dict(DONOR_D01_TYPING)
DONOR_D02_TYPING["HLA-A"] = locus("A*02:01", "A*24:02")

# 10/10 但配合度低、年龄偏大
DONOR_D03_TYPING = dict(DONOR_D01_TYPING)

# 分型不完整
DONOR_D05_TYPING = {k: v for k, v in DONOR_D01_TYPING.items() if k != "HLA-DQB1"}

# 与患者 B 10/10 全合
DONOR_D06_TYPING = {
    "HLA-A": locus("A*02:01", "A*24:02"),
    "HLA-B": locus("B*46:01", "B*51:01"),
    "HLA-C": locus("C*01:02", "C*15:02"),
    "HLA-DRB1": locus("DRB1*09:01", "DRB1*14:54"),
    "HLA-DQB1": locus("DQB1*03:01", "DQB1*05:03"),
}

DONOR_D07_TYPING = dict(DONOR_D06_TYPING)


def build_world(svc):
    """登记 2 名患者与 7 名志愿者，返回句柄字典。"""
    s = svc
    s.enroll_patient(OFFICER, "PAT-A", "患者甲", "ID-A-0001", "13900000001", PATIENT_A_TYPING)
    s.enroll_patient(OFFICER, "PAT-B", "患者乙", "ID-B-0002", "13900000002", PATIENT_B_TYPING)

    s.enroll_donor(
        OFFICER, "D01", "张伟", "110101-1998-A1", "13811110001",
        {"age": 28, "typing": DONOR_D01_TYPING, "availability_confidence": 90},
        ALL_CONTACT_SCOPES,
    )
    s.enroll_donor(
        OFFICER, "D02", "李娜", "310101-1996-B2", "13822220002",
        {"age": 30, "typing": DONOR_D02_TYPING, "availability_confidence": 80},
        ALL_CONTACT_SCOPES,
    )
    s.enroll_donor(
        OFFICER, "D03", "王强", "440101-1986-C3", "13833330003",
        {"age": 40, "typing": DONOR_D03_TYPING, "availability_confidence": 55},
        # 只有初次联络同意，演示同意范围不足
        [SCOPE_INITIAL_CONTACT],
    )
    s.enroll_donor(
        OFFICER, "D04", "赵敏", "320101-1990-D4", "13844440004",
        {"age": 35, "typing": DONOR_D01_TYPING, "medical_deferral": True,
         "availability_confidence": 70},
        ALL_CONTACT_SCOPES,
    )
    s.enroll_donor(
        OFFICER, "D05", "陈杰", "510101-1999-E5", "13855550005",
        {"age": 27, "typing": DONOR_D05_TYPING, "typing_complete": False,
         "availability_confidence": 75},
        ALL_CONTACT_SCOPES,
    )
    s.enroll_donor(
        OFFICER, "D06", "林芳", "330101-1997-F6", "13866660006",
        {"age": 29, "typing": DONOR_D06_TYPING, "availability_confidence": 88},
        ALL_CONTACT_SCOPES,
    )
    s.enroll_donor(
        OFFICER, "D07", "周磊", "120101-1995-G7", "13877770007",
        {"age": 31, "typing": DONOR_D07_TYPING, "availability_confidence": 50},
        ALL_CONTACT_SCOPES,
    )
    return {
        "patients": ["PAT-A", "PAT-B"],
        "donors": ["D01", "D02", "D03", "D04", "D05", "D06", "D07"],
    }


def alias_of(svc, case_id, donor_id):
    return svc.store.donor_alias_in_case(case_id, donor_id)


def at(day=0, hour=0, minute=0):
    """相对虚拟起点的 ISO 时间。"""
    from datetime import timedelta

    return iso(TIME_ZERO + timedelta(days=day, hours=hour, minutes=minute))
