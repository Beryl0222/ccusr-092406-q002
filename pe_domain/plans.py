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

版本生效时点（修复“用后批准的第二版核对第一版期间课程”）：

- 提交、批准、取代三类版本事件都写入账本，各自带可比较的 ``LedgerPosition``
  （实际发生位置 at + 同位置次序 seq），与授课事件共用同一排序空间；
- 任一事件只能引用“在该事件实际发生位置已经批准、且尚未被新版本替代”的版本
  （``effective_on``），批准间隙内的事件无版本可引用，记为 uncovered 而非异常；
- 较晚的批准/取代绝不改变更早位置上的版本选择，因此迟到上传（at 早于接收序号）
  与账本乱序重放、服务恢复重放得到完全相同的版本选择。
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import ActivityKind, LedgerPosition, SemesterPlan, Venue, VenueType


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

    旧版本永不删除——识别“阴阳课表”时需要把实际授课事件与**当时生效**的
    批准版本逐条对照。

    生效时点完全由账本位置决定：版本库通过账本登记 ``plan_submitted`` /
    ``plan_approved`` 事件（取代在新版本批准的同一事务内登记），恢复时
    只重放这些事件即可重建同样的状态，乱序/迟到/重启结果一致。
    """

    SUBMIT_EVENT = "plan_submitted"
    APPROVE_EVENT = "plan_approved"

    def __init__(self, ledger=None):
        self._plans: dict[tuple[str, str], list[SemesterPlan]] = {}
        self._ledger = ledger
        if ledger is not None:
            self._restore(ledger)

    # ------------------------------------------------------------------
    # 从账本恢复：只追加事件重放，得到与在线登记完全一致的版本状态
    # ------------------------------------------------------------------
    def _restore(self, ledger) -> None:
        for entry in ledger.entries():
            p = entry.payload
            if not isinstance(p, dict) or p.get("kind") not in (
                self.SUBMIT_EVENT, self.APPROVE_EVENT
            ):
                continue
            # 位置以账本条目为准：迟到登记的方案事件 at 可能早于接收序号
            if p["kind"] == self.SUBMIT_EVENT:
                self._ingest_submit(p["plan"], entry.position)
            else:
                self._ingest_approve(
                    p["school_id"], p["semester"], p["version"], entry.position
                )

    def _ingest_submit(self, plan: SemesterPlan, pos: LedgerPosition) -> SemesterPlan:
        key = (plan.school_id, plan.semester)
        history = self._plans.setdefault(key, [])
        if plan.version != len(history) + 1:
            raise ValueError(
                f"版本号必须连续提交：收到 v{plan.version}，下一应为 v{len(history) + 1}"
            )
        submitted = plan.with_status(
            status="submitted", submitted_at=pos,
            approved_at=None, superseded_at=None,
        )
        history.append(submitted)
        return submitted

    def _ingest_approve(
        self, school_id: str, semester: str, version: int, pos: LedgerPosition
    ) -> SemesterPlan:
        history = self._plans[(school_id, semester)]
        idx = version - 1
        plan = history[idx]
        approved = plan.with_status(status="approved", approved_at=pos)
        history[idx] = approved
        # 仅取代“当前批准中”的版本；早已 superseded 的版本保留其原取代位置，
        # 多次取代后每个版本的生效区间仍是 [approved_at, superseded_at)。
        for i, older in enumerate(history):
            if i != idx and older.status == "approved":
                history[i] = older.with_status(status="superseded", superseded_at=pos)
        return approved

    # ------------------------------------------------------------------
    def submit(
        self,
        plan: SemesterPlan,
        venues: dict[str, Venue],
        teachers: dict,
        *,
        submitted_by: str,
        at: int | None = None,
    ) -> SemesterPlan:
        issues = validate_plan(plan, venues, teachers)
        if issues:
            raise ValueError(f"方案校验未通过，共 {len(issues)} 项问题") from None
        key = (plan.school_id, plan.semester)
        history = self._plans.setdefault(key, [])
        version = len(history) + 1
        numbered = plan.with_status(
            version=version, submitted_by=submitted_by,
            submitted_at=None, approved_at=None, superseded_at=None,
        )
        if self._ledger is None:
            return self._ingest_submit(numbered, LedgerPosition(at if at is not None else version, version))
        entry = self._ledger.append_plan_event(
            self.SUBMIT_EVENT,
            {"kind": self.SUBMIT_EVENT, "plan": numbered},
            at=at,
        )
        return self._ingest_submit(numbered, entry.position)

    def approve(
        self,
        school_id: str,
        semester: str,
        version: int,
        *,
        at: int | None = None,
    ) -> SemesterPlan:
        # 先确认版本存在且可批准
        self.get(school_id, semester, version)
        if self._ledger is None:
            return self._ingest_approve(
                school_id, semester, version,
                LedgerPosition(at if at is not None else version, version),
            )
        entry = self._ledger.append_plan_event(
            self.APPROVE_EVENT,
            {"kind": self.APPROVE_EVENT, "school_id": school_id,
             "semester": semester, "version": version},
            at=at,
        )
        return self._ingest_approve(school_id, semester, version, entry.position)

    def get(self, school_id: str, semester: str, version: int | None = None) -> SemesterPlan:
        history = self._plans[(school_id, semester)]
        if version is None:
            for plan in reversed(history):
                if plan.status == "approved":
                    return plan
            raise ValueError("该学期尚无已批准方案")
        return history[version - 1]

    def effective_on(
        self, school_id: str, semester: str, at: int | LedgerPosition
    ) -> SemesterPlan:
        """返回在位置 ``at`` “当时已批准且尚未被替代”的方案版本。

        ``at`` 可为整数（实际发生位置）或 ``LedgerPosition``。
        批准间隙（尚未有任何批准）或在最早版本批准之前抛错，由调用方
        记为“当时无生效方案”，绝不退而使用最新版本。
        """
        pos = at if isinstance(at, LedgerPosition) else LedgerPosition(at, at)
        history = self._plans.get((school_id, semester), [])
        effective = [p for p in history if p.effective_at(pos)]
        if not effective:
            raise ValueError("当时无已批准且未被替代的生效方案")
        # 同一位置不应有两个生效版本（取代是半开区间）；取最高版本兜底确定性
        return max(effective, key=lambda p: p.version)

    def effective_version_at(
        self, school_id: str, semester: str, at: int | LedgerPosition
    ) -> int | None:
        """与 effective_on 相同的选择，返回版本号；无生效版本时返回 None。"""
        try:
            return self.effective_on(school_id, semester, at).version
        except ValueError:
            return None

    def history(self, school_id: str, semester: str) -> tuple[SemesterPlan, ...]:
        return tuple(self._plans.get((school_id, semester), []))

    # ------------------------------------------------------------------
    # 修订影响分析：只标记受影响区间，供有选择地重算
    # ------------------------------------------------------------------
    def revision_impact(
        self, school_id: str, semester: str, new_version: int
    ) -> dict:
        """比较相邻两版，返回影响场地/教师/场地应急替代/技能目标的槽位。

        仅这些槽位对应、且发生在新版本生效区间内的场次需要重算；
        未变化的槽位与新版本生效前的历史场次沿用旧版依据。
        """
        history = self._plans[(school_id, semester)]
        if not 2 <= new_version <= len(history):
            raise ValueError("只能与上一版比较修订影响")
        old = {s.slot_id: s for s in history[new_version - 2].slots}
        new = {s.slot_id: s for s in history[new_version - 1].slots}
        changed: dict[str, tuple[str, ...]] = {}
        for slot_id in sorted(set(old) | set(new)):
            dims: list[str] = []
            o, n = old.get(slot_id), new.get(slot_id)
            if o is None or n is None:
                dims.append("slot_added" if o is None else "slot_removed")
            else:
                if n.venue_id != o.venue_id:
                    dims.append("venue")
                if n.teacher_id != o.teacher_id:
                    dims.append("teacher")
                if n.weather_alternatives != o.weather_alternatives:
                    dims.append("weather_alternative")
                if n.skill_code != o.skill_code:
                    dims.append("skill")
            if dims:
                changed[slot_id] = tuple(dims)
        new_plan = history[new_version - 1]
        return {
            "version": new_version,
            "effective_at": new_plan.approved_at,
            "changed_slots": changed,
        }
