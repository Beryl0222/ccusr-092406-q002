"""只追加事件账本与班级实况还原。

连续发生“场地冲突 → 临时占课 → 离线补签”后，仍能按时间顺序重放事件，
还原每个班真正完成的内容、缺口与补课安排。账本只追加、不修改不删除；
所有判定（确认、异常、覆盖）都是重放的派生结果，可随时重新计算。

事件的两个时间位置必须分开：

- ``occurred_at``：事件**实际发生**的账本位置（教师授课/学生到场时刻）。
  迟到上传（离线补传、服务恢复后补送）仍按实际发生位置参与版本绑定与
  场次匹配，而不是按服务器接收顺序；
- ``seq``：入账序号，仅在同一发生时刻做全序决胜。

事件类型：

- teacher_report / venue_observation / sample_attendance
- plan_submitted / plan_approved（方案动作，位置与版本库一致）
- schedule_change（合规调课）
- takeover（临时占课：其他学科占用，记录占用科目与依据单号，可为空表示突发）
- weather_trigger（降雨/高温触发，关联预案）
- makeup_plan（补课安排，挂接缺课场次）
- review_resolved（教研结论）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from .events import (
    ConfirmationResult,
    Occasion,
    SampleAttendance,
    SessionMode,
    TeacherReport,
    VenueObservation,
    assess_session,
)
from .models import STANDARD_MINUTES, ActivityKind, LedgerPosition
from .plans import NoApprovedPlanAtPosition


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    event_type: str
    payload: Any  # 领域对象或 dict（均为不可变）
    position: LedgerPosition | None = None

    @property
    def occurred_at(self) -> Optional[str]:
        return None if self.position is None else self.position.occurred_at


@dataclass(frozen=True)
class ReconstructedSession:
    occasion: Occasion
    class_id: str
    kind: ActivityKind
    taught_skill: str
    mode: SessionMode
    minutes: int
    confirmed: bool
    reasons: tuple[str, ...]
    flags: tuple[str, ...]
    notes: tuple[str, ...]
    makeup_for: Optional[Occasion]
    per_student: dict[str, int]
    plan_version: int = 0  # 该场实际绑定核对的方案版本（0=传统单版模式）


class EventLedger:
    def __init__(self):
        self._entries: list[LedgerEntry] = []

    def append(
        self,
        event_type: str,
        payload,
        *,
        occurred_at: str | None = None,
        phase: int = 2,
    ) -> LedgerEntry:
        """追加事件。

        occurred_at 为事件实际发生位置；缺省时退回接收入账顺序
        （仅传统单版重放使用，版本化重放要求显式发生位置）。
        """
        seq = len(self._entries) + 1
        position = LedgerPosition(seq, occurred_at, phase) if occurred_at else None
        entry = LedgerEntry(seq, event_type, payload, position)
        self._entries.append(entry)
        return entry

    def entries(self, event_type: str | None = None) -> tuple[LedgerEntry, ...]:
        if event_type is None:
            return tuple(self._entries)
        return tuple(e for e in self._entries if e.event_type == event_type)

    def ordered_entries(self) -> tuple[LedgerEntry, ...]:
        """按“实际发生位置”全序返回事件。

        迟到上传的事件按 occurred_at 归位，不按接收顺序；无发生位置的
        事件排在有位置事件之后，保持接收先后。同刻由 phase
        （提交 0 < 批准 1 < 业务 2）再由 seq 决胜，边界确定。
        """
        def key(e: LedgerEntry):
            if e.position is None:
                return (1, e.seq, 0, 0)
            return (0, e.position.occurred_at, e.position.phase, e.position.seq)
        return tuple(sorted(self._entries, key=key))

    # ------------------------------------------------------------------
    def rebuild_class(
        self,
        class_id: str,
        class_slots,
        class_headcount: int,
        adaptations: dict,
        token_to_student: dict[str, str],
        venues: dict,
        current_week: int,
        *,
        assessor: Callable[..., ConfirmationResult] = assess_session,
    ) -> dict:
        """重放账本，还原单班实况（传统单版模式：直接给定生效 slots）。"""
        return self._replay(
            class_id=class_id,
            headcount=class_headcount,
            adaptations=adaptations,
            token_to_student=token_to_student,
            venues=venues,
            current_week=current_week,
            assessor=assessor,
            versioned=False,
            registry=None, school_id="", semester="",
            anchor_monday=None,
            static_slots=tuple(class_slots),
        )

    def rebuild_class_versioned(
        self,
        class_id: str,
        registry,
        school_id: str,
        semester: str,
        class_headcount: int,
        adaptations: dict,
        token_to_student: dict[str, str],
        venues: dict,
        current_week: int,
        *,
        anchor_monday: datetime | None = None,
        assessor: Callable[..., ConfirmationResult] = assess_session,
    ) -> dict:
        """版本化重放：每个场次只与“其发生位置当时生效的批准版本”核对。

        - 批准间隙（当时无已批准方案）的场次状态为 ``no_plan``，不计阴阳
          课表异常，也不被后来批准的版本回写；
        - 迟到上传的证据按实际发生位置归位，绑定该场次的同一版本；
        - 方案修订后新增/删除的 slot 只在其生效版本的周计划中展开。

        anchor_monday 为教学周 1 的周一（日终探测与教学周换算的日历基准）；
        缺省取最早业务事件所在 ISO 周的周一，重放结果确定。
        """
        return self._replay(
            class_id=class_id,
            headcount=class_headcount,
            adaptations=adaptations,
            token_to_student=token_to_student,
            venues=venues,
            current_week=current_week,
            assessor=assessor,
            versioned=True,
            registry=registry, school_id=school_id, semester=semester,
            anchor_monday=anchor_monday,
            static_slots=(),
        )

    # ------------------------------------------------------------------
    def _replay(
        self, *, class_id, headcount, adaptations, token_to_student, venues,
        current_week, assessor, versioned, registry, school_id, semester,
        anchor_monday, static_slots,
    ) -> dict:
        from collections import defaultdict

        ordered = self.ordered_entries()

        # 版本化模式下的按版本索引（惰性构建）
        plan_slots: dict[int, dict[str, Any]] = {}   # version -> {slot_id: slot}
        bound_version: dict[str, int] = {}           # occasion_key -> 版本
        unregulated: dict[str, str] = {}             # occasion_key -> 原因
        # 本班在任意版本出现过的 slot：用于先做班级归属过滤，
        # 避免学校级账本中他班事件污染本班重建。
        class_slot_ids: set[str] = set()
        if versioned:
            for p in registry.history(school_id, semester):
                class_slot_ids.update(s.slot_id for s in p.slots_for(class_id))

        def slots_for_version(version: int):
            if version not in plan_slots:
                plan = registry.get(school_id, semester, version)
                plan_slots[version] = {s.slot_id: s for s in plan.slots_for(class_id)}
            return plan_slots[version]

        def bind(occ: Occasion, position: LedgerPosition):
            """把场次绑定到位置当时生效的方案；返回该版本中的 slot 或 None。

            返回 None 有两种语义：``no_approved_plan_at_position``（批准间隙，
            事件不参与核对）与 ``slot_not_in_version``（版本已绑定但该 slot
            不在当时版本中，事件留证并加偏离注记），由 bound_version 区分。
            他班 slot 一律返回 None 且不写入本班任何索引。
            """
            key = occ.key()
            if not versioned:
                return next((s for s in static_slots if s.slot_id == occ.slot_id), None)
            if occ.slot_id not in class_slot_ids:
                return None
            if key not in bound_version:
                try:
                    plan = registry.version_effective_at(school_id, semester, position)
                except NoApprovedPlanAtPosition:
                    unregulated.setdefault(key, "no_approved_plan_at_position")
                    return None
                bound_version[key] = plan.version
            by_id = slots_for_version(bound_version[key])
            slot = by_id.get(occ.slot_id)
            if slot is None:
                unregulated.setdefault(key, "slot_not_in_version")
            return slot

        def accepts(occ: Occasion, position: LedgerPosition) -> bool:
            """事件是否属于本班当时版本的计划范畴。"""
            if not versioned:
                return occ.slot_id in static_slot_ids
            if occ.slot_id not in class_slot_ids:
                return False
            if bind(occ, position) is not None:
                return True
            # 已绑定版本但 slot 不在该版本：事件留证并参与重建（加偏离注记）
            return occ.key() in bound_version

        def position_of(entry: LedgerEntry) -> LedgerPosition:
            if entry.position is not None:
                return entry.position
            # 无显式发生位置：以接收入账序号构造（传统模式不做版本绑定）
            return LedgerPosition(entry.seq, f"recv:{entry.seq:010d}", 2)

        reports: dict[Occasion, TeacherReport] = {}
        observations: dict[Occasion, VenueObservation] = {}
        attendances: dict[Occasion, list[SampleAttendance]] = defaultdict(list)
        rescheduled: dict[Occasion, str] = {}
        takeovers: list[dict] = []
        makeup_links: dict[Occasion, Occasion] = {}
        weather: dict[Occasion, str] = {}
        # slot_id 集合：传统模式用静态 slots；版本化模式按绑定版本累积
        static_slot_ids = {s.slot_id for s in static_slots}

        for entry in ordered:
            p = entry.payload
            et = entry.event_type
            pos = position_of(entry)
            if et == "teacher_report":
                if accepts(p.occasion, pos):
                    reports[p.occasion] = p
            elif et == "venue_observation":
                if accepts(p.occasion, pos):
                    observations[p.occasion] = p
            elif et == "sample_attendance":
                if accepts(p.occasion, pos):
                    attendances[p.occasion].append(p)
            elif et == "schedule_change":
                if accepts(p.occasion, pos):
                    rescheduled[p.occasion] = p.basis_ref
            elif et == "takeover":
                occ = Occasion(p["slot_id"], p["week"])
                if accepts(occ, pos):
                    takeovers.append(p)
            elif et == "weather_trigger":
                if accepts(p["occasion"], pos):
                    weather[p["occasion"]] = p["policy_ref"]
            elif et == "makeup_plan":
                original, makeup = p["makeup_for"], p["occasion"]
                # 补课场次按补课安排自身的发生位置绑定；原场次可能属于旧
                # 版本，回填只认 occasion key，不要求同版本接纳。
                if (not versioned and original.slot_id in static_slot_ids) or \
                        (versioned and accepts(makeup, pos)):
                    makeup_links[original] = makeup

        sessions: list[ReconstructedSession] = []
        occasion_states: dict[str, str] = {}
        made_up: set[str] = set()
        takeover_keys = {Occasion(t["slot_id"], t["week"]).key() for t in takeovers}
        flags_index: dict[str, tuple[str, ...]] = {}
        notes_index: dict[str, tuple[str, ...]] = {}

        # 按场次做三方确认 + 与绑定版本的方案逐条对照
        for occ, report in sorted(reports.items(), key=lambda kv: (kv[0].week, kv[0].slot_id)):
            notes: list[str] = []
            key = occ.key()
            version = bound_version.get(key, 0)
            bound_slot = None
            if versioned and version:
                bound_slot = plan_slots[version].get(occ.slot_id)

            obs = observations.get(occ)
            if obs is not None:
                venue = venues.get(obs.venue_id)
                if venue is not None and obs.observed_headcount > venue.safe_capacity:
                    notes.append("capacity_breach")
            result = assessor(
                report, obs, attendances.get(occ, []),
                headcount, adaptations, token_to_student,
            )
            flags_index[key] = result.flags

            # 实际场地冲突（账本留证事件）
            for entry in ordered:
                if (
                    entry.event_type == "venue_conflict_reported"
                    and entry.payload["occasion"] == occ
                ):
                    notes.append("venue_conflict_actual")

            # 与“当时生效版本”的方案对照（修订后的新版不能回写旧场）
            if versioned and bound_slot is None and version and \
                    unregulated.get(key) == "slot_not_in_version":
                notes.append(f"plan_deviation:slot_not_in_version:v{version}")
            if bound_slot is not None:
                if report.mode in (SessionMode.NORMAL, SessionMode.FREE,
                                   SessionMode.EXAM_DRILL):
                    if report.venue_id != bound_slot.venue_id:
                        notes.append(f"plan_deviation:venue:v{version}")
                elif report.mode in (SessionMode.RAIN_ALT, SessionMode.HEAT_ALT):
                    cond = "rain" if report.mode == SessionMode.RAIN_ALT else "heat"
                    alt = next((a for a in bound_slot.weather_alternatives
                                if a.condition == cond), None)
                    if alt is None or report.venue_id != alt.venue_id:
                        notes.append(f"plan_deviation:weather_alt:v{version}")
                if report.teacher_id != bound_slot.teacher_id and \
                        report.mode != SessionMode.RESCHEDULED:
                    notes.append(f"plan_deviation:teacher:v{version}")
                if report.taught_skill != bound_slot.skill_code:
                    notes.append(f"plan_deviation:skill:v{version}")
            notes_index[key] = tuple(notes)

            sessions.append(ReconstructedSession(
                occasion=occ, class_id=class_id, kind=report.kind,
                taught_skill=report.taught_skill, mode=report.mode,
                minutes=result.effective_minutes, confirmed=result.confirmed,
                reasons=result.reasons, flags=result.flags,
                notes=tuple(notes), makeup_for=report.makeup_for,
                per_student=result.per_student_minutes, plan_version=version,
            ))
            if result.confirmed:
                if report.mode == SessionMode.MAKEUP and report.makeup_for is not None:
                    occasion_states[report.makeup_for.key()] = "completed"
                    made_up.add(report.makeup_for.key())
                    occasion_states[key] = "completed_makeup"
                else:
                    occasion_states[key] = "completed"
            else:
                occasion_states[key] = "in_review"

        # 展开计划场次状态。
        # 版本化模式按 (slot × 周) 逐日探测“该场次当日 00:00 已批准且未被
        # 取代”的版本：周中批准时，批准日之前的场次处于批准间隙（no_plan，
        # 不判缺失、不被后来版本回写）。日首探测只影响“无事件场次”的
        # missing/no_plan 归类；有事件场次的版本由其实际 occurred_at 精确绑定。
        missing: list[str] = []
        fmt = "%Y-%m-%dT%H:%M:%S"
        monday = anchor_monday if anchor_monday is not None else (
            self._anchor_monday(ordered) if versioned else None)

        all_slot_by_id: dict[str, Any] = {}
        if versioned:
            for p in registry.history(school_id, semester):
                for sid, s in slots_for_version(p.version).items():
                    all_slot_by_id.setdefault(sid, s)

        for week in range(1, current_week + 1):
            candidates = static_slots if not versioned else tuple(all_slot_by_id.values())
            for base_slot in candidates:
                occ = Occasion(base_slot.slot_id, week)
                key = occ.key()
                if not versioned and not base_slot.active_in_week(week):
                    continue
                # 事件已得出结论（completed/in_review/completed_makeup 等）：
                # 保留按实际发生位置绑定的结论，不再展开覆盖。
                if key in occasion_states:
                    continue
                slot = base_slot
                if versioned:
                    if monday is None:
                        continue
                    # 无事件场次无法取实际发生时刻；以该场次日历日 00:00 探测：
                    # 批准必须不晚于场次当日，当日才批准的版本不回写早段课程。
                    day_start = monday + timedelta(
                        days=7 * (week - 1) + (slot.weekday - 1))
                    probe = LedgerPosition(0, day_start.strftime(fmt), 2)
                    try:
                        plan = registry.version_effective_at(school_id, semester, probe)
                    except NoApprovedPlanAtPosition:
                        occasion_states[key] = "no_plan"
                        unregulated.setdefault(key, "no_approved_plan_at_position")
                        continue
                    version_slots = slots_for_version(plan.version)
                    resolved = version_slots.get(slot.slot_id)
                    if resolved is None:
                        # 当时生效版本不含此 slot（修订删除）：不是计划场次
                        continue
                    if not resolved.active_in_week(week):
                        continue
                    slot = resolved
                    bound_version.setdefault(key, plan.version)
                if occ in rescheduled:
                    occasion_states[key] = "rescheduled"
                    continue
                if key in takeover_keys:
                    occasion_states[key] = "taken_over"
                    continue
                if occ in weather and occ not in reports:
                    occasion_states[key] = "weather_pending"
                    continue
                occasion_states[key] = "missing"
                missing.append(key)

        # 批准间隙内留有事件但未形成结论的场次：无方案可核对，单列不判异常
        for key in unregulated:
            if key not in occasion_states:
                occasion_states[key] = "no_plan"

        pending_makeup = [
            k for k, st in occasion_states.items()
            if st in ("taken_over", "missing", "weather_pending", "in_review")
        ]
        makeup_schedule = {
            original.key(): makeup.key()
            for original, makeup in makeup_links.items()
        }

        return {
            "class_id": class_id,
            "states": occasion_states,
            "sessions": sessions,
            "missing_occasions": tuple(sorted(missing)),
            "pending_makeup": tuple(sorted(pending_makeup)),
            "makeup_schedule": makeup_schedule,
            "made_up": made_up,
            "rescheduled": {k.key(): ref for k, ref in rescheduled.items()},
            "takeovers": tuple(sorted(takeovers, key=lambda t: (t["week"], t["slot_id"]))),
            "flags": flags_index,
            "notes": notes_index,
            "plan_versions": dict(bound_version),
            "unregulated": dict(unregulated),
        }

    # ------------------------------------------------------------------
    def _anchor_monday(self, ordered: tuple[LedgerEntry, ...]) -> datetime | None:
        """教学周锚点：最早业务事件（不含方案动作）所在周的周一 00:00。"""
        earliest: datetime | None = None
        fmt = "%Y-%m-%dT%H:%M:%S"
        for e in ordered:
            if e.position is None or e.event_type in ("plan_submitted", "plan_approved"):
                continue
            try:
                ts = datetime.strptime(e.position.occurred_at[:19], fmt)
            except ValueError:
                continue
            earliest = ts if earliest is None or ts < earliest else earliest
        if earliest is None:
            return None
        return datetime(earliest.year, earliest.month, earliest.day) \
            - timedelta(days=earliest.weekday())
