"""只追加事件账本与班级实况还原。

连续发生“场地冲突 → 临时占课 → 离线补签”后，仍能按时间顺序重放事件，
还原每个班真正完成的内容、缺口与补课安排。账本只追加、不修改不删除；
所有判定（确认、异常、覆盖）都是重放的派生结果，可随时重新计算。

事件类型：

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
from .models import STANDARD_MINUTES, ActivityKind


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    event_type: str
    payload: Any  # 领域对象或 dict（均为不可变）


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


class EventLedger:
    def __init__(self):
        self._entries: list[LedgerEntry] = []

    def append(self, event_type: str, payload) -> LedgerEntry:
        entry = LedgerEntry(len(self._entries) + 1, event_type, payload)
        self._entries.append(entry)
        return entry

    def entries(self, event_type: str | None = None) -> tuple[LedgerEntry, ...]:
        if event_type is None:
            return tuple(self._entries)
        return tuple(e for e in self._entries if e.event_type == event_type)

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
        """重放账本，还原单班实况。

        class_slots: 该班的 PlanSlot 集合（生效方案版本）。
        返回结构化实况：场次状态、会话、缺课、补课挂接、调课、占课、标记。
        """
        from collections import defaultdict

        # 展开计划场次（只核对到当前教学周；未来周次不算缺口）
        plan_week = current_week
        reports: dict[Occasion, TeacherReport] = {}
        observations: dict[Occasion, VenueObservation] = {}
        attendances: dict[Occasion, list[SampleAttendance]] = defaultdict(list)
        rescheduled: dict[Occasion, str] = {}   # -> 依据
        takeovers: list[dict] = []
        makeup_links: dict[Occasion, Occasion] = {}  # 原场次 -> 补课场次
        weather: dict[Occasion, str] = {}

        slot_ids = {s.slot_id for s in class_slots}

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
                rescheduled[p.occasion] = p.basis_ref
            elif et == "takeover" and p["slot_id"] in slot_ids:
                takeovers.append(p)
            elif et == "weather_trigger" and p["occasion"].slot_id in slot_ids:
                weather[p["occasion"]] = p["policy_ref"]
            elif et == "makeup_plan":
                if p["makeup_for"].slot_id in slot_ids:
                    makeup_links[p["makeup_for"]] = p["occasion"]

        sessions: list[ReconstructedSession] = []
        occasion_states: dict[str, str] = {}
        made_up: set[str] = set()
        takeover_keys = {Occasion(t["slot_id"], t["week"]).key() for t in takeovers}
        flags_index: dict[str, tuple[str, ...]] = {}

        # 按场次做三方确认
        for occ, report in sorted(reports.items(), key=lambda kv: (kv[0].week, kv[0].slot_id)):
            notes: list[str] = []
            obs = observations.get(occ)
            if obs is not None:
                venue = venues.get(obs.venue_id)
                if venue is not None and obs.observed_headcount > venue.safe_capacity:
                    notes.append("capacity_breach")
            result = assessor(
                report, obs, attendances.get(occ, []),
                class_headcount, adaptations, token_to_student,
            )
            key = occ.key()
            flags_index[key] = result.flags

            # 实际场地冲突：同一场地同一时刻出现他班观测（在全局视图中检查，
            # 这里通过账本做简化判定：venue_conflict_reported 事件）
            for entry in self._entries:
                if (
                    entry.event_type == "venue_conflict_reported"
                    and entry.payload["occasion"] == occ
                ):
                    notes.append("venue_conflict_actual")

            sessions.append(ReconstructedSession(
                occasion=occ, class_id=class_id, kind=report.kind,
                taught_skill=report.taught_skill, mode=report.mode,
                minutes=result.effective_minutes, confirmed=result.confirmed,
                reasons=result.reasons, flags=result.flags,
                notes=tuple(notes), makeup_for=report.makeup_for,
                per_student=result.per_student_minutes,
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

        # 展开计划场次状态（计划有但无报告）
        missing: list[str] = []
        for slot in class_slots:
            for week in range(1, plan_week + 1):
                if not slot.active_in_week(week):
                    continue
                occ = Occasion(slot.slot_id, week)
                key = occ.key()
                if key in occasion_states:
                    continue
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

        # 占课 / 已排补课的缺课 -> 待补课
        pending_makeup = [
            k for k, st in occasion_states.items()
            if st in ("taken_over", "missing", "weather_pending") or
            (st == "in_review")
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
        }
