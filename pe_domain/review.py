"""异常识别与教研复核闭环。

原则：系统只“发现可疑、提交证据、等待教研结论”，不做自动处罚、不公开通报。
异常班级进入“教研复核”状态，复核结论可能是：无异常（天气/调课有据）、
确有缺口（生成补课任务）、材料不足（退回补证）。

可识别的异常模式（均基于重建后的客观事件，可解释）：

- yin_yang_schedule   “阴阳课表”：方案有该场次，但无任何教师记录，
                       或实际内容与方案长期不符；
- exam_drill_pattern  连续多场只练考试项目；
- free_play_pattern   连续多场整节自由活动；
- venue_conflict      实际场地观测与他班占用冲突；
- takeover_chain      临时占课连续发生（其他学科占用体育课）；
- capacity_breach     实际在场人数超过安全容量；
- signin_anomaly      重复签到或超时补签集中出现。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


# 连续出现的阈值：达到即提示（不是处罚线，是教研介入线）
PATTERN_THRESHOLD = 2


class AnomalyKind(str, Enum):
    YIN_YANG = "yin_yang_schedule"
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


@dataclass
class ReviewCase:
    case_id: str
    class_id: str
    anomalies: tuple[Anomaly, ...]
    status: ReviewStatus = ReviewStatus.OPEN
    conclusion: Optional[str] = None
    reviewer: Optional[str] = None

    def resolve(self, status: ReviewStatus, reviewer: str, conclusion: str):
        if self.status not in (ReviewStatus.OPEN, ReviewStatus.NEED_MORE):
            raise ValueError("已结案的复核不能再次裁定")
        self.status = status
        self.reviewer = reviewer
        self.conclusion = conclusion


def detect_anomalies(class_id: str, reconstructed: dict) -> list[Anomaly]:
    """reconstructed 为 ledger.rebuild_class() 的输出。

    只做规则识别，不修改任何状态。
    """
    findings: list[Anomaly] = []
    sessions = reconstructed["sessions"]          # 按时间排序的会话
    missing = reconstructed["missing_occasions"]  # 方案有、无任何事件
    takeovers = reconstructed["takeovers"]
    flags_by_occasion = reconstructed["flags"]

    # 阴阳课表：方案场次完全无事件（排除已挂接补课与合规调课）
    truly_missing = [
        k for k in missing
        if k not in reconstructed["made_up"] and k not in reconstructed["rescheduled"]
    ]
    if truly_missing:
        findings.append(Anomaly(
            class_id, AnomalyKind.YIN_YANG,
            tuple(sorted(truly_missing)),
            f"{len(truly_missing)} 个课表场次无任何授课/场地/学生事件",
        ))

    # 连续应考训练 / 自由活动
    exam_run: list[str] = []
    free_run: list[str] = []
    for s in sessions:
        exam_run = exam_run + [s.occasion.key()] if s.mode.value == "exam_drill" else []
        free_run = free_run + [s.occasion.key()] if s.mode.value == "free" else []
        if len(exam_run) == PATTERN_THRESHOLD:
            findings.append(Anomaly(class_id, AnomalyKind.EXAM_DRILL, tuple(exam_run),
                                    f"连续 {PATTERN_THRESHOLD} 场只练考试项目"))
        if len(free_run) == PATTERN_THRESHOLD:
            findings.append(Anomaly(class_id, AnomalyKind.FREE_PLAY, tuple(free_run),
                                    f"连续 {PATTERN_THRESHOLD} 场整节自由活动"))

    # 场地冲突 / 容量
    conflicts = [s.occasion.key() for s in sessions if "venue_conflict_actual" in (s.notes or ())]
    if conflicts:
        findings.append(Anomaly(class_id, AnomalyKind.VENUE_CONFLICT, tuple(conflicts),
                                "实际授课发生场地冲突"))
    cap = [s.occasion.key() for s in sessions if "capacity_breach" in (s.notes or ())]
    if cap:
        findings.append(Anomaly(class_id, AnomalyKind.CAPACITY, tuple(cap),
                                "实际在场人数超过安全容量"))

    # 临时占课链
    if len(takeovers) >= PATTERN_THRESHOLD:
        from .events import Occasion
        evidence = tuple(
            Occasion(t["slot_id"], t["week"]).key()
            for t in takeovers
        )
        findings.append(Anomaly(class_id, AnomalyKind.TAKEOVER, evidence,
                                f"{len(takeovers)} 起连续临时占课"))

    # 签到异常（重复/超时补签）
    signin_hits = sorted(
        occ for occ, flags in flags_by_occasion.items()
        if any(f.startswith(("duplicate_signin", "offline_late")) for f in flags)
    )
    if signin_hits:
        findings.append(Anomaly(class_id, AnomalyKind.SIGNIN, tuple(signin_hits),
                                f"{len(signin_hits)} 个场次出现重复或超时补签"))

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

    def get(self, case_id: str) -> ReviewCase:
        return self._cases[case_id]

    def open_classes(self) -> tuple[str, ...]:
        return tuple(sorted(self._open_by_class))
