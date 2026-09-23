"""学期方案的版本管理与提交前校验。

校验规则（全部为可解释的结构性检查，不打分排队）：

1. 每天一节体育课：每个班级每天最多排一节 PE_CLASS，且每周不少于 5 节；
2. 场地冲突：同一场地在同一 (周次, 星期) 不得被两个时段占用；
3. 教师冲突：同一教师同一时间不得跨场地授课；
4. 安全容量：班级人数不得超过场地安全容量；
5. 资质匹配：授课教师必须具备该时段技能目标对应的资质；
6. 技能目标完整性：每个 slot 引用的 skill_code 必须存在对应 SkillGoal；
7. 室外时段必须给出降雨与高温替代，且替代场地为室内、容量足够。

校验不通过返回结构化问题清单，由学校修改后重新提交，不产生任何处罚记录。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ActivityKind, SemesterPlan, Venue, VenueType


@dataclass(frozen=True)
class PlanIssue:
    code: str
    slot_id: str | None
    message: str


def _week_occasions(slot) -> tuple[int, ...]:
    if slot.week_parity == "odd":
        return tuple(range(1, 20, 2))
    if slot.week_parity == "even":
        return tuple(range(2, 20, 2))
    return tuple(range(1, 20))


def validate_plan(
    plan: SemesterPlan,
    venues: dict[str, Venue],
    teachers: dict,
) -> list[PlanIssue]:
    """返回问题清单；空清单表示可提交。"""
    issues: list[PlanIssue] = []
    skill_codes = {g.skill_code for g in plan.skill_goals}

    def add(code: str, message: str, slot_id: str | None = None):
        issues.append(PlanIssue(code, slot_id, message))

    # 规则 6：技能目标存在性
    for slot in plan.slots:
        if slot.skill_code not in skill_codes:
            add("skill_missing", f"时段 {slot.slot_id} 引用的技能目标 {slot.skill_code} 未定义", slot.slot_id)
        venue = venues.get(slot.venue_id)
        teacher = teachers.get(slot.teacher_id)

        # 场地与容量
        if venue is None:
            add("venue_missing", f"时段 {slot.slot_id} 使用了不存在的场地 {slot.venue_id}", slot.slot_id)
        elif slot.headcount > venue.safe_capacity:
            add(
                "capacity_exceeded",
                f"班级人数 {slot.headcount} 超过场地 {venue.name} 安全容量 {venue.safe_capacity}",
                slot.slot_id,
            )

        # 教师与资质
        if teacher is None:
            add("teacher_missing", f"时段 {slot.slot_id} 指定了不存在的教师 {slot.teacher_id}", slot.slot_id)
        elif slot.skill_code in skill_codes and not teacher.qualified_for(slot.skill_code):
            add(
                "qualification_mismatch",
                f"教师 {teacher.name} 不具备 {slot.skill_code} 教学资质",
                slot.slot_id,
            )

        # 规则 7：室外时段的天气替代
        if venue is not None and venue.kind is VenueType.OUTDOOR and slot.kind in (
            ActivityKind.PE_CLASS,
            ActivityKind.CLASS_MATCH,
        ):
            conditions = {a.condition for a in slot.weather_alternatives}
            for cond in ("rain", "heat"):
                if cond not in conditions:
                    add("weather_alt_missing", f"室外时段缺少 {cond} 替代方案", slot.slot_id)
            for alt in slot.weather_alternatives:
                alt_venue = venues.get(alt.venue_id)
                if alt_venue is None:
                    add("weather_alt_venue_missing", f"替代场地 {alt.venue_id} 不存在", slot.slot_id)
                elif alt_venue.kind is not VenueType.INDOOR:
                    add("weather_alt_not_indoor", f"{alt.condition} 替代场地必须为室内", slot.slot_id)
                elif slot.headcount > alt_venue.safe_capacity:
                    add("weather_alt_capacity", f"替代场地容量不足（{slot.headcount}>{alt_venue.safe_capacity}）", slot.slot_id)

    # 规则 1：每天一节体育课（按班级 × 星期统计；同一星期的 odd/even 视为排了）
    pe_slots = [s for s in plan.slots if s.kind is ActivityKind.PE_CLASS]
    by_class: dict[str, set[int]] = {}
    for slot in pe_slots:
        by_class.setdefault(slot.class_id, set()).add(slot.weekday)
    all_classes = {s.class_id for s in plan.slots}
    for class_id in sorted(all_classes):
        days = by_class.get(class_id, set())
        if len(days) < 5:
            add("daily_pe_shortfall", f"班级 {class_id} 每周仅安排 {len(days)} 天体育课，应不少于 5 天")

    # 重复排课：同班同天多节体育课
    seen_class_day: dict[tuple[str, int], str] = {}
    for slot in pe_slots:
        key = (slot.class_id, slot.weekday)
        if key in seen_class_day:
            add("double_pe_same_day", f"班级 {slot.class_id} 星期{slot.weekday} 重复排体育课", slot.slot_id)
        seen_class_day[key] = slot.slot_id

    # 规则 2/3：场地冲突与教师冲突（按周次展开到奇偶周）
    venue_busy: dict[tuple[str, int, int], str] = {}
    teacher_busy: dict[tuple[str, int, int], str] = {}
    for slot in plan.slots:
        for week in _week_occasions(slot):
            vkey = (slot.venue_id, week, slot.weekday)
            if vkey in venue_busy:
                add("venue_conflict", f"场地第 {week} 周星期{slot.weekday} 与时段 {venue_busy[vkey]} 冲突", slot.slot_id)
            else:
                venue_busy[vkey] = slot.slot_id
            tkey = (slot.teacher_id, week, slot.weekday)
            if tkey in teacher_busy:
                add("teacher_conflict", f"教师第 {week} 周星期{slot.weekday} 与时段 {teacher_busy[tkey]} 冲突", slot.slot_id)
            else:
                teacher_busy[tkey] = slot.slot_id

    return issues


class PlanRegistry:
    """方案版本库：提交即分配递增版本号，批准后冻结，修订生成新版本。

    旧版本永不删除——识别“阴阳课表”时需要把实际授课事件与当时生效的
    批准版本逐条对照。
    """

    def __init__(self):
        self._plans: dict[tuple[str, str], list[SemesterPlan]] = {}

    def submit(
        self,
        plan: SemesterPlan,
        venues: dict[str, Venue],
        teachers: dict,
        *,
        submitted_by: str,
    ) -> SemesterPlan:
        issues = validate_plan(plan, venues, teachers)
        if issues:
            raise ValueError(f"方案校验未通过，共 {len(issues)} 项问题") from None
        key = (plan.school_id, plan.semester)
        history = self._plans.setdefault(key, [])
        version = len(history) + 1
        submitted = plan.with_status(version=version, status="submitted", submitted_by=submitted_by)
        history.append(submitted)
        return submitted

    def approve(self, school_id: str, semester: str, version: int) -> SemesterPlan:
        plan = self.get(school_id, semester, version)
        idx = self._plans[(school_id, semester)].index(plan)
        approved = plan.with_status(status="approved")
        history = self._plans[(school_id, semester)]
        history[idx] = approved
        # 旧批准版本标记为 superseded
        for i, older in enumerate(history):
            if i != idx and older.status == "approved":
                history[i] = older.with_status(status="superseded")
        return approved

    def get(self, school_id: str, semester: str, version: int | None = None) -> SemesterPlan:
        history = self._plans[(school_id, semester)]
        if version is None:
            for plan in reversed(history):
                if plan.status == "approved":
                    return plan
            raise ValueError("该学期尚无已批准方案")
        return history[version - 1]

    def effective_on(self, school_id: str, semester: str, *, at_index: int) -> SemesterPlan:
        """返回某次提交序号之前已批准的版本（事件对照用）。

        at_index 为账本事件序号；真实系统中改用事件时间戳。
        """
        history = self._plans[(school_id, semester)]
        approved = [p for p in history if p.status in ("approved", "superseded")]
        if not approved:
            raise ValueError("当时无生效方案")
        return approved[-1]

    def history(self, school_id: str, semester: str) -> tuple[SemesterPlan, ...]:
        return tuple(self._plans.get((school_id, semester), []))
