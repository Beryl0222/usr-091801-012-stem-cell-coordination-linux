"""测试共用的装配与分型数据。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from coordination import (
    AuditLog,
    CaseRegistry,
    CollectionScheduler,
    ConsentBook,
    DisruptionService,
    DonorCommitments,
    DonorProfile,
    FixedClock,
    IdentityVault,
    Matcher,
    MilestoneBoard,
    Notifier,
)

UTC = timezone.utc
CST = timezone(timedelta(hours=8))  # 中国标准时间
EDT = timezone(timedelta(hours=-4))  # 美东夏令时

T0 = datetime(2026, 9, 19, 0, 0, tzinfo=UTC)


def hla(a, b, c, drb1, dqb1):
    return {"A": a, "B": b, "C": c, "DRB1": drb1, "DQB1": dqb1}


# 患者甲：与志愿者乙 10/10 全合
PATIENT_JIA_HLA = hla(
    ("A*01:01", "A*02:01"),
    ("B*07:02", "B*08:01"),
    ("C*07:01", "C*07:02"),
    ("DRB1*03:01", "DRB1*04:01"),
    ("DQB1*02:01", "DQB1*03:01"),
)
# 患者乙：与患者甲仅 DQB1 不同，与志愿者乙 8/10
PATIENT_YI_HLA = hla(
    ("A*01:01", "A*02:01"),
    ("B*07:02", "B*08:01"),
    ("C*07:01", "C*07:02"),
    ("DRB1*03:01", "DRB1*04:01"),
    ("DQB1*05:01", "DQB1*06:01"),
)

# (姓名, 分型, 初始同意范围)
DONOR_FIXTURES = [
    ("志愿者甲", hla(("A*01:01", "A*02:01"), ("B*07:02", "B*08:01"), ("C*07:01", "C*07:02"),
                    ("DRB1*03:01", "DRB1*07:01"), ("DQB1*02:01", "DQB1*04:01")), {"search"}),
    ("志愿者乙", PATIENT_JIA_HLA, {"search", "recontact", "exam"}),
    ("志愿者丙", hla(("A*01:01", "A*11:01"), ("B*07:02", "B*13:01"), ("C*07:01", "C*03:01"),
                    ("DRB1*03:01", "DRB1*11:01"), ("DQB1*02:01", "DQB1*04:02")),
     {"search", "recontact", "exam"}),
    ("志愿者丁", hla(("A*01:01", "A*03:01"), ("B*07:02", "B*08:01"), ("C*07:01", "C*07:02"),
                    ("DRB1*03:01", "DRB1*04:01"), ("DQB1*05:01", "DQB1*06:01")),
     {"search", "recontact", "exam"}),
    ("志愿者戊", hla(("A*01:01", "A*02:01"), ("B*07:02", "B*08:01"), ("C*07:01", "C*07:02"),
                    ("DRB1*15:01", "DRB1*16:01"), ("DQB1*06:02", "DQB1*02:01")),
     {"search", "recontact", "exam"}),
]


class World:
    """一套联调用例共用的领域服务装配。"""

    def __init__(self, start: datetime = T0):
        self.clock = FixedClock(start)
        self.audit = AuditLog(self.clock)
        self.vault = IdentityVault(
            b"joint-test-master-key",
            self.audit,
            self.clock,
            approvers={"sec-a", "sec-b"},
            unseal_ttl=timedelta(minutes=20),
        )
        self.consent = ConsentBook(self.clock, self.audit)
        self.notifier = Notifier(self.clock, self.audit)
        self.commitments = DonorCommitments()
        self.matcher = Matcher()
        self.cases = CaseRegistry(
            consent=self.consent,
            commitments=self.commitments,
            notifier=self.notifier,
            matcher=self.matcher,
            audit=self.audit,
            clock=self.clock,
            contact_cooling=timedelta(hours=12),
            collection_cooling=timedelta(hours=48),
        )
        self.scheduler = CollectionScheduler(self.clock, self.audit)
        self.board = MilestoneBoard(self.clock, self.audit)
        self.disruptions = DisruptionService(
            cases=self.cases,
            consent=self.consent,
            scheduler=self.scheduler,
            board=self.board,
            audit=self.audit,
            clock=self.clock,
            viability=timedelta(hours=36),
        )
        self.donor_alias: dict[str, str] = {}
        self.donor_hla: dict[str, dict] = {}
        self.donor_identity: dict[str, tuple[str, str]] = {}
        self.patient_alias: dict[str, str] = {}

    def enroll_donors(self, fixtures=DONOR_FIXTURES) -> None:
        for index, (name, typing, scope) in enumerate(fixtures):
            subject_id = self.vault.enroll("donor", name, f"1390000000{index}")
            alias = self.vault.alias_for(subject_id)
            self.consent.update_scope(alias, scope, actor="registry")
            self.donor_alias[name] = alias
            self.donor_hla[name] = typing
            self.donor_identity[name] = (name, f"1390000000{index}")

    def enroll_patient(self, name: str) -> str:
        subject_id = self.vault.enroll("patient", name, "patient-contact")
        alias = self.vault.alias_for(subject_id)
        self.patient_alias[name] = alias
        return alias

    def donor_pool(self) -> list[DonorProfile]:
        """按入库顺序生成候选池（业务侧只有别名与分型）。"""
        pool = []
        for index, (name, _, _) in enumerate(DONOR_FIXTURES):
            if name not in self.donor_alias:
                continue
            pool.append(
                DonorProfile(
                    donor_alias=self.donor_alias[name],
                    hla=self.donor_hla[name],
                    registered_at=T0 + timedelta(days=index),
                )
            )
        return pool
