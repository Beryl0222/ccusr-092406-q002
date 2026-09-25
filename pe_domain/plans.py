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

from .models import ActivityKind, LedgerPosition, SemesterPlan, Venue, VenueType


class NoApprovedPlanAtPosition(LookupError):
    """给定账本位置之前（或当时）该学期尚无已批准且未被取代的方案。

    这是“批准间隙”的正常领域结果：早期事件不能被后来才批准的版本回写核对，
    这些场次标记为“当时无生效方案”，不计阴阳课表异常。
    """


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


# ---------------------------------------------------------------- 版本影响面
#
# 方案修订可能改变四类核对依据：场地、教师、场地应急替代（降雨/高温）、
# 技能目标。只有处于修订影响区间（新版本生效起）且命中受影响 slot 的场次
# 才需要重算；历史场次维持原版本核对。

_AFFECTED_FIELDS = ("venue_id", "teacher_id", "skill_code", "weather_alternatives")


def affected_slots(old: SemesterPlan, new: SemesterPlan) -> frozenset[str]:
    """返回新旧版本间四类依据发生变化的 slot_id 集合（按 slot_id 对齐）。

    版本间 slot 可能增删：新增 slot 影响其自身（新场次自新版本起才存在）；
    删除 slot 同样返回其 id——它在新版本中不再是计划场次，重算时由版本
    绑定规则自然排除（事件仍按发生时的旧版本核对）。
    """
    old_by_id = {s.slot_id: s for s in old.slots}
    new_by_id = {s.slot_id: s for s in new.slots}
    changed: set[str] = set()
    for slot_id in sorted(set(old_by_id) | set(new_by_id)):
        o = old_by_id.get(slot_id)
        n = new_by_id.get(slot_id)
        if o is None or n is None:
            changed.add(slot_id)  # 新增 / 删除
            continue
        if any(getattr(o, f) != getattr(n, f) for f in _AFFECTED_FIELDS):
            changed.add(slot_id)
    return frozenset(changed)


def affected_dimensions(old: SemesterPlan, new: SemesterPlan) -> frozenset[str]:
    """受影响的依据维度名称（venue/teacher/weather_alt/skill）。"""
    old_by_id = {s.slot_id: s for s in old.slots}
    new_by_id = {s.slot_id: s for s in new.slots}
    dims: set[str] = set()
    for slot_id in set(old_by_id) & set(new_by_id):
        o, n = old_by_id[slot_id], new_by_id[slot_id]
        if o.venue_id != n.venue_id:
            dims.add("venue")
        if o.teacher_id != n.teacher_id:
            dims.add("teacher")
        if o.weather_alternatives != n.weather_alternatives:
            dims.add("weather_alt")
        if o.skill_code != n.skill_code:
            dims.add("skill")
    # slot 增删可能同时牵动四个维度，保守标记
    if set(old_by_id) != set(new_by_id):
        dims.update(("venue", "teacher", "weather_alt", "skill"))
    return frozenset(dims)


class PlanRegistry:
    """方案版本库：提交即分配递增版本号，批准后冻结，修订生成新版本。

    旧版本永不删除——识别“阴阳课表”时需要把实际授课事件与**当时生效的**
    批准版本逐条对照。提交、批准、取代三个动作各自记录可比较的账本位置
    （``LedgerPosition``），因此：

    - 同一学期任一事件只能引用“当时已批准且尚未被新版本替代”的版本
      （``version_effective_at``）；
    - 较晚批准不改变早期版本的生效区间，早期结论不被回写；
    - 乱序重放/服务恢复只需按相同位置序列重建（``from_journal``），
      版本选择必然一致。
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
        at: LedgerPosition | None = None,
    ) -> SemesterPlan:
        issues = validate_plan(plan, venues, teachers)
        if issues:
            raise ValueError(f"方案校验未通过，共 {len(issues)} 项问题") from None
        key = (plan.school_id, plan.semester)
        history = self._plans.setdefault(key, [])
        version = len(history) + 1
        submitted = plan.with_status(
            version=version,
            status="submitted",
            submitted_by=submitted_by,
            submitted_at=at,
        )
        history.append(submitted)
        return submitted

    def approve(
        self,
        school_id: str,
        semester: str,
        version: int,
        *,
        at: LedgerPosition | None = None,
    ) -> SemesterPlan:
        plan = self.get(school_id, semester, version)
        if plan.status not in ("submitted",):
            raise ValueError(f"版本 {version} 当前状态 {plan.status}，不可批准")
        idx = self._plans[(school_id, semester)].index(plan)
        approved = plan.with_status(status="approved", approved_at=at)
        history = self._plans[(school_id, semester)]
        history[idx] = approved
        # 新批准版本的批准位置，就是此前生效版本被取代的位置（同刻）：
        # 取代点开区间，故同刻的业务事件已引用新版本。
        for i, older in enumerate(history):
            if i != idx and older.status == "approved":
                history[i] = older.with_status(status="superseded", superseded_at=at)
        return approved

    def get(self, school_id: str, semester: str, version: int | None = None) -> SemesterPlan:
        history = self._plans[(school_id, semester)]
        if version is None:
            for plan in reversed(history):
                if plan.status == "approved":
                    return plan
            raise ValueError("该学期尚无已批准方案")
        return history[version - 1]

    def version_effective_at(
        self,
        school_id: str,
        semester: str,
        position: LedgerPosition,
    ) -> SemesterPlan:
        """返回该位置“当时已批准且尚未被新版本替代”的唯一方案版本。

        审批间隙（首个版本批准之前 / 旧版本已被取代而新版本尚未批准的
        情形不会发生——取代与新批准同刻）抛 ``NoApprovedPlanAtPosition``。
        """
        history = self._plans.get((school_id, semester), [])
        candidates = [p for p in history if p.effective_at(position)]
        if not candidates:
            raise NoApprovedPlanAtPosition(
                f"{school_id}/{semester} 在位置 {position.occurred_at} 无已批准且未被取代的方案"
            )
        # 区间互不重叠；若理论上重叠，取版本号最大者，保证选择确定
        return max(candidates, key=lambda p: p.version)

    def effective_on(self, school_id: str, semester: str, *, at_index: int) -> SemesterPlan:
        """兼容旧调用：按账本序号选择当时已批准且未被取代的版本。

        新代码应使用 :meth:`version_effective_at`（按发生位置而非序号）。
        """
        history = self._plans.get((school_id, semester), [])
        candidates = [
            p for p in history
            if p.approved_at is not None and p.approved_at.seq <= at_index
            and (p.superseded_at is None or p.superseded_at.seq > at_index)
        ]
        if not candidates:
            raise NoApprovedPlanAtPosition(f"序号 {at_index} 前无已批准且未被取代的方案")
        return max(candidates, key=lambda p: p.version)

    def timeline(self, school_id: str, semester: str) -> tuple[tuple[LedgerPosition, str, int], ...]:
        """该学期全部版本动作的位置时间线：(位置, 动作, 版本)。

        动作为 submitted / approved / superseded，按位置全序排列，
        供重放、审计与测试核对。
        """
        events: list[tuple[LedgerPosition, str, int]] = []
        for p in self._plans.get((school_id, semester), []):
            if p.submitted_at is not None:
                events.append((p.submitted_at, "submitted", p.version))
            if p.approved_at is not None:
                events.append((p.approved_at, "approved", p.version))
            if p.superseded_at is not None:
                events.append((p.superseded_at, "superseded", p.version))
        return tuple(sorted(events, key=lambda e: LedgerPosition.sort_key(e[0])))

    def history(self, school_id: str, semester: str) -> tuple[SemesterPlan, ...]:
        return tuple(self._plans.get((school_id, semester), []))

    # ------------------------------------------------------------------
    def journal(self) -> tuple[dict, ...]:
        """导出版本库的只追加动作流（提交/批准），用于服务恢复。

        取代位置由相邻批准确定性派生，不单独入流——回放结果与导出前
        逐字节一致（superseded_at == 新版本的 approved_at）。
        """
        records: list[dict] = []
        for key in sorted(self._plans):
            school_id, semester = key
            for p in self._plans[key]:
                if p.submitted_at is not None:
                    records.append({
                        "type": "plan_submitted",
                        "school_id": school_id,
                        "semester": semester,
                        "version": p.version,
                        "plan": p,
                        "submitted_by": p.submitted_by,
                    })
                if p.approved_at is not None:
                    records.append({
                        "type": "plan_approved",
                        "school_id": school_id,
                        "semester": semester,
                        "version": p.version,
                        "approved_at": p.approved_at,
                    })
        def _key(rec):
            plan = rec.get("plan")
            pos = plan.submitted_at if rec["type"] == "plan_submitted" else rec["approved_at"]
            return LedgerPosition.sort_key(pos)
        return tuple(sorted(records, key=_key))

    @classmethod
    def from_journal(
        cls,
        records,
        venues: dict[str, Venue],
        teachers: dict,
    ) -> "PlanRegistry":
        """按动作流重建版本库；重放顺序与位置由记录携带，不依赖接收顺序。"""
        registry = cls()
        ordered = sorted(
            records,
            key=lambda r: LedgerPosition.sort_key(
                r["plan"].submitted_at if r["type"] == "plan_submitted" else r["approved_at"]
            ),
        )
        for rec in ordered:
            if rec["type"] == "plan_submitted":
                plan = rec["plan"]
                registry.submit(
                    plan.with_status(status="draft", submitted_at=None,
                                     approved_at=None, superseded_at=None),
                    venues, teachers,
                    submitted_by=rec["submitted_by"],
                    at=plan.submitted_at,
                )
            elif rec["type"] == "plan_approved":
                registry.approve(
                    rec["school_id"], rec["semester"], rec["version"],
                    at=rec["approved_at"],
                )
            else:
                raise ValueError(f"未知版本动作：{rec['type']}")
        return registry
