"""异常识别与教研复核闭环。

原则：系统只“发现可疑、提交证据、等待教研结论”，不做自动处罚、不公开通报。
异常班级进入“教研复核”状态，复核结论可能是：无异常（天气/调课有据）、
确有缺口（生成补课任务）、材料不足（退回补证）。

可识别的异常模式（均基于重建后的客观事件，可解释）：

- yin_yang_schedule   “阴阳课表”：方案有该场次，但无任何教师记录，
                       或实际内容与方案长期不符；
- plan_deviation     实际授课与**场次发生当时生效的批准版本**不一致
                       （场地、教师、技能目标、天气应急替代）；
- exam_drill_pattern  连续多场只练考试项目；
- free_play_pattern   连续多场整节自由活动；
- venue_conflict      实际场地观测与他班占用冲突；
- takeover_chain      临时占课连续发生（其他学科占用体育课）；
- capacity_breach     实际在场人数超过安全容量；
- signin_anomaly      重复签到或超时补签集中出现。

历史结论保护：方案修订触发重算时，只处理处于修订影响区间的场次；
已结案的复核**不被二次裁定**——原结论与原依据版本完整保留，
新材料只能以 :class:`ReviewCorrection` 追加。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional


# 连续出现的阈值：达到即提示（不是处罚线，是教研介入线）
PATTERN_THRESHOLD = 2


class AnomalyKind(str, Enum):
    YIN_YANG = "yin_yang_schedule"
    PLAN_DEVIATION = "plan_deviation"
    EXAM_DRILL = "exam_drill_pattern"
    FREE_PLAY = "free_play_pattern"
    VENUE_CONFLICT = "venue_conflict"
    TAKEOVER = "takeover_chain"
    CAPACITY = "capacity_breach"
    SIGNIN = "signin_anomaly"


# plan_deviation 注记维度 -> 人话说明
_DEVIATION_DIMENSIONS = {
    "venue": "实际场地与当时批准版本不符",
    "teacher": "实际授课教师与当时批准版本不符",
    "skill": "实际技能目标与当时批准版本不符",
    "weather_alt": "实际应急替代场地与当时批准版本不符",
    "slot_not_in_version": "该场次不在当时批准版本中",
}


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
    plan_versions: tuple[int, ...] = ()  # 证据引用的方案版本（同一版本口径）


@dataclass(frozen=True)
class ReviewCorrection:
    """已结案复核的追加更正：不覆盖原结论，只追加新依据。

    方案修订只重算影响区间内场次后，把新增/消解的异常作为更正附在原案上，
    教研视图同时呈现原结论（含原依据版本）与更正链。
    """

    correction_id: str
    reason: str
    added: tuple[Anomaly, ...]
    withdrawn: tuple[Anomaly, ...]  # 重算后不再成立的旧异常（仅登记，不抹除历史）
    basis_versions: tuple[int, ...]
    note: str = ""


@dataclass
class ReviewCase:
    case_id: str
    class_id: str
    anomalies: tuple[Anomaly, ...]
    status: ReviewStatus = ReviewStatus.OPEN
    conclusion: Optional[str] = None
    reviewer: Optional[str] = None
    basis_versions: tuple[int, ...] = ()  # 结案所依据的方案版本
    corrections: list[ReviewCorrection] = field(default_factory=list)

    def resolve(self, status: ReviewStatus, reviewer: str, conclusion: str,
                basis_versions: tuple[int, ...] = ()):
        if self.status not in (ReviewStatus.OPEN, ReviewStatus.NEED_MORE):
            raise ValueError("已结案的复核不能再次裁定")
        self.status = status
        self.reviewer = reviewer
        self.conclusion = conclusion
        self.basis_versions = tuple(sorted(set(basis_versions)))

    def append_correction(self, correction: ReviewCorrection):
        """已结案案件只能追加更正，不能改判。"""
        if self.status in (ReviewStatus.OPEN, ReviewStatus.NEED_MORE):
            raise ValueError("未结案复核应直接重算，不追加更正")
        if any(c.correction_id == correction.correction_id for c in self.corrections):
            raise ValueError(f"更正 {correction.correction_id} 已存在（更正只追加、幂等）")
        self.corrections.append(correction)

    def effective_anomalies(self) -> tuple[Anomaly, ...]:
        """当前仍成立的异常：原异常减去各次更正中登记消解的，再加新增的。"""
        withdrawn_ids = {
            (a.kind, a.evidence)
            for c in self.corrections for a in c.withdrawn
        }
        live = [
            a for a in self.anomalies
            if (a.kind, a.evidence) not in withdrawn_ids
        ]
        live.extend(a for c in self.corrections for a in c.added)
        return tuple(live)


def detect_anomalies(
    class_id: str,
    reconstructed: dict,
    *,
    scope: frozenset[str] | None = None,
) -> list[Anomaly]:
    """reconstructed 为 ledger 版本化重放输出。

    scope：只识别这些 occasion key（方案修订的影响区间）；缺省为全部。
    只做规则识别，不修改任何状态。
    """
    def in_scope(key: str) -> bool:
        return scope is None or key in scope

    findings: list[Anomaly] = []
    sessions = reconstructed["sessions"]
    missing = reconstructed["missing_occasions"]
    takeovers = reconstructed["takeovers"]
    flags_by_occasion = reconstructed["flags"]
    versions_by_occasion = reconstructed.get("plan_versions", {})

    def versions_for(keys) -> tuple[int, ...]:
        return tuple(sorted({
            versions_by_occasion.get(k, 0) for k in keys
            if versions_by_occasion.get(k)
        }))

    # 阴阳课表：方案场次完全无事件（排除已挂接补课与合规调课；
    # 批准间隙的 no_plan 场次不在 missing 中，不会被后来版本回写）
    truly_missing = [
        k for k in missing
        if k not in reconstructed["made_up"] and k not in reconstructed["rescheduled"]
        and in_scope(k)
    ]
    if truly_missing:
        findings.append(Anomaly(
            class_id, AnomalyKind.YIN_YANG,
            tuple(sorted(truly_missing)),
            f"{len(truly_missing)} 个课表场次无任何授课/场地/学生事件",
            versions_for(truly_missing),
        ))

    # 与当时生效版本的偏离：按维度聚合
    deviations: dict[str, list[str]] = {}
    for s in sessions:
        key = s.occasion.key()
        if not in_scope(key):
            continue
        for note in s.notes or ():
            if not note.startswith("plan_deviation:"):
                continue
            dim = note.split(":", 2)[1]
            deviations.setdefault(dim, []).append(key)
    for dim, keys in deviations.items():
        keys = sorted(set(keys))
        findings.append(Anomaly(
            class_id, AnomalyKind.PLAN_DEVIATION, tuple(keys),
            _DEVIATION_DIMENSIONS.get(dim, dim),
            versions_for(keys),
        ))

    # 连续应考训练 / 自由活动
    exam_run: list[str] = []
    free_run: list[str] = []
    for s in sessions:
        key = s.occasion.key()
        exam_run = exam_run + [key] if s.mode.value == "exam_drill" else []
        free_run = free_run + [key] if s.mode.value == "free" else []
        if len(exam_run) == PATTERN_THRESHOLD and all(in_scope(k) for k in exam_run):
            findings.append(Anomaly(class_id, AnomalyKind.EXAM_DRILL, tuple(exam_run),
                                    f"连续 {PATTERN_THRESHOLD} 场只练考试项目",
                                    versions_for(exam_run)))
        if len(free_run) == PATTERN_THRESHOLD and all(in_scope(k) for k in free_run):
            findings.append(Anomaly(class_id, AnomalyKind.FREE_PLAY, tuple(free_run),
                                    f"连续 {PATTERN_THRESHOLD} 场整节自由活动",
                                    versions_for(free_run)))

    # 场地冲突 / 容量
    conflicts = [s.occasion.key() for s in sessions
                 if "venue_conflict_actual" in (s.notes or ()) and in_scope(s.occasion.key())]
    if conflicts:
        findings.append(Anomaly(class_id, AnomalyKind.VENUE_CONFLICT, tuple(conflicts),
                                "实际授课发生场地冲突", versions_for(conflicts)))
    cap = [s.occasion.key() for s in sessions
           if "capacity_breach" in (s.notes or ()) and in_scope(s.occasion.key())]
    if cap:
        findings.append(Anomaly(class_id, AnomalyKind.CAPACITY, tuple(cap),
                                "实际在场人数超过安全容量", versions_for(cap)))

    # 临时占课链
    from .events import Occasion
    takeover_hits = [
        Occasion(t["slot_id"], t["week"]).key()
        for t in takeovers
        if in_scope(Occasion(t["slot_id"], t["week"]).key())
    ]
    if len(takeover_hits) >= PATTERN_THRESHOLD:
        findings.append(Anomaly(class_id, AnomalyKind.TAKEOVER, tuple(takeover_hits),
                                f"{len(takeover_hits)} 起连续临时占课",
                                versions_for(takeover_hits)))

    # 签到异常（重复/超时补签）
    signin_hits = sorted(
        occ for occ, flags in flags_by_occasion.items()
        if in_scope(occ)
        and any(f.startswith(("duplicate_signin", "offline_late")) for f in flags)
    )
    if signin_hits:
        findings.append(Anomaly(class_id, AnomalyKind.SIGNIN, tuple(signin_hits),
                                f"{len(signin_hits)} 个场次出现重复或超时补签",
                                versions_for(signin_hits)))

    return findings


class ReviewBoard:
    """复核台账：异常班级先入复核，任何对外结论只在结案后产生。

    方案修订的影响区间重算结果通过 :meth:`reconcile` 进入台账：
    未结案直接换证据重开；已结案原结论保留、只追加 :class:`ReviewCorrection`。
    """

    def __init__(self):
        self._cases: dict[str, ReviewCase] = {}
        self._open_by_class: dict[str, str] = {}
        self._correction_seq = 0

    def open_case(self, class_id: str, anomalies: list[Anomaly]) -> ReviewCase:
        if class_id in self._open_by_class:
            return self._cases[self._open_by_class[class_id]]
        case_id = f"RC-{len(self._cases) + 1:04d}"
        case = ReviewCase(case_id, class_id, tuple(anomalies))
        self._cases[case_id] = case
        self._open_by_class[class_id] = case_id
        return case

    def resolve(self, case_id: str, status: ReviewStatus, reviewer: str,
                conclusion: str, basis_versions: tuple[int, ...] = ()) -> ReviewCase:
        case = self._cases[case_id]
        case.resolve(status, reviewer, conclusion, basis_versions)
        self._open_by_class.pop(case.class_id, None)
        return case

    def reconcile(
        self,
        class_id: str,
        new_anomalies: list[Anomaly],
        *,
        reason: str,
        basis_versions: tuple[int, ...],
        scope: frozenset[str] | None = None,
    ) -> tuple[Optional[ReviewCase], Optional[ReviewCorrection]]:
        """按影响区间重算结果更新台账。

        - 无历史案件：有异常则开案；
        - 未结案：以新证据替换（重算覆盖）；
        - 已结案：原结论不动，返回/落账一条追加更正
          （新增异常 + 区间内不再成立的旧异常）。

        scope 之外的历史异常一律不参与比较——修订只重算影响区间，
        区间外已结案的依据与结论原样保留。
        """
        def touches_scope(anomaly: Anomaly) -> bool:
            if scope is None:
                return True
            return any(k in scope for k in anomaly.evidence)

        case_id = self._open_by_class.get(class_id)
        if case_id is not None:
            case = self._cases[case_id]
            if scope is None:
                case.anomalies = tuple(new_anomalies)
            else:
                kept: list[Anomaly] = []
                for a in case.anomalies:
                    if not touches_scope(a):
                        kept.append(a)
                        continue
                    # 跨区间的聚合异常：剔除区间内证据，区间外部分保留
                    outside = tuple(k for k in a.evidence if k not in scope)
                    if outside:
                        kept.append(replace(a, evidence=outside))
                case.anomalies = tuple(kept) + tuple(new_anomalies)
            return case, None
        historical = [c for c in self._cases.values() if c.class_id == class_id]
        if not historical:
            # 无历史案件且区间内无异常：无操作（修订后主动重算的正常结果）
            if not new_anomalies:
                return None, None
            return self.open_case(class_id, new_anomalies), None

        case = historical[-1]  # 同班最近一案（已结案）
        prior = tuple(a for a in case.effective_anomalies() if touches_scope(a))
        new_sig = {(a.kind, a.evidence) for a in new_anomalies}
        old_sig = {(a.kind, a.evidence) for a in prior}
        withdrawn = tuple(a for a in prior if (a.kind, a.evidence) not in new_sig)
        added = tuple(a for a in new_anomalies if (a.kind, a.evidence) not in old_sig)
        if not added and not withdrawn:
            return case, None
        self._correction_seq += 1
        correction = ReviewCorrection(
            correction_id=f"RXC-{self._correction_seq:04d}",
            reason=reason, added=added, withdrawn=withdrawn,
            basis_versions=tuple(sorted(set(basis_versions))),
        )
        case.append_correction(correction)
        return case, correction

    def get(self, case_id: str) -> ReviewCase:
        return self._cases[case_id]

    def cases_for(self, class_id: str) -> tuple[ReviewCase, ...]:
        return tuple(c for c in self._cases.values() if c.class_id == class_id)

    def snapshot(self) -> "ReviewBoard":
        """深拷贝台账（乱序归一/恢复测试时不共享可变案件）。"""
        import copy
        clone = ReviewBoard()
        clone._cases = copy.deepcopy(self._cases)
        clone._open_by_class = dict(self._open_by_class)
        clone._correction_seq = self._correction_seq
        return clone

    def open_classes(self) -> tuple[str, ...]:
        return tuple(sorted(self._open_by_class))
