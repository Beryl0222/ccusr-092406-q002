"""只追加事件账本与班级实况还原。

连续发生“场地冲突 → 临时占课 → 离线补签”后，仍能按时间顺序重放事件，
还原每个班真正完成的内容、缺口与补课安排。账本只追加、不修改不删除；
所有判定（确认、异常、覆盖）都是重放的派生结果，可随时重新计算。

事件位置（修复版本生效时点的关键）：

- 每条事件除接收序号 ``seq`` 外，还带**实际发生位置** ``position.at``，
  与方案提交/批准/取代事件共用同一条可比较位置轴（``LedgerPosition``）；
- 迟到上传时接收序号更晚，但 ``at`` 仍是实际发生点，重放按 ``at`` 匹配
  当时生效的方案版本，而不是按接收顺序；
- 因此无论事件以何种顺序送达、或服务恢复后从只追加存储重放，
  版本选择与三方确认结果都完全一致（确定性重放）。

事件类型：

- plan_submitted / plan_approved（方案版本事件，由 PlanRegistry 登记）
- teacher_report / venue_observation / sample_attendance
- schedule_change（合规调课）
- takeover（临时占课：其他学科占用，记录占用科目与依据单号，可为空表示突发）
- weather_trigger（降雨/高温触发，关联预案）
- makeup_plan（补课安排，挂接缺课场次）
- review_resolved（教研结论）
"""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass(frozen=True)
class LedgerEntry:
    seq: int                       # 接收序号（到达顺序，仅用于同位置定序）
    event_type: str
    payload: Any                   # 领域对象或 dict（均为不可变）
    position: LedgerPosition = LedgerPosition(0)


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
    plan_version: Optional[int] = None  # 核对所依据的当时生效方案版本


# 需要与生效方案核对的实际安排维度
_PLAN_DIMENSIONS = ("venue", "teacher", "skill")


class EventLedger:
    def __init__(self):
        self._entries: list[LedgerEntry] = []

    def append(
        self,
        event_type: str,
        payload,
        *,
        at: int | None = None,
        seq: int | None = None,
    ) -> LedgerEntry:
        """追加事件。

        at 为实际发生位置；缺省取接收序号（在线即时上报）。
        seq 仅供从持久化存储恢复时保持原接收序号。
        """
        recv_seq = seq if seq is not None else len(self._entries) + 1
        pos = LedgerPosition(at if at is not None else recv_seq, recv_seq)
        entry = LedgerEntry(recv_seq, event_type, payload, pos)
        self._entries.append(entry)
        return entry

    def append_plan_event(self, event_type: str, payload, *, at: int | None = None) -> LedgerEntry:
        """方案版本事件（提交/批准）与授课事件进入同一条位置轴。"""
        return self.append(event_type, payload, at=at)

    def entries(self, event_type: str | None = None) -> tuple[LedgerEntry, ...]:
        if event_type is None:
            return tuple(self._entries)
        return tuple(e for e in self._entries if e.event_type == event_type)

    @classmethod
    def restore(cls, entries) -> "EventLedger":
        """从只追加的事件记录恢复账本（服务恢复路径）。

        位置与接收序号原样保留，重放结果与在线过程一致。
        """
        ledger = cls()
        for e in entries:
            ledger.append(e.event_type, e.payload, at=e.position.at, seq=e.seq)
        return ledger

    # ------------------------------------------------------------------
    def rebuild_class(
        self,
        class_id: str,
        class_slots=None,
        class_headcount: int = 0,
        adaptations: dict | None = None,
        token_to_student: dict[str, str] | None = None,
        venues: dict | None = None,
        current_week: int = 0,
        *,
        registry=None,
        school_id: str | None = None,
        semester: str | None = None,
        week_at: Callable[[int], int] | None = None,
        scope: dict | None = None,
        assessor: Callable[..., ConfirmationResult] = assess_session,
    ) -> dict:
        """重放账本，还原单班实况。

        版本核对：提供 ``registry`` 时，每个场次按其**实际发生位置**选择
        “当时已批准且尚未被替代”的方案版本；无生效版本（批准之前/批准间隙）
        的场次计入 ``uncovered``，不判缺失也不判异常，绝不退用最新版本。
        不提供 registry 时退化为单版本模式（``class_slots`` 直接给定）。

        ``week_at`` 把教学周换算为场次位置（版本化模式必需，用于无事件周）。
        ``scope`` 为影响区间重算：{"slot_ids": frozenset|None, "weeks": range|None}，
        只返回落在区间内的场次，区间外的历史结论不重算。
        """
        from collections import defaultdict

        adaptations = adaptations or {}
        token_to_student = token_to_student or {}
        venues = venues or {}

        versioned = registry is not None
        if versioned and week_at is None:
            raise ValueError("版本化重放必须提供 week_at 以确定每个教学周的位置")

        def in_scope(slot_id: str, week: int) -> bool:
            if scope is None:
                return True
            allowed_slots = scope.get("slot_ids")
            allowed_weeks = scope.get("weeks")
            if allowed_slots is not None and slot_id not in allowed_slots:
                return False
            if allowed_weeks is not None and week not in allowed_weeks:
                return False
            return True

        # 该班全部版本中出现过的槽位（用于接纳已被改名/删除槽位的历史事件）
        if versioned:
            all_slots = {
                s
                for p in registry.history(school_id, semester)
                for s in p.slots
                if s.class_id == class_id
            }
        else:
            all_slots = set(class_slots)
        slot_ids = {s.slot_id for s in all_slots}

        def position_for(week: int) -> LedgerPosition:
            at = week_at(week) if week_at is not None else week
            return LedgerPosition(at, 0)

        def resolve(slot_id: str, week: int):
            """返回 (版本号, 该版本中该班的该槽位)；无生效版本 -> (None, None)。"""
            if not versioned:
                slot = next((s for s in class_slots if s.slot_id == slot_id), None)
                return None, slot
            pos = position_for(week)
            version = registry.effective_version_at(school_id, semester, pos)
            if version is None:
                return None, None
            plan = registry.get(school_id, semester, version)
            slot = next(
                (s for s in plan.slots if s.class_id == class_id and s.slot_id == slot_id),
                None,
            )
            return version, slot

        reports: dict[Occasion, TeacherReport] = {}
        observations: dict[Occasion, VenueObservation] = {}
        attendances: dict[Occasion, list[SampleAttendance]] = defaultdict(list)
        rescheduled: dict[Occasion, object] = {}   # -> ScheduleChange
        takeovers: list[dict] = []
        makeup_links: dict[Occasion, Occasion] = {}  # 原场次 -> 补课场次
        weather: dict[Occasion, str] = {}

        for entry in self._entries:
            p = entry.payload
            et = entry.event_type
            if et == "teacher_report" and p.occasion.slot_id in slot_ids:
                reports[p.occasion] = p
            elif et == "venue_observation" and p.occasion.slot_id in slot_ids:
                observations[p.occasion] = p
            elif et == "sample_attendance" and p.occasion.slot_id in slot_ids:
                attendances[p.occasion].append(p)
            elif et == "schedule_change" and p.occasion.slot_id in slot_ids:
                rescheduled[p.occasion] = p
            elif et == "takeover" and p["slot_id"] in slot_ids:
                takeovers.append(p)
            elif et == "weather_trigger" and p["occasion"].slot_id in slot_ids:
                weather[p["occasion"]] = p["policy_ref"]
            elif et == "makeup_plan":
                if p["makeup_for"].slot_id in slot_ids:
                    makeup_links[p["makeup_for"]] = p["occasion"]

        sessions: list[ReconstructedSession] = []
        occasion_states: dict[str, str] = {}
        basis_versions: dict[str, Optional[int]] = {}
        made_up: set[str] = set()
        uncovered: set[str] = set()
        takeover_keys = {Occasion(t["slot_id"], t["week"]).key() for t in takeovers}
        flags_index: dict[str, tuple[str, ...]] = {}

        def plan_mismatch_notes(occ: Occasion, report: TeacherReport, version, slot) -> list[str]:
            """实际场地/教师/技能与“当时生效版本”逐条核对。"""
            notes: list[str] = []
            if not versioned or version is None:
                return notes
            tag = f"[v{version}]"
            if slot is None:
                # 事件引用的槽位在当时生效版本中不存在（已删除/改名的课表外授课）
                notes.append(f"plan_slot_missing{tag}")
                return notes
            change = rescheduled.get(occ)
            # 调课按审批后的新安排核对；补课挂接原场次，不与普通课表比对
            if report.mode == SessionMode.MAKEUP:
                return notes
            if report.mode == SessionMode.RESCHEDULED and change is not None:
                expect_venue, expect_teacher = change.new_venue_id, change.new_teacher_id
            elif report.mode in (SessionMode.RAIN_ALT, SessionMode.HEAT_ALT):
                cond = report.mode.value.replace("_alt", "")
                expect_venue = next(
                    (a.venue_id for a in slot.weather_alternatives if a.condition == cond),
                    None,
                )
                expect_teacher = slot.teacher_id
            elif report.mode in (SessionMode.FREE, SessionMode.EXAM_DRILL):
                # 自由活动/应考训练不安排计划技能，场地教师仍须相符
                expect_venue, expect_teacher = slot.venue_id, slot.teacher_id
            else:
                expect_venue, expect_teacher = slot.venue_id, slot.teacher_id
            if expect_venue and report.venue_id != expect_venue:
                notes.append(f"plan_venue_mismatch{tag}")
            if report.teacher_id != expect_teacher:
                notes.append(f"plan_teacher_mismatch{tag}")
            if (
                report.mode not in (SessionMode.FREE, SessionMode.EXAM_DRILL)
                and report.taught_skill != slot.skill_code
            ):
                notes.append(f"plan_skill_mismatch{tag}")
            return notes

        # 按场次做三方确认（迟到事件也按实际场次归集，与接收顺序无关）
        for occ, report in sorted(reports.items(), key=lambda kv: (kv[0].at, kv[0].week, kv[0].slot_id)):
            if not in_scope(occ.slot_id, occ.week):
                continue
            version, slot = resolve(occ.slot_id, occ.week)
            key = occ.key()
            basis_versions[key] = version
            notes: list[str] = []

            obs = observations.get(occ)
            if obs is not None:
                venue = venues.get(obs.venue_id)
                if venue is not None and obs.observed_headcount > venue.safe_capacity:
                    notes.append("capacity_breach")

            # 同场次签到按实际发生时刻定序，保证乱序送达时重放确定
            occ_attendance = sorted(
                attendances.get(occ, []),
                key=lambda a: (a.occurred_at, a.received_at, a.token),
            )
            result = assessor(
                report, obs, occ_attendance,
                class_headcount, adaptations, token_to_student,
            )
            flags_index[key] = result.flags

            # 实际场地冲突：他班观测留证事件
            for entry in self._entries:
                if (
                    entry.event_type == "venue_conflict_reported"
                    and entry.payload["occasion"] == occ
                ):
                    notes.append("venue_conflict_actual")

            if versioned and version is None:
                # 批准之前 / 批准间隙发生：三方确认照常，但无版本可核对
                notes.append("no_effective_plan")
                uncovered.add(key)
            else:
                notes.extend(plan_mismatch_notes(occ, report, version, slot))

            sessions.append(ReconstructedSession(
                occasion=occ, class_id=class_id, kind=report.kind,
                taught_skill=report.taught_skill, mode=report.mode,
                minutes=result.effective_minutes, confirmed=result.confirmed,
                reasons=result.reasons, flags=result.flags,
                notes=tuple(notes), makeup_for=report.makeup_for,
                per_student=result.per_student_minutes, plan_version=version,
            ))
            if version is None and versioned:
                occasion_states[key] = "unverified"
            elif result.confirmed:
                if report.mode == SessionMode.MAKEUP and report.makeup_for is not None:
                    # 补课回填原场次：原场次的依据版本按**原场次所在周**生效的
                    # 版本确定（与覆盖按原周归类一致），而不是补课发生周的版本。
                    orig = report.makeup_for
                    orig_version, _ = resolve(orig.slot_id, orig.week)
                    basis_versions[orig.key()] = orig_version
                    made_up.add(orig.key())
                    if versioned and orig_version is None:
                        occasion_states[orig.key()] = "unverified"
                        uncovered.add(orig.key())
                    else:
                        occasion_states[orig.key()] = "completed"
                    occasion_states[key] = "completed_makeup"
                else:
                    occasion_states[key] = "completed"
            else:
                occasion_states[key] = "in_review"

        # 展开计划场次状态（计划有但无报告）——逐周按当时生效版本判断。
        # 同一 slot_id 跨版本只迭代一次；是否为当周计划场次由当时版本判定。
        missing: list[str] = []
        unique_slot_ids = sorted({s.slot_id for s in all_slots})
        for slot_id in unique_slot_ids:
            for week in range(1, current_week + 1):
                if not in_scope(slot_id, week):
                    continue
                occ = Occasion(slot_id, week)
                key = occ.key()
                if key in occasion_states:
                    continue
                version, active_slot = resolve(slot_id, week)
                if versioned and version is None:
                    # 该周尚无可核对方案：既不算缺口也不进异常
                    occasion_states[key] = "unverified"
                    basis_versions[key] = None
                    uncovered.add(key)
                    continue
                if active_slot is None or not active_slot.active_in_week(week):
                    # 该槽位在当时版本中不存在/本周不排课：不是计划场次
                    continue
                basis_versions[key] = version
                if occ in rescheduled:
                    occasion_states[key] = "rescheduled"
                    continue
                if key in takeover_keys:
                    occasion_states[key] = "taken_over"
                    continue
                if occ in weather and occ not in reports:
                    occasion_states[key] = "weather_pending"  # 触发但未执行替代
                    continue
                occasion_states[key] = "missing"
                missing.append(key)

        # 占课 / 已排补课的缺课 -> 待补课（无版本可核对的场次不挂补课）
        pending_makeup = [
            k for k, st in occasion_states.items()
            if k not in uncovered
            and (
                st in ("taken_over", "missing", "weather_pending")
                or st == "in_review"
            )
        ]
        makeup_schedule = {
            original.key(): makeup.key()
            for original, makeup in makeup_links.items()
        }

        return {
            "class_id": class_id,
            "versioned": versioned,
            "scope": scope,
            "states": occasion_states,
            "sessions": sessions,
            "missing_occasions": tuple(sorted(missing)),
            "pending_makeup": tuple(sorted(pending_makeup)),
            "makeup_schedule": makeup_schedule,
            "made_up": made_up,
            "rescheduled": {k.key(): ref.basis_ref for k, ref in rescheduled.items()},
            "takeovers": tuple(sorted(takeovers, key=lambda t: (t["week"], t["slot_id"]))),
            "flags": flags_index,
            "basis_versions": basis_versions,
            "uncovered": tuple(sorted(uncovered)),
        }
