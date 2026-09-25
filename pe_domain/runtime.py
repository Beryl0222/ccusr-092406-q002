"""运行时协调层：事件账本 + 方案版本库 + 复核台账的唯一装配点。

所有对外视图（班级覆盖、教研异常、家长分钟）都必须从这里的**同一次
版本化重放**派生，杜绝“各拿一个版本”的口径分裂。

关键不变量：

1. 事件按**实际发生位置**（occurred_at）全序归位，接收顺序不影响版本
   选择；迟到上传（离线/服务恢复补送）匹配实际发生的场次；
2. 归一化（``normalized`` / ``from_journal``）只重排与重编号，不改变任何
   业务含义——乱序摄入与服务恢复后重建得到逐字段相同的重放结果；
3. 方案修订只重算影响区间（新版本生效周起、命中受影响 slot）的场次，
   已结案复核保留原结论与原依据版本，只追加更正。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Optional

from .ledger import EventLedger, LedgerEntry
from .models import LedgerPosition
from .plans import PlanRegistry, affected_slots
from .review import ReviewBoard, detect_anomalies

_TIME_FMT = "%Y-%m-%dT%H:%M:%S"


@dataclass(frozen=True)
class JournalItem:
    """账本与版本库的统一只追加动作（接收顺序无关，重放按位置归位）。"""

    event_type: str
    payload: Any
    occurred_at: str
    phase: int = 2


class SchoolRuntime:
    def __init__(
        self,
        school_id: str,
        semester: str,
        venues: dict,
        teachers: dict,
        *,
        board: Optional[ReviewBoard] = None,
    ):
        self.school_id = school_id
        self.semester = semester
        self.venues = venues
        self.teachers = teachers
        self.registry = PlanRegistry()
        self.ledger = EventLedger()
        self.board = board or ReviewBoard()

    # ---------------------------------------------------------------- 摄入
    def submit_plan(self, plan, *, submitted_by: str, occurred_at: str):
        """提交方案：提交位置留账，版本处于 submitted，不核对任何事件。"""
        entry = self.ledger.append(
            "plan_submitted",
            {"version": plan.version or 0, "plan": plan, "submitted_by": submitted_by},
            occurred_at=occurred_at, phase=0,
        )
        return self.registry.submit(
            plan, self.venues, self.teachers,
            submitted_by=submitted_by, at=entry.position,
        )

    def approve_plan(self, version: int, *, occurred_at: str):
        """批准方案：批准位置（闭区间）起生效；同刻取代旧版本（开区间）。"""
        entry = self.ledger.append(
            "plan_approved", {"version": version},
            occurred_at=occurred_at, phase=1,
        )
        return self.registry.approve(
            self.school_id, self.semester, version, at=entry.position,
        )

    def record(self, event_type: str, payload, *, occurred_at: str) -> LedgerEntry:
        """记录业务事件（教师/场地/签到/天气/占课/调课/补课等）。"""
        return self.ledger.append(event_type, payload, occurred_at=occurred_at, phase=2)

    # ---------------------------------------------------------------- 归一/恢复
    def journal(self) -> tuple[JournalItem, ...]:
        """导出动作流：只含事件类型、负载与发生位置，不含接收序号。"""
        items = []
        for e in self.ledger.entries():
            items.append(JournalItem(
                e.event_type, e.payload,
                e.position.occurred_at, e.position.phase,
            ))
        return tuple(items)

    @classmethod
    def from_journal(
        cls,
        items,
        school_id: str,
        semester: str,
        venues: dict,
        teachers: dict,
        *,
        board: Optional[ReviewBoard] = None,
    ) -> "SchoolRuntime":
        """从动作流恢复：按发生位置全序重放，重编号位置，重建版本库。

        传入顺序（接收顺序）任意：提交、批准、业务事件的相对先后只由
        (occurred_at, phase) 决定，因此恢复结果与乱序重放完全一致。
        """
        runtime = cls(school_id, semester, venues, teachers, board=board)
        ordered = sorted(items, key=lambda i: (i.occurred_at, i.phase))
        for new_seq, item in enumerate(ordered, start=1):
            position = LedgerPosition(new_seq, item.occurred_at, item.phase)
            if item.event_type == "plan_submitted":
                rec = item.payload
                runtime.ledger.append(
                    "plan_submitted", rec,
                    occurred_at=item.occurred_at, phase=0,
                )
                plan = rec["plan"] if isinstance(rec, dict) else rec.plan
                submitted_by = rec["submitted_by"] if isinstance(rec, dict) else rec.submitted_by
                runtime.registry.submit(
                    plan, venues, teachers,
                    submitted_by=submitted_by, at=position,
                )
            elif item.event_type == "plan_approved":
                rec = item.payload
                version = rec["version"] if isinstance(rec, dict) else rec.version
                runtime.ledger.append(
                    "plan_approved", rec,
                    occurred_at=item.occurred_at, phase=1,
                )
                runtime.registry.approve(school_id, semester, version, at=position)
            else:
                runtime.ledger.append(
                    item.event_type, item.payload,
                    occurred_at=item.occurred_at, phase=2,
                )
        return runtime

    def normalized(self) -> "SchoolRuntime":
        """乱序归一：按发生位置重排重编号后的等价运行时。"""
        return self.from_journal(
            self.journal(), self.school_id, self.semester,
            self.venues, self.teachers, board=self.board.snapshot(),
        )

    # ---------------------------------------------------------------- 重放
    def anchor_monday(self) -> datetime | None:
        """教学周 1 周一（与账本重放同一锚点：最早业务事件所在周周一）。"""
        return self.ledger._anchor_monday(self.ledger.ordered_entries())

    def rebuild(
        self,
        class_id: str,
        class_headcount: int,
        current_week: int,
        *,
        adaptations: dict | None = None,
        token_to_student: dict | None = None,
    ) -> dict:
        """版本化重放单班（所有视图的唯一事实来源）。"""
        return self.ledger.rebuild_class_versioned(
            class_id, self.registry, self.school_id, self.semester,
            class_headcount, adaptations or {}, token_to_student or {},
            self.venues, current_week, anchor_monday=self.anchor_monday(),
        )

    def goals_by_version(self) -> dict[int, tuple]:
        """版本号 -> 该版本技能目标（覆盖核对按场次绑定版本取目标）。"""
        result: dict[int, tuple] = {}
        for plan in self.registry.history(self.school_id, self.semester):
            result[plan.version] = plan.skill_goals
        return result

    def impact_scope(
        self,
        new_version: int,
        current_week: int,
    ) -> frozenset[str]:
        """计算修订影响区间内的场次 key 集合。

        区间 = 新版本批准周（含）至当前周；命中 affected_slots 判定为
        受影响（场地/教师/天气应急替代/技能目标变化或 slot 增删）的 slot。
        """
        history = self.registry.history(self.school_id, self.semester)
        new = history[new_version - 1]
        previous = history[new_version - 2] if new_version >= 2 else None
        if previous is None or new.approved_at is None:
            return frozenset()
        changed = affected_slots(previous, new)
        first_week = self._week_of_position(new.approved_at)
        approval_dt = datetime.strptime(new.approved_at.occurred_at[:19], _TIME_FMT)
        old_by_id = {s.slot_id: s for s in previous.slots}
        new_by_id = {s.slot_id: s for s in new.slots}
        monday = self.anchor_monday()
        keys: set[str] = set()
        for slot_id in changed:
            for week in range(max(1, first_week), current_week + 1):
                slot = new_by_id.get(slot_id) or old_by_id.get(slot_id)
                if slot is None or not slot.active_in_week(week):
                    continue
                if monday is not None and week == first_week:
                    # 批准日当天早段课程仍属旧版本：以当日 00:00 与批准时刻
                    # 比较，只纳入批准日之后的场次（保守排除批准当日）。
                    day_start = monday + timedelta(
                        days=7 * (week - 1) + (slot.weekday - 1))
                    if day_start <= approval_dt:
                        continue
                keys.add(f"{slot_id}#w{week}")
        return frozenset(keys)

    def _week_of_position(self, position: LedgerPosition) -> int:
        """位置对应的教学周（与账本重放同一锚点）。"""
        monday = self.anchor_monday()
        if monday is None:
            return 1
        target = datetime.strptime(position.occurred_at[:19], _TIME_FMT)
        return (target.date() - monday.date()).days // 7 + 1

    def recompute(
        self,
        class_id: str,
        class_headcount: int,
        current_week: int,
        scope: frozenset[str],
        *,
        reason: str,
        adaptations: dict | None = None,
        token_to_student: dict | None = None,
        basis_versions: tuple[int, ...] | None = None,
    ) -> dict:
        """按给定影响区间重放并更新复核台账。

        区间内的识别结果重新计算；区间外已完成复核的历史结论保留原依据，
        已结案者只收到追加更正。方案修订（:meth:`recompute_for_revision`）
        与迟到上传补证（scope 取补证场次）都走这一条路。
        """
        rebuilt = self.rebuild(
            class_id, class_headcount, current_week,
            adaptations=adaptations, token_to_student=token_to_student,
        )
        anomalies = detect_anomalies(class_id, rebuilt, scope=scope)
        versions = basis_versions
        if versions is None:
            versions = tuple(sorted({
                rebuilt["plan_versions"].get(k, 0) for k in scope
                if rebuilt["plan_versions"].get(k)
            }))
        case, correction = self.board.reconcile(
            class_id, anomalies, reason=reason,
            basis_versions=tuple(versions), scope=scope,
        )
        return {
            "rebuilt": rebuilt,
            "scope": scope,
            "anomalies": anomalies,
            "case": case,
            "correction": correction,
        }

    def recompute_for_revision(
        self,
        class_id: str,
        class_headcount: int,
        current_week: int,
        new_version: int,
        *,
        adaptations: dict | None = None,
        token_to_student: dict | None = None,
    ) -> dict:
        """修订后的增量重算：区间=新版本批准周起受影响 slot 的场次。"""
        scope = self.impact_scope(new_version, current_week)
        class_slot_ids = self._class_slot_ids(class_id)
        scope = frozenset(k for k in scope if k.rsplit("#w", 1)[0] in class_slot_ids)
        return self.recompute(
            class_id, class_headcount, current_week, scope,
            reason=f"方案修订至 v{new_version}，按影响区间重算",
            adaptations=adaptations, token_to_student=token_to_student,
            basis_versions=(new_version,),
        )

    def _class_slot_ids(self, class_id: str) -> set[str]:
        ids: set[str] = set()
        for plan in self.registry.history(self.school_id, self.semester):
            ids.update(s.slot_id for s in plan.slots_for(class_id))
        return ids
