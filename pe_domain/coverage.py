"""“教会、勤练、常赛”可解释覆盖规则。

不以单一体测分数评价学生；覆盖只看“经交叉确认的实际授课事件”是否兑现
方案中各技能目标的计划周次。每项判定都输出依据（场次列表）与缺口原因。

口径：

- 教会：在该技能 teach_weeks 内（或挂接该周缺课的补课），至少有 1 场
  经确认、且实际教授该技能的体育课。自由活动/纯应考训练不计。
- 勤练：practice_weeks 内，体育课/大课间/课后服务中实际练习该技能的
  确认场次达到计划数（方案中这些周的相关 slot 数）。
- 常赛：match_weeks 内，经确认的班级赛事场次达到计划数。
- 调课：以审批通过的 ScheduleChange 为准，按调整后的时间核对。
- 补课：MAKEUP 场次回填其 makeup_for 指向的原场次，原场次记为补课后完成。
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import SessionMode
from .models import ActivityKind

# 可产生“教会/勤练”技能覆盖的形态（自由活动、纯应考训练排除）
SKILL_GRANTING_MODES = {
    SessionMode.NORMAL,
    SessionMode.RAIN_ALT,
    SessionMode.HEAT_ALT,
    SessionMode.RESCHEDULED,
    SessionMode.MAKEUP,
}


@dataclass(frozen=True)
class SkillCoverage:
    skill_code: str
    taught: bool
    practiced_ratio: float        # 0.0-1.0
    matched_ratio: float
    evidence: tuple[str, ...]     # 命中的场次 key
    gaps: tuple[str, ...]         # 可解释缺口


@dataclass(frozen=True)
class ClassCoverage:
    class_id: str
    total_occasions: int
    completed: int
    pending_makeup: int
    in_review: int
    skill_coverage: tuple[SkillCoverage, ...]
    # 本班核对所引用的当时生效方案版本（家长视图/公众聚合/教研异常共用同一来源）
    plan_versions: tuple[int, ...] = ()
    unverified: int = 0  # 当时无生效方案的场次（不计入任何分母，也不判缺口）

    @property
    def completion_ratio(self) -> float:
        return round(self.completed / self.total_occasions, 3) if self.total_occasions else 0.0


def _week_of(occasion_key: str) -> int:
    return int(occasion_key.rsplit("w", 1)[1])


def compute_skill_coverage(goal, confirmed_sessions, *, rescheduled_weeks=None):
    """confirmed_sessions: ledger 重建后的已确认会话列表（对象含 occasion/taught_skill/mode/kind）。

    rescheduled_weeks: 调课生效后，原 slot 在某周改期的集合（视为该周仍有安排），
    用于避免把合规调课误判为缺口。
    """
    rescheduled_weeks = rescheduled_weeks or set()
    evidence: list[str] = []
    taught_weeks_hit: set[int] = set()
    practice_weeks_hit: set[int] = set()
    match_weeks_hit: set[int] = set()
    practiced_planned = len(goal.practice_weeks)
    matched_planned = len(goal.match_weeks)
    gaps: list[str] = []

    for s in confirmed_sessions:
        week = _week_of(s.occasion.key())
        key = s.occasion.key()
        if s.taught_skill != goal.skill_code or s.mode not in SKILL_GRANTING_MODES:
            continue
        # 补课按其挂接原场次所在周归类
        if s.mode == SessionMode.MAKEUP and s.makeup_for is not None:
            week = _week_of(s.makeup_for.key())
        if s.kind is ActivityKind.PE_CLASS:
            if week in goal.teach_weeks:
                taught_weeks_hit.add(week)
                evidence.append(f"teach:{key}")
            if week in goal.practice_weeks:
                practice_weeks_hit.add(week)
                evidence.append(f"practice:{key}")
        elif s.kind in (ActivityKind.DAILY_BREAK, ActivityKind.AFTER_SCHOOL):
            if week in goal.practice_weeks:
                practice_weeks_hit.add(week)
                evidence.append(f"practice:{key}")
        elif s.kind is ActivityKind.CLASS_MATCH and week in goal.match_weeks:
            match_weeks_hit.add(week)
            evidence.append(f"match:{key}")

    taught = bool(taught_weeks_hit)
    practiced = len(practice_weeks_hit & set(goal.practice_weeks))
    matched = len(match_weeks_hit & set(goal.match_weeks))

    if not taught:
        gaps.append(f"技能 {goal.skill_code} 在计划教授周 {sorted(goal.teach_weeks)} 内无确认授课")
    if practiced < practiced_planned:
        gaps.append(f"勤练缺口：{practiced}/{practiced_planned} 周")
    if matched < matched_planned:
        gaps.append(f"常赛缺口：{matched}/{matched_planned} 场")

    return SkillCoverage(
        skill_code=goal.skill_code,
        taught=taught,
        practiced_ratio=min(1.0, round(practiced / practiced_planned, 3)) if practiced_planned else 1.0,
        matched_ratio=min(1.0, round(matched / matched_planned, 3)) if matched_planned else 1.0,
        evidence=tuple(sorted(evidence)),
        gaps=tuple(gaps),
    )


def compute_class_coverage(
    class_id: str,
    rebuilt: dict,
    skill_goals,
) -> ClassCoverage:
    """rebuilt 为 ledger.rebuild_class() 的输出。

    无生效方案（批准之前/批准间隙）的场次不计入分母、不判缺口、不产生技能
    覆盖；版本化重放时班级覆盖率携带当时生效的方案版本集合，供三视图共用。
    """
    states = rebuilt["states"]
    uncovered = set(rebuilt.get("uncovered", ()))
    versioned = rebuilt.get("versioned", False)
    scoped_states = {
        k: v for k, v in states.items()
        if k not in uncovered
    }
    planned_occasions = {k: {"state": v} for k, v in scoped_states.items()}
    completed = sum(1 for v in planned_occasions.values() if v["state"] == "completed")
    pending = sum(
        1 for v in planned_occasions.values()
        if v["state"] in ("taken_over", "missing", "weather_pending")
    )
    in_review = sum(1 for v in planned_occasions.values() if v["state"] == "in_review")
    # 无版本可核对的确认场次不授予任何技能覆盖
    sessions = [
        s for s in rebuilt["sessions"]
        if s.confirmed and (not versioned or s.plan_version is not None)
    ]
    skill_cov = tuple(
        compute_skill_coverage(goal, sessions)
        for goal in skill_goals
    )
    basis = rebuilt.get("basis_versions", {})
    plan_versions = tuple(sorted({
        ver for ver in basis.values() if ver is not None
    }))
    return ClassCoverage(
        class_id=class_id,
        total_occasions=len(planned_occasions),
        completed=completed,
        pending_makeup=pending,
        in_review=in_review,
        skill_coverage=skill_cov,
        plan_versions=plan_versions,
        unverified=len(uncovered),
    )
