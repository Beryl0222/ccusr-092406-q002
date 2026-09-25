"""方案版本生效时点的回归测试。

复现并锁定学期初事故的修复：系统收到事件序号后，不能用后来才批准的第二版
课表去核对第一版期间的课程。覆盖：

- 提交/批准/取代都保留可比较的账本位置，同序号边界由 (at, seq) 确定；
- 批准间隙（提交未批准、版本交替空窗）的事件无版本可引用，不判异常；
- 多次取代时每个位置只解析到“当时已批准且尚未被替代”的版本；
- 迟到上传按实际发生位置而非接收顺序匹配版本；
- 账本乱序重放与服务恢复后版本选择完全一致；
- 方案修订只重算受影响（场地/教师/应急替代/技能）区间，已完成复核的
  历史结论保留原依据并追加更正；
- 家长视图、班级覆盖率与教研异常引用同一方案版本。
"""

import random
import unittest

from pe_domain.coverage import ClassCoverage, SkillCoverage, compute_class_coverage
from pe_domain.events import (
    Occasion,
    SessionMode,
    TeacherReport,
    VenueObservation,
)
from pe_domain.ledger import EventLedger
from pe_domain.models import (
    ActivityKind,
    PlanSlot,
    SemesterPlan,
    SkillGoal,
    Teacher,
    Venue,
    VenueType,
    WeatherAlternative,
)
from pe_domain.plans import PlanRegistry
from pe_domain.review import (
    AnomalyKind,
    ReviewBoard,
    ReviewStatus,
    detect_anomalies,
)
from pe_domain.visibility import (
    IdentityVault,
    build_parent_view,
    build_public_summary,
)

# ------------------------------------------------------------ 夹具

FIELD = Venue("V-FIELD", "室外操场", VenueType.OUTDOOR, 50)
FIELD2 = Venue("V-FIELD-2", "第二操场", VenueType.OUTDOOR, 50)
GYM = Venue("V-GYM", "体育馆", VenueType.INDOOR, 45)
ROOM = Venue("V-ROOM", "备用形体房", VenueType.INDOOR, 45)
VENUES = {v.venue_id: v for v in (FIELD, FIELD2, GYM, ROOM)}
T_WANG = Teacher("T-WANG", "王老师", frozenset({"BB", "急救"}))
T_CHEN = Teacher("T-CHEN", "陈老师", frozenset({"BB", "急救"}))
TEACHERS = {t.teacher_id: t for t in (T_WANG, T_CHEN)}
RAIN_GYM = WeatherAlternative("rain", "V-GYM", "室内球性练习", "POL-RAIN-01")
HEAT_GYM = WeatherAlternative("heat", "V-GYM", "室内低强度活动", "POL-HEAT-01")
RAIN_ROOM = WeatherAlternative("rain", "V-ROOM", "备用房室内活动", "POL-RAIN-02")
ALTS = (RAIN_GYM, HEAT_GYM)
GOAL_BB = SkillGoal("BB", "篮球", teach_weeks=(1, 2, 3, 4, 5),
                    practice_weeks=(1, 2, 3, 4, 5), match_weeks=())
HEADCOUNT = 40
SCHOOL, SEMESTER = "S", "2026-1"

# 教学周 -> 账本实际发生位置（周间留出方案提交/批准的空隙）
WEEK_AT = {1: 100, 2: 200, 3: 350, 4: 450, 5: 550}
WEEK_POS = lambda w: WEEK_AT[w]  # noqa: E731


def pe_slot(slot_id, *, venue="V-FIELD", teacher=T_WANG, skill="BB", alts=ALTS):
    return PlanSlot(
        slot_id=slot_id, class_id="C1", weekday=int(slot_id.rsplit("-", 1)[1]),
        kind=ActivityKind.PE_CLASS, week_parity="all",
        venue_id=venue, teacher_id=teacher.teacher_id, skill_code=skill,
        headcount=HEADCOUNT, weather_alternatives=tuple(alts),
    )


def base_slots():
    return [pe_slot(f"C1-PE-{d}") for d in range(1, 6)]


def plan_v1():
    return SemesterPlan(SCHOOL, SEMESTER, 0, tuple(base_slots()), (GOAL_BB,))


def plan_v2():
    """第二版：仅周一改到体育馆、换陈老师（场地+教师修订）。"""
    slots = tuple(
        pe_slot("C1-PE-1", venue="V-GYM", teacher=T_CHEN)
        if s.slot_id == "C1-PE-1" else s
        for s in base_slots()
    )
    return SemesterPlan(SCHOOL, SEMESTER, 0, slots, (GOAL_BB,))


def plan_v3():
    """第三版：周一再改到第二操场、陈老师不变（场地修订）。"""
    slots = tuple(
        pe_slot("C1-PE-1", venue="V-FIELD-2", teacher=T_CHEN)
        if s.slot_id == "C1-PE-1" else s
        for s in base_slots()
    )
    return SemesterPlan(SCHOOL, SEMESTER, 0, slots, (GOAL_BB,))


def occ(slot_id, week):
    return Occasion(slot_id, week, at=WEEK_AT[week])


def record(ledger, slot_id, week, *, venue="V-FIELD", teacher=T_WANG,
           skill="BB", mode=SessionMode.NORMAL, tokens=4, basis=""):
    """登记一场三方齐备、可确认的课；位置取该教学周的实际发生位置。"""
    o = occ(slot_id, week)
    venue_id = venue.venue_id if isinstance(venue, Venue) else venue
    teacher_id = teacher.teacher_id if isinstance(teacher, Teacher) else teacher
    ledger.append("teacher_report", TeacherReport(
        o, teacher_id, ActivityKind.PE_CLASS, skill, 40, mode, venue_id,
        basis_ref=basis))
    ledger.append("venue_observation", VenueObservation(o, venue_id, HEADCOUNT))
    token_ids = tokens if isinstance(tokens, (list, tuple)) else [f"tok{i}" for i in range(tokens)]
    for i, tok in enumerate(token_ids):
        ledger.append("sample_attendance", _att(tok, o, week, i))
    return o


def _att(token, o, week, i):
    from pe_domain.events import SampleAttendance
    ts = f"2026-09-{WEEK_AT[week] % 30 + i:02d}T10:00:00"
    return SampleAttendance(o, token, ts, ts, "online")


def approved_versions(v1_at=20, v2_at=300, v3_at=500):
    """建立账本+版本库：v1/v2/v3 在指定位置批准。"""
    ledger = EventLedger()
    registry = PlanRegistry(ledger)
    registry.submit(plan_v1(), VENUES, TEACHERS, submitted_by="admin", at=10)
    registry.approve(SCHOOL, SEMESTER, 1, at=v1_at)
    submitted = [plan_v1()]
    if v2_at is not None:
        registry.submit(plan_v2(), VENUES, TEACHERS, submitted_by="admin", at=v2_at - 50)
        registry.approve(SCHOOL, SEMESTER, 2, at=v2_at)
        submitted.append(plan_v2())
    if v3_at is not None:
        registry.submit(plan_v3(), VENUES, TEACHERS, submitted_by="admin", at=v3_at - 50)
        registry.approve(SCHOOL, SEMESTER, 3, at=v3_at)
        submitted.append(plan_v3())
    return ledger, registry


def rebuild(ledger, registry, current_week=5, **kw):
    return ledger.rebuild_class(
        "C1", None, HEADCOUNT, {}, {}, VENUES, current_week,
        registry=registry, school_id=SCHOOL, semester=SEMESTER,
        week_at=WEEK_POS, **kw,
    )


# ------------------------------------------------------------ 测试

class VersionPositionTest(unittest.TestCase):
    def test_submit_approve_supersede_keep_comparable_positions(self):
        ledger, registry = approved_versions()
        v1, v2, v3 = (registry.get(SCHOOL, SEMESTER, n) for n in (1, 2, 3))
        self.assertEqual((v1.submitted_at.at, v1.approved_at.at), (10, 20))
        self.assertEqual((v2.submitted_at.at, v2.approved_at.at), (250, 300))
        self.assertEqual((v3.submitted_at.at, v3.approved_at.at), (450, 500))
        # 取代位置与新版本批准位置相同
        self.assertEqual(v1.superseded_at.at, 300)
        self.assertEqual(v2.superseded_at.at, 500)
        self.assertIsNone(v3.superseded_at)
        self.assertEqual((v1.status, v2.status, v3.status),
                         ("superseded", "superseded", "approved"))

    def test_effective_on_approval_gap_returns_none(self):
        _, registry = approved_versions()
        # 提交(at10) 之后、批准(at20) 之前：尚无生效版本
        self.assertIsNone(registry.effective_version_at(SCHOOL, SEMESTER, 15))
        # v2 已提交(250)未批准(300) 的间隙：仍只能引用 v1
        self.assertEqual(registry.effective_version_at(SCHOOL, SEMESTER, 270), 1)
        # v3 已提交(450)未批准(500)：仍是 v2
        self.assertEqual(registry.effective_version_at(SCHOOL, SEMESTER, 480), 2)

    def test_same_position_boundary_resolved_by_seq(self):
        # 批准事件与授课事件同 at：先到达（seq 更小）的批准在该位置生效
        ledger = EventLedger()
        registry = PlanRegistry(ledger)
        registry.submit(plan_v1(), VENUES, TEACHERS, submitted_by="admin", at=300)
        registry.approve(SCHOOL, SEMESTER, 1, at=300)
        registry.submit(plan_v2(), VENUES, TEACHERS, submitted_by="admin", at=300)
        registry.approve(SCHOOL, SEMESTER, 2, at=300)
        v1, v2 = registry.get(SCHOOL, SEMESTER, 1), registry.get(SCHOOL, SEMESTER, 2)
        # v1 生效区间 [批准seq, 取代seq)：同 at 下边界半开
        self.assertTrue(v1.effective_at(v1.approved_at))
        self.assertFalse(v1.effective_at(v2.approved_at))
        self.assertTrue(v2.effective_at(v2.approved_at))

    def test_multiple_supersessions_pick_version_per_position(self):
        _, registry = approved_versions()
        expected = {100: 1, 200: 1, 300: 2, 350: 2, 450: 2, 500: 3, 550: 3}
        for at, version in expected.items():
            self.assertEqual(
                registry.effective_version_at(SCHOOL, SEMESTER, at), version,
                f"位置 {at} 应解析到 v{version}",
            )

    def test_revision_impact_lists_only_changed_dimensions(self):
        _, registry = approved_versions()
        impact2 = registry.revision_impact(SCHOOL, SEMESTER, 2)
        self.assertEqual(set(impact2["changed_slots"]), {"C1-PE-1"})
        self.assertEqual(
            set(impact2["changed_slots"]["C1-PE-1"]), {"venue", "teacher"}
        )
        impact3 = registry.revision_impact(SCHOOL, SEMESTER, 3)
        self.assertEqual(impact3["changed_slots"]["C1-PE-1"], ("venue",))
        # 未变化槽位不在影响面内
        self.assertNotIn("C1-PE-2", impact2["changed_slots"])


class EventVersionMatchingTest(unittest.TestCase):
    def test_early_sessions_checked_against_v1_not_latest_v2(self):
        """事故复现：第 1 周按第一版在操场由王老师上课，第二版晚些才批准，
        重建绝不能用第二版把旧场地/教师判成异常。"""
        ledger, registry = approved_versions()
        record(ledger, "C1-PE-1", 1)  # FIELD / T_WANG（v1 安排）
        record(ledger, "C1-PE-2", 1)
        rebuilt = rebuild(ledger, registry, current_week=1)
        s1 = next(s for s in rebuilt["sessions"] if s.occasion.key() == "C1-PE-1#w1")
        self.assertEqual(s1.plan_version, 1)
        self.assertTrue(s1.confirmed)
        self.assertFalse(any(n.startswith("plan_") for n in s1.notes))
        anomalies = detect_anomalies("C1", rebuilt)
        self.assertFalse(any(a.kind == AnomalyKind.PLAN_MISMATCH for a in anomalies))

    def test_later_approval_does_not_rewrite_early_conclusions(self):
        """第 1 周若实际按第二版上课，相对当时生效的 v1 即为不符，
        证据固定为 v1；v2 批准后重算仍引用 v1，不回写。"""
        ledger, registry = approved_versions()
        record(ledger, "C1-PE-1", 1, venue=GYM, teacher=T_CHEN)  # v2 的安排
        record(ledger, "C1-PE-1", 3)  # v2 已生效，却仍按 v1 在 FIELD/WANG 上课
        rebuilt = rebuild(ledger, registry, current_week=3)
        by_key = {s.occasion.key(): s for s in rebuilt["sessions"]}
        self.assertEqual(by_key["C1-PE-1#w1"].plan_version, 1)
        self.assertEqual(by_key["C1-PE-1#w3"].plan_version, 2)
        anomalies = detect_anomalies("C1", rebuilt)
        mismatch = {a.plan_version: a.evidence for a in anomalies
                    if a.kind == AnomalyKind.PLAN_MISMATCH}
        self.assertEqual(mismatch, {
            1: ("C1-PE-1#w1",),
            2: ("C1-PE-1#w3",),
        })
        # 再次重放（服务恢复后）结论相同
        again = rebuild(ledger, registry, current_week=3)
        self.assertEqual(
            {(a.plan_version, a.evidence) for a in detect_anomalies("C1", again)},
            {(a.plan_version, a.evidence) for a in anomalies},
        )

    def test_late_upload_matched_by_actual_position_not_receive_order(self):
        """第 1 周的课在 v2 批准之后才迟到上传，仍按第 1 周位置匹配 v1：
        按 v1 在 FIELD/WANG 上课不构成与 v2 的不符。"""
        ledger, registry = approved_versions()  # v2 已在 300 批准
        late = record(ledger, "C1-PE-1", 1)     # 接收序号很晚，但 at=100
        self.assertGreater(late.at, 0)
        rebuilt = rebuild(ledger, registry, current_week=1)
        s = next(x for x in rebuilt["sessions"] if x.occasion.key() == "C1-PE-1#w1")
        self.assertEqual(s.plan_version, 1)
        self.assertTrue(s.confirmed)
        self.assertFalse(any(n.startswith("plan_") for n in s.notes))

    def test_events_before_first_approval_are_uncovered_not_anomalies(self):
        """批准间隙：v1 直到位置 500 才批准，第 1 周（at100）的课与无事件
        槽位都无版本可核对——状态 unverified，既不判阴阳课表也不挂补课。"""
        ledger, registry = approved_versions(v1_at=500, v2_at=None, v3_at=None)
        record(ledger, "C1-PE-1", 1)
        rebuilt = rebuild(ledger, registry, current_week=1)
        self.assertIn("C1-PE-1#w1", rebuilt["uncovered"])
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "unverified")
        self.assertEqual(rebuilt["states"]["C1-PE-2#w1"], "unverified")
        self.assertEqual(rebuilt["missing_occasions"], ())
        self.assertEqual(rebuilt["pending_makeup"], ())
        self.assertEqual(detect_anomalies("C1", rebuilt), [])

    def test_version_basis_per_week_under_multiple_supersessions(self):
        ledger, registry = approved_versions()
        record(ledger, "C1-PE-2", 1)
        record(ledger, "C1-PE-2", 3, venue=GYM, teacher=T_CHEN)  # 该槽 v2 未改 → 不符
        record(ledger, "C1-PE-2", 5)
        rebuilt = rebuild(ledger, registry, current_week=5)
        basis = rebuilt["basis_versions"]
        self.assertEqual(basis["C1-PE-2#w1"], 1)
        self.assertEqual(basis["C1-PE-2#w3"], 2)
        self.assertEqual(basis["C1-PE-2#w5"], 3)


class ReplayDeterminismTest(unittest.TestCase):
    def _build(self):
        ledger, registry = approved_versions()
        record(ledger, "C1-PE-1", 1)
        record(ledger, "C1-PE-1", 3, venue=GYM, teacher=T_CHEN)
        record(ledger, "C1-PE-2", 4)
        return ledger

    def test_out_of_order_replay_gives_same_version_choice(self):
        ledger = self._build()
        plan_types = ("plan_submitted", "plan_approved")
        plan_entries = [e for e in ledger.entries() if e.event_type in plan_types]
        teach_entries = [e for e in ledger.entries() if e.event_type not in plan_types]
        # 授课事件乱序（含迟到）送达，方案事件保持先后
        shuffled = teach_entries[:]
        random.Random(7).shuffle(shuffled)
        messy = EventLedger.restore(plan_entries + shuffled)
        registry_messy = PlanRegistry(messy)

        clean = EventLedger.restore(ledger.entries())
        registry_clean = PlanRegistry(clean)
        a = rebuild(messy, registry_messy)
        b = rebuild(clean, registry_clean)
        self.assertEqual(a["basis_versions"], b["basis_versions"])
        self.assertEqual(a["states"], b["states"])
        self.assertEqual(a["uncovered"], b["uncovered"])
        self.assertEqual(
            [(x.kind, x.evidence, x.plan_version) for x in detect_anomalies("C1", a)],
            [(x.kind, x.evidence, x.plan_version) for x in detect_anomalies("C1", b)],
        )

    def test_service_recovery_from_append_only_entries(self):
        ledger = self._build()
        recovered = EventLedger.restore(ledger.entries())
        registry_recovered = PlanRegistry(recovered)
        # 位置（at 与接收 seq）原样恢复
        self.assertEqual(
            [(e.seq, e.position.at) for e in recovered.entries()],
            [(e.seq, e.position.at) for e in ledger.entries()],
        )
        before = rebuild(ledger, PlanRegistry(ledger))
        after = rebuild(recovered, registry_recovered)
        self.assertEqual(before["basis_versions"], after["basis_versions"])
        self.assertEqual(before["states"], after["states"])


    def test_late_plan_approval_received_after_events_uses_actual_position(self):
        """v2 实际在位置 280 批准，但批准事件在第 3 周课程之后才送达；
        重放恢复后仍按实际批准位置生效：第 2 周用 v1、第 3 周用 v2。"""
        ledger = EventLedger()
        live = PlanRegistry(ledger)
        live.submit(plan_v1(), VENUES, TEACHERS, submitted_by="admin", at=10)
        live.approve(SCHOOL, SEMESTER, 1, at=20)
        live.submit(plan_v2(), VENUES, TEACHERS, submitted_by="admin", at=250)
        record(ledger, "C1-PE-2", 2)                                  # at=200，v1 期
        record(ledger, "C1-PE-1", 3, venue=GYM, teacher=T_CHEN)       # at=350，按 v2 上
        # 批准事件迟到：实际发生在 280，接收序号在第 3 周事件之后
        ledger.append_plan_event(
            PlanRegistry.APPROVE_EVENT,
            {"kind": PlanRegistry.APPROVE_EVENT, "school_id": SCHOOL,
             "semester": SEMESTER, "version": 2},
            at=280,
        )
        recovered_registry = PlanRegistry(EventLedger.restore(ledger.entries()))
        self.assertEqual(recovered_registry.effective_version_at(SCHOOL, SEMESTER, 200), 1)
        self.assertEqual(recovered_registry.effective_version_at(SCHOOL, SEMESTER, 350), 2)
        rebuilt = rebuild(ledger, recovered_registry, current_week=3)
        by_key = {s.occasion.key(): s for s in rebuilt["sessions"]}
        self.assertEqual(by_key["C1-PE-2#w2"].plan_version, 1)
        self.assertEqual(by_key["C1-PE-1#w3"].plan_version, 2)
        self.assertFalse(any(
            n.startswith("plan_") for s in rebuilt["sessions"] for n in s.notes
        ))


class ImpactScopeTest(unittest.TestCase):
    def test_makeup_backfill_uses_original_week_version(self):
        """第 1 周（v1 期）缺课，第 3 周（v2 期）补课回填：
        原场次依据版本取原周 v1，补课场次依据版本取补课周 v2。"""
        from pe_domain.events import SampleAttendance
        ledger, registry = approved_versions()
        orig = occ("C1-PE-1", 1)
        ledger.append("takeover", {"slot_id": "C1-PE-1", "week": 1,
                                   "subject": "数学", "ref": ""})
        makeup = occ("C1-PE-3", 3)
        ledger.append("makeup_plan", {"occasion": makeup, "makeup_for": orig})
        ledger.append("teacher_report", TeacherReport(
            makeup, T_WANG.teacher_id, ActivityKind.PE_CLASS, "BB", 40,
            SessionMode.MAKEUP, "V-FIELD", basis_ref="MK-1", makeup_for=orig))
        ledger.append("venue_observation", VenueObservation(makeup, "V-FIELD", HEADCOUNT))
        for i in range(4):
            ts = f"2026-09-{12 + i}T10:00:00"
            ledger.append("sample_attendance", SampleAttendance(makeup, f"m{i}", ts, ts, "online"))
        rebuilt = rebuild(ledger, registry, current_week=3)
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "completed")
        self.assertEqual(rebuilt["states"]["C1-PE-3#w3"], "completed_makeup")
        self.assertEqual(rebuilt["basis_versions"]["C1-PE-1#w1"], 1)
        self.assertEqual(rebuilt["basis_versions"]["C1-PE-3#w3"], 2)
        self.assertIn("C1-PE-1#w1", rebuilt["made_up"])
        self.assertNotIn("C1-PE-1#w1", rebuilt["pending_makeup"])

    def test_recompute_only_affected_range(self):
        ledger, registry = approved_versions()  # v2 仅改 C1-PE-1，300 生效
        for w in (1, 2, 3):
            record(ledger, "C1-PE-1", w,
                   **({"venue": GYM, "teacher": T_CHEN} if w == 3 else {}))
            record(ledger, "C1-PE-2", w)
        impact = registry.revision_impact(SCHOOL, SEMESTER, 2)
        affected_slots = frozenset(impact["changed_slots"])
        # 只重算受影响槽位、且位于新版本生效区间（第 3 周起）的场次
        scoped = rebuild(
            ledger, registry, current_week=3,
            scope={"slot_ids": affected_slots, "weeks": range(3, 4)},
        )
        self.assertTrue(scoped["scope"] is not None)
        self.assertEqual(set(scoped["states"]), {"C1-PE-1#w3"})
        self.assertEqual(scoped["basis_versions"]["C1-PE-1#w3"], 2)
        self.assertEqual(len(scoped["sessions"]), 1)
        # 影响区间之外：第 1/2 周与其他槽位不出现在重算结果中（历史结论保留）
        self.assertNotIn("C1-PE-1#w1", scoped["states"])
        self.assertNotIn("C1-PE-2#w3", scoped["states"])


class WeatherAlternativeRevisionTest(unittest.TestCase):
    def _plans_alt_revision(self):
        """v2 仅把周一降雨替代从体育馆改为备用形体房。"""
        slots = tuple(
            pe_slot("C1-PE-1", alts=(RAIN_ROOM, HEAT_GYM))
            if s.slot_id == "C1-PE-1" else s
            for s in base_slots()
        )
        v2 = SemesterPlan(SCHOOL, SEMESTER, 0, slots, (GOAL_BB,))
        ledger = EventLedger()
        registry = PlanRegistry(ledger)
        registry.submit(plan_v1(), VENUES, TEACHERS, submitted_by="admin", at=10)
        registry.approve(SCHOOL, SEMESTER, 1, at=20)
        registry.submit(v2, VENUES, TEACHERS, submitted_by="admin", at=250)
        registry.approve(SCHOOL, SEMESTER, 2, at=300)
        return ledger, registry

    def test_weather_alternative_change_is_impact_dimension(self):
        _, registry = self._plans_alt_revision()
        impact = registry.revision_impact(SCHOOL, SEMESTER, 2)
        self.assertEqual(impact["changed_slots"]["C1-PE-1"], ("weather_alternative",))

    def test_rain_alt_checked_against_version_at_occasion_time(self):
        # 第 1 周（v1 期）降雨在体育馆 -> 合规；第 3 周（v2 期）降雨仍去体育馆
        # 而 v2 已改为形体房 -> 与当时版本不符；迟到上传不改变这一判定。
        ledger, registry = self._plans_alt_revision()
        record(ledger, "C1-PE-1", 1, venue=GYM, mode=SessionMode.RAIN_ALT,
               basis="POL-RAIN-01")
        record(ledger, "C1-PE-1", 3, venue=GYM, mode=SessionMode.RAIN_ALT,
               basis="POL-RAIN-01")  # 接收晚，at=350 仍属 v2 期
        rebuilt = rebuild(ledger, registry, current_week=3)
        by_key = {s.occasion.key(): s for s in rebuilt["sessions"]}
        w1, w3 = by_key["C1-PE-1#w1"], by_key["C1-PE-1#w3"]
        self.assertEqual(w1.plan_version, 1)
        self.assertEqual(w3.plan_version, 2)
        self.assertFalse(any("plan_" in n for n in w1.notes))
        self.assertTrue(any(n.startswith("plan_venue_mismatch") for n in w3.notes))
        mismatch = [a for a in detect_anomalies("C1", rebuilt)
                    if a.kind == AnomalyKind.PLAN_MISMATCH]
        self.assertEqual([a.plan_version for a in mismatch], [2])
        self.assertEqual(mismatch[0].evidence, ("C1-PE-1#w3",))


class ReviewCorrectionTest(unittest.TestCase):
    def test_closed_case_keeps_basis_and_appends_correction(self):
        ledger, registry = approved_versions()
        record(ledger, "C1-PE-1", 1, venue=GYM, teacher=T_CHEN)  # 与 v1 不符
        rebuilt = rebuild(ledger, registry, current_week=1)
        anomalies = detect_anomalies("C1", rebuilt)
        self.assertTrue(anomalies)

        board = ReviewBoard()
        case = board.open_case("C1", anomalies)
        board.resolve(case.case_id, ReviewStatus.MAKEUP_ORDERED, "教研员刘",
                      "第 1 周场地与 v1 不符，安排补课")
        frozen_conclusion = case.conclusion
        self.assertEqual(case.basis["plan_versions"], (1,))
        self.assertEqual(case.basis["anomalies"], tuple(anomalies))

        # v2 生效后重算出现新版本依据的异常，只能追加更正，不能二次裁定
        record(ledger, "C1-PE-1", 3)  # v2 下应在 GYM，实际在 FIELD
        rebuilt2 = rebuild(ledger, registry, current_week=3)
        new_findings = [a for a in detect_anomalies("C1", rebuilt2)
                        if a.plan_version == 2]
        self.assertTrue(new_findings)
        correction = board.append_correction(
            case.case_id, "教研员刘",
            "第二版生效后第 3 周场地仍不符，并入原案跟踪",
            anomalies=tuple(new_findings),
        )
        self.assertEqual(correction.basis_versions, (2,))
        self.assertEqual(len(case.corrections), 1)
        # 原结论与原依据原样保留
        self.assertEqual(case.conclusion, frozen_conclusion)
        self.assertEqual(case.basis["plan_versions"], (1,))
        self.assertEqual(case.status, ReviewStatus.MAKEUP_ORDERED)
        with self.assertRaises(ValueError):
            board.resolve(case.case_id, ReviewStatus.CLEARED, "教研员刘", "禁止二次裁定")


class SharedVersionAcrossViewsTest(unittest.TestCase):
    def setUp(self):
        ledger, registry = approved_versions()
        self.vault = IdentityVault("salt")
        self.tokens = []
        for i in range(4):
            self.tokens.append(self.vault.enroll(f"stu-{i}"))
        self.vault.link_parent("parent-0", "stu-0")
        record(ledger, "C1-PE-1", 1, tokens=self.tokens)   # v1 期间
        record(ledger, "C1-PE-2", 1, tokens=self.tokens)
        record(ledger, "C1-PE-1", 3, tokens=self.tokens)   # v2 期间按 v1 上课 → 不符
        self.ledger, self.registry = ledger, registry
        self.rebuilt = rebuild(ledger, registry, current_week=3)
        self.coverage = compute_class_coverage("C1", self.rebuilt, (GOAL_BB,))

    def test_class_coverage_cites_effective_versions(self):
        self.assertEqual(self.coverage.plan_versions, (1, 2))
        # 无版本场次为 0（本例各周均有批准版本）
        self.assertEqual(self.coverage.unverified, 0)

    def test_parent_view_cites_same_versions(self):
        view = build_parent_view("parent-0", self.vault, {}, self.rebuilt["sessions"])
        self.assertEqual(view.plan_versions, (1, 2))
        versions = {row[3] for row in view.recent_minutes}
        self.assertTrue(versions <= {1, 2})
        self.assertTrue(view.recent_minutes)  # 看到本人子女的确认场次时长

    def test_public_summary_and_anomalies_share_version_source(self):
        def synthetic(cid):
            return ClassCoverage(
                class_id=cid, total_occasions=10, completed=10, pending_makeup=0,
                in_review=0,
                skill_coverage=(SkillCoverage("BB", True, 1.0, 1.0, (), ()),),
                plan_versions=(1, 2),
            )
        coverages = {"C1": self.coverage, "C2": synthetic("C2"), "C3": synthetic("C3")}
        summary = build_public_summary(SCHOOL, coverages, {})
        self.assertTrue(summary.published)
        self.assertEqual(summary.plan_versions, (1, 2))
        # 教研异常引用的版本必须来自同一版本集合
        anomalies = detect_anomalies("C1", self.rebuilt)
        cited = {a.plan_version for a in anomalies if a.plan_version is not None}
        self.assertTrue(cited <= set(summary.plan_versions))
        self.assertIn(2, cited)  # 第 3 周按 v1 上课被 v2 判为不符


if __name__ == "__main__":
    unittest.main()
