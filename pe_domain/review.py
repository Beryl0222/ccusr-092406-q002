"""异常识别与教研复核闭环。

原则：系统只“发现可疑、提交证据、等待教研结论”，不做自动处罚、不公开通报。
异常班级进入“教研复核”状态，复核结论可能是：无异常（天气/调课有据）、
确有缺口（生成补课任务）、材料不足（退回补证）。

可识别的异常模式（均基于重建后的客观事件，可解释）：

- yin_yang_schedule   “阴阳课表”：方案有该场次，但无任何教师记录，
                       或实际内容与方案长期不符；
- plan_mismatch       实际场地/教师/技能与**当时生效的批准版本**不符
                       （证据带版本号，较晚版本绝不回写早期结论）；
- exam_drill_pattern  连续多场只练考试项目；
- free_play_pattern   连续多场整节自由活动；
- venue_conflict      实际场地观测与他班占用冲突；
- takeover_chain      临时占课连续发生（其他学科占用体育课）；
- capacity_breach     实际在场人数超过安全容量；
- signin_anomaly      重复签到或超时补签集中出现。

历史结论不可回写：已结案的裁定与所依据的方案版本永久冻结，后续方案修订
只能“追加更正”（append_correction），不能改写或撤销原结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from .events import Occasion


# 连续出现的阈值：达到即提示（不是处罚线，是教研介入线）
PATTERN_THRESHOLD = 2


class AnomalyKind(str, Enum):
    YIN_YANG = "yin_yang_schedule"
    PLAN_MISMATCH = "plan_mismatch"
    EXAM_DRILL = "exam_drill_pattern"
    FREE_PLAY = "free_play_pattern"
    VENUE_CONFLICT = "venue_conflict"
    TAKEOVER = "takeover_chain"
    CAPACITY = "capacity_breach"
    SIGNIN = "signin_anomaly"


class ReviewStatus(str, Enum):
    OPEN = "open"               # 待教研复核
    NEED_MORE = "need_more"     # 材料不足，退回补证
    CLEARED = "cleared"         # 复核无异常（替代/调课有据）
    MAKEUP_ORDERED = "makeup_ordered"  # 确认缺口，安排补课


@dataclass(frozen=True)
class Anomaly:
    class_id: str
    kind: AnomalyKind
    evidence: tuple[str, ...]  # 场次 key 或事件序号
    message: str
    plan_version: Optional[int] = None  # 该异常引用的当时生效方案版本


@dataclass(frozen=True)
class ReviewCorrection:
    """结案后的追加更正：只增不改，保留可追溯链。"""

    reviewer: str
    note: str
    basis_versions: tuple[Optional[int], ...]  # 更正所依据的方案版本
    anomalies: tuple[Anomaly, ...] = ()


@dataclass
class ReviewCase:
    case_id: str
    class_id: str
    anomalies: tuple[Anomaly, ...]
    status: ReviewStatus = ReviewStatus.OPEN
    conclusion: Optional[str] = None
    reviewer: Optional[str] = None
    # 结案时冻结的裁定依据：结论/复核人/异常快照（含方案版本），此后不可变
    basis: Optional[dict] = None
    corrections: list[ReviewCorrection] = field(default_factory=list)

    def resolve(self, status: ReviewStatus, reviewer: str, conclusion: str):
        if self.status not in (ReviewStatus.OPEN, ReviewStatus.NEED_MORE):
            raise ValueError("已结案的复核不能再次裁定")
        self.status = status
        self.reviewer = reviewer
        self.conclusion = conclusion
        # 冻结原依据：方案修订后重算也不得改写这里的版本引用
        self.basis = {
            "reviewer": reviewer,
            "conclusion": conclusion,
            "anomalies": self.anomalies,
            "plan_versions": tuple(sorted({
                a.plan_version for a in self.anomalies if a.plan_version is not None
            })),
        }

    def append_correction(
        self,
        reviewer: str,
        note: str,
        *,
        anomalies: tuple[Anomaly, ...] = (),
    ) -> ReviewCorrection:
        """对已结案复核追加更正。原结论与依据保留，更正只增不改。"""
        if self.basis is None:
            raise ValueError("尚未结案的复核应直接补证/裁定，而非追加更正")
        versions = tuple(sorted({
            a.plan_version for a in anomalies if a.plan_version is not None
        }))
        correction = ReviewCorrection(reviewer, note, versions, tuple(anomalies))
        self.corrections.append(correction)
        return correction


def detect_anomalies(class_id: str, reconstructed: dict) -> list[Anomaly]:
    """reconstructed 为 ledger.rebuild_class() 的输出。

    只做规则识别，不修改任何状态。所有结论都以场次的当时生效方案版本
    （rebuilt["basis_versions"]）为依据；无生效版本的场次（批准之前/
    批准间隙）不产生任何异常。
    """
    findings: list[Anomaly] = []
    sessions = reconstructed["sessions"]          # 按时间排序的会话
    missing = reconstructed["missing_occasions"]  # 方案有、无任何事件
    takeovers = reconstructed["takeovers"]
    flags_by_occasion = reconstructed["flags"]
    basis = reconstructed.get("basis_versions", {})
    versioned = reconstructed.get("versioned", False)
    uncovered = set(reconstructed.get("uncovered", ()))

    def version_of(key: str) -> Optional[int]:
        return basis.get(key)

    # 阴阳课表：方案场次完全无事件（排除已挂接补课、合规调课与无版本可核对场次）
    truly_missing = [
        k for k in missing
        if k not in reconstructed["made_up"]
        and k not in reconstructed["rescheduled"]
        and k not in uncovered
    ]
    if truly_missing:
        findings.append(Anomaly(
            class_id, AnomalyKind.YIN_YANG,
            tuple(sorted(truly_missing)),
            f"{len(truly_missing)} 个课表场次无任何授课/场地/学生事件",
            plan_version=version_of(truly_missing[0]) if versioned else None,
        ))

    # 实际安排与当时生效方案版本不符（场地/教师/技能），按版本归组
    mismatch_by_version: dict[Optional[int], list[str]] = {}
    for s in sessions:
        notes = s.notes or ()
        if any(n.startswith("plan_venue_mismatch")
               or n.startswith("plan_teacher_mismatch")
               or n.startswith("plan_skill_mismatch")
               or n.startswith("plan_slot_missing") for n in notes):
            mismatch_by_version.setdefault(s.plan_version, []).append(s.occasion.key())
    for ver, keys in sorted(mismatch_by_version.items(), key=lambda kv: (kv[0] is None, kv[0])):
        findings.append(Anomaly(
            class_id, AnomalyKind.PLAN_MISMATCH,
            tuple(sorted(keys)),
            f"实际场地/教师/技能与生效方案版本 v{ver} 不符" if ver else
            "实际场地/教师/技能与生效方案不符",
            plan_version=ver,
        ))

    # 连续应考训练 / 自由活动（无版本场次不计模式链）
    exam_run: list[str] = []
    free_run: list[str] = []
    for s in sessions:
        if s.occasion.key() in uncovered:
            exam_run, free_run = [], []
            continue
        exam_run = exam_run + [s.occasion.key()] if s.mode.value == "exam_drill" else []
        free_run = free_run + [s.occasion.key()] if s.mode.value == "free" else []
        if len(exam_run) == PATTERN_THRESHOLD:
            findings.append(Anomaly(
                class_id, AnomalyKind.EXAM_DRILL, tuple(exam_run),
                f"连续 {PATTERN_THRESHOLD} 场只练考试项目",
                plan_version=version_of(exam_run[-1]) if versioned else None,
            ))
        if len(free_run) == PATTERN_THRESHOLD:
            findings.append(Anomaly(
                class_id, AnomalyKind.FREE_PLAY, tuple(free_run),
                f"连续 {PATTERN_THRESHOLD} 场整节自由活动",
                plan_version=version_of(free_run[-1]) if versioned else None,
            ))

    # 场地冲突 / 容量
    conflicts = [s.occasion.key() for s in sessions if "venue_conflict_actual" in (s.notes or ())]
    if conflicts:
        findings.append(Anomaly(
            class_id, AnomalyKind.VENUE_CONFLICT, tuple(conflicts),
            "实际授课发生场地冲突",
            plan_version=version_of(conflicts[0]) if versioned else None,
        ))
    cap = [s.occasion.key() for s in sessions if "capacity_breach" in (s.notes or ())]
    if cap:
        findings.append(Anomaly(
            class_id, AnomalyKind.CAPACITY, tuple(cap),
            "实际在场人数超过安全容量",
            plan_version=version_of(cap[0]) if versioned else None,
        ))

    # 临时占课链
    chain_keys = [
        Occasion(t["slot_id"], t["week"]).key()
        for t in takeovers
        if Occasion(t["slot_id"], t["week"]).key() not in uncovered
    ]
    if len(chain_keys) >= PATTERN_THRESHOLD:
        findings.append(Anomaly(
            class_id, AnomalyKind.TAKEOVER, tuple(chain_keys),
            f"{len(chain_keys)} 起连续临时占课",
            plan_version=version_of(chain_keys[-1]) if versioned else None,
        ))

    # 签到异常（重复/超时补签）
    signin_hits = sorted(
        occ for occ, flags in flags_by_occasion.items()
        if occ not in uncovered
        and any(f.startswith(("duplicate_signin", "offline_late")) for f in flags)
    )
    if signin_hits:
        findings.append(Anomaly(
            class_id, AnomalyKind.SIGNIN, tuple(signin_hits),
            f"{len(signin_hits)} 个场次出现重复或超时补签",
            plan_version=version_of(signin_hits[0]) if versioned else None,
        ))

    return findings


class ReviewBoard:
    """复核台账：异常班级先入复核，任何对外结论只在结案后产生。"""

    def __init__(self):
        self._cases: dict[str, ReviewCase] = {}
        self._open_by_class: dict[str, str] = {}

    def open_case(self, class_id: str, anomalies: list[Anomaly]) -> ReviewCase:
        if class_id in self._open_by_class:
            return self._cases[self._open_by_class[class_id]]
        case_id = f"RC-{len(self._cases) + 1:04d}"
        case = ReviewCase(case_id, class_id, tuple(anomalies))
        self._cases[case_id] = case
        self._open_by_class[class_id] = case_id
        return case

    def resolve(self, case_id: str, status: ReviewStatus, reviewer: str, conclusion: str) -> ReviewCase:
        case = self._cases[case_id]
        case.resolve(status, reviewer, conclusion)
        self._open_by_class.pop(case.class_id, None)
        return case

    def append_correction(
        self, case_id: str, reviewer: str, note: str,
        *, anomalies: tuple[Anomaly, ...] = (),
    ) -> ReviewCorrection:
        """对已结案复核追加更正（原依据保留）。"""
        return self._cases[case_id].append_correction(reviewer, note, anomalies=anomalies)

    def get(self, case_id: str) -> ReviewCase:
        return self._cases[case_id]

    def open_classes(self) -> tuple[str, ...]:
        return tuple(sorted(self._open_by_class))
