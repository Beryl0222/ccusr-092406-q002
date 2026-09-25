"""方案版本生效时点的回归测试。

覆盖需求点名的边界：

- 同序号边界：批准（phase=1）与业务事件（phase=2）同刻时的版本选择；
- 批准间隙：首个版本批准之前的场次不被后来批准回写；
- 多次取代：v1→v2→v3 期间各周分别绑定当时生效版本；
- 迟到上传：按实际发生位置（occurred_at）而非接收顺序匹配版本；
- 乱序重放/服务恢复：归一化后版本选择与结论逐字段一致；
- 影响区间重算：只重算受修订影响（场地/教师/天气应急替代/技能）的场次，
  已结案复核保留原依据并只追加更正；
- 家长视图、班级覆盖率、教研异常引用同一方案版本。
"""

import datetime
import random
import unittest

from pe_domain.coverage import compute_class_coverage
from pe_domain.events import (
    Occasion,
    SampleAttendance,
    SessionMode,
    TeacherReport,
    VenueObservation,
)
from pe_domain.models import (
    ActivityKind,
    LedgerPosition,
    PlanSlot,
    SemesterPlan,
    SkillGoal,
    Teacher,
    Venue,
    VenueType,
    WeatherAlternative,
)
from pe_domain.plans import (
    NoApprovedPlanAtPosition,
    PlanRegistry,
    affected_dimensions,
    affected_slots,
)
from pe_domain.review import (
    Anomaly,
    AnomalyKind,
    ReviewBoard,
    ReviewStatus,
    detect_anomalies,
)
from pe_domain.runtime import SchoolRuntime
from pe_domain.visibility import (
    IdentityVault,
    build_parent_view,
    build_public_summary,
)

# ---------------------------------------------------------------- 夹具

FIELD = Venue("V-FIELD", "室外操场", VenueType.OUTDOOR, 50)
FIELD2 = Venue("V-FIELD-2", "西操场", VenueType.OUTDOOR, 50)
GYM = Venue("V-GYM", "体育馆", VenueType.INDOOR, 45)
ROOM2 = Venue("V-ROOM-2", "第二形体房", VenueType.INDOOR, 45)
VENUES = {v.venue_id: v for v in (FIELD, FIELD2, GYM, ROOM2)}

T_WANG = Teacher("T-WANG", "王老师", frozenset({"BB", "TJ", "急救"}))
T_LI = Teacher("T-LI", "李老师", frozenset({"BB", "TJ", "急救"}))
TEACHERS = {t.teacher_id: t for t in (T_WANG, T_LI)}

RAIN_GYM = WeatherAlternative("rain", "V-GYM", "室内球性练习", "POL-RAIN-01")
HEAT_GYM = WeatherAlternative("heat", "V-GYM", "室内低强度活动", "POL-HEAT-01")
RAIN_ROOM = WeatherAlternative("rain", "V-ROOM-2", "室内柔韧练习", "POL-RAIN-02")
HEAT_ROOM = WeatherAlternative("heat", "V-ROOM-2", "室内低强度活动", "POL-HEAT-02")

GOAL_BB = SkillGoal("BB", "篮球", teach_weeks=(1, 2), practice_weeks=(1, 2, 3, 4),
                    match_weeks=(3, 4))
GOAL_TJ = SkillGoal("TJ", "田径", teach_weeks=(3, 4), practice_weeks=(3, 4, 5, 6),
                    match_weeks=(5, 6))
GOALS = (GOAL_BB, GOAL_TJ)

SCHOOL = "S"
SEMESTER = "2026-1"
HEADCOUNT = 40
MONDAY_W1 = datetime.date(2026, 8, 31)  # 教学周 1 的周一（经日历核对为周一）


def ts(week: int, weekday: int, hour: int = 10, minute: int = 0) -> str:
    """教学周 week（1 起）、星期 weekday（1=周一）的 ISO 时刻。"""
    day = MONDAY_W1 + datetime.timedelta(days=(week - 1) * 7 + (weekday - 1))
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00"


def build_plan(*, venue="V-FIELD", teacher="T-WANG", skill="BB",
               alts=(RAIN_GYM, HEAT_GYM)) -> SemesterPlan:
    slots = tuple(
        PlanSlot(
            slot_id=f"C1-PE-{d}", class_id="C1", weekday=d,
            kind=ActivityKind.PE_CLASS, week_parity="all",
            venue_id=venue, teacher_id=teacher, skill_code=skill,
            headcount=HEADCOUNT, weather_alternatives=tuple(alts),
        )
        for d in range(1, 6)
    )
    return SemesterPlan(SCHOOL, SEMESTER, 0, slots, GOALS)


def new_runtime() -> SchoolRuntime:
    return SchoolRuntime(SCHOOL, SEMESTER, VENUES, TEACHERS)


def record_session(rt: SchoolRuntime, slot_id: str, week: int, *,
                   venue="V-FIELD", teacher="T-WANG", skill="BB",
                   mode=SessionMode.NORMAL, basis_ref="",
                   makeup_for=None, tokens=4, at=None, token_ids=None):
    """记录一场经三方确认的课；occurred_at 是实际发生位置。"""
    weekday = int(slot_id.rsplit("-", 1)[1])
    when = at or ts(week, weekday)
    occ = Occasion(slot_id, week)
    rt.record("teacher_report", TeacherReport(
        occ, teacher, ActivityKind.PE_CLASS, skill, 40, mode, venue,
        basis_ref=basis_ref, makeup_for=makeup_for), occurred_at=when)
    rt.record("venue_observation", VenueObservation(occ, venue, HEADCOUNT),
              occurred_at=when)
    ids = token_ids if token_ids is not None else [f"tk{i}" for i in range(tokens)]
    for tid in ids:
        rt.record("sample_attendance",
                  SampleAttendance(occ, tid, when, when, "online"),
                  occurred_at=when)
    return occ


def record_full_week(rt: SchoolRuntime, week: int, **kw):
    for d in range(1, 6):
        record_session(rt, f"C1-PE-{d}", week, **kw)


# ================================================================ 版本库

class PlanRegistryTimelineTest(unittest.TestCase):
    def test_submit_approve_supersede_keep_comparable_positions(self):
        registry = PlanRegistry()
        p1 = registry.submit(build_plan(), VENUES, TEACHERS, submitted_by="admin",
                             at=LedgerPosition(1, "2026-08-25T09:00:00", 0))
        a1 = registry.approve(SCHOOL, SEMESTER, 1,
                              at=LedgerPosition(2, "2026-08-28T09:00:00", 1))
        p2 = registry.submit(build_plan(venue="V-FIELD-2"), VENUES, TEACHERS,
                             submitted_by="admin",
                             at=LedgerPosition(3, "2026-09-11T09:00:00", 0))
        a2 = registry.approve(SCHOOL, SEMESTER, 2,
                              at=LedgerPosition(4, "2026-09-11T18:00:00", 1))

        self.assertEqual(p1.submitted_at.occurred_at, "2026-08-25T09:00:00")
        self.assertEqual(a1.approved_at.occurred_at, "2026-08-28T09:00:00")
        # v1 的取代位置 = v2 的批准位置（同刻开区间）
        v1_after = registry.get(SCHOOL, SEMESTER, 1)
        self.assertEqual(v1_after.status, "superseded")
        self.assertEqual(v1_after.superseded_at, a2.approved_at)
        timeline = registry.timeline(SCHOOL, SEMESTER)
        self.assertEqual(
            [(act, ver) for _, act, ver in timeline],
            [("submitted", 1), ("approved", 1), ("submitted", 2),
             ("superseded", 1), ("approved", 2)],
        )
        # 取代位置与新批准位置同刻可比较
        self.assertEqual(timeline[-2][0], timeline[-1][0])

    def test_same_position_boundary_phase_breaks_tie(self):
        """同刻：批准（phase=1）先于业务事件（phase=2）；业务事件引用新版。"""
        registry = PlanRegistry()
        registry.submit(build_plan(), VENUES, TEACHERS, submitted_by="admin",
                        at=LedgerPosition(1, "2026-08-25T09:00:00", 0))
        registry.approve(SCHOOL, SEMESTER, 1,
                         at=LedgerPosition(2, "2026-08-28T09:00:00", 1))
        registry.submit(build_plan(venue="V-FIELD-2"), VENUES, TEACHERS,
                        submitted_by="admin",
                        at=LedgerPosition(3, "2026-09-12T09:00:00", 0))
        registry.approve(SCHOOL, SEMESTER, 2,
                         at=LedgerPosition(4, "2026-09-14T10:00:00", 1))

        # 同刻的业务事件（phase=2）必须落在 v2；前一分钟仍是 v1
        business = LedgerPosition(5, "2026-09-14T10:00:00", 2)
        just_before = LedgerPosition(6, "2026-09-14T09:59:59", 2)
        self.assertEqual(
            registry.version_effective_at(SCHOOL, SEMESTER, business).version, 2)
        self.assertEqual(
            registry.version_effective_at(SCHOOL, SEMESTER, just_before).version, 1)

    def test_approval_gap_before_first_approval_is_not_backfilled(self):
        registry = PlanRegistry()
        registry.submit(build_plan(), VENUES, TEACHERS, submitted_by="admin",
                        at=LedgerPosition(1, "2026-08-25T09:00:00", 0))
        early = LedgerPosition(2, "2026-09-02T10:00:00", 2)
        with self.assertRaises(NoApprovedPlanAtPosition):
            registry.version_effective_at(SCHOOL, SEMESTER, early)
        # 第 2 周周三才批准
        registry.approve(SCHOOL, SEMESTER, 1,
                         at=LedgerPosition(3, "2026-09-09T12:00:00", 1))
        # 早期位置依然无版本——晚批准不回写
        with self.assertRaises(NoApprovedPlanAtPosition):
            registry.version_effective_at(SCHOOL, SEMESTER, early)
        after = LedgerPosition(4, "2026-09-10T10:00:00", 2)
        self.assertEqual(
            registry.version_effective_at(SCHOOL, SEMESTER, after).version, 1)

    def test_submitted_version_cannot_check_events(self):
        registry = PlanRegistry()
        registry.submit(build_plan(), VENUES, TEACHERS, submitted_by="admin",
                        at=LedgerPosition(1, "2026-08-25T09:00:00", 0))
        with self.assertRaises(NoApprovedPlanAtPosition):
            registry.version_effective_at(
                SCHOOL, SEMESTER, LedgerPosition(2, "2026-09-01T10:00:00", 2))
        registry.approve(SCHOOL, SEMESTER, 1,
                         at=LedgerPosition(3, "2026-08-28T09:00:00", 1))
        # 已批准不可重复批准（状态机保护）
        with self.assertRaises(ValueError):
            registry.approve(SCHOOL, SEMESTER, 1,
                             at=LedgerPosition(4, "2026-08-29T09:00:00", 1))
        # 已取代也不可再批
        registry.submit(build_plan(venue="V-FIELD-2"), VENUES, TEACHERS,
                        submitted_by="a",
                        at=LedgerPosition(5, "2026-09-10T09:00:00", 0))
        registry.approve(SCHOOL, SEMESTER, 2,
                         at=LedgerPosition(6, "2026-09-11T18:00:00", 1))
        with self.assertRaises(ValueError):
            registry.approve(SCHOOL, SEMESTER, 1,
                             at=LedgerPosition(7, "2026-09-12T18:00:00", 1))

    def test_multiple_supersessions_each_interval_picks_its_version(self):
        registry = PlanRegistry()
        registry.submit(build_plan(), VENUES, TEACHERS, submitted_by="a",
                        at=LedgerPosition(1, "2026-08-25T09:00:00", 0))
        registry.approve(SCHOOL, SEMESTER, 1,
                         at=LedgerPosition(2, "2026-08-28T09:00:00", 1))
        registry.submit(build_plan(venue="V-FIELD-2"), VENUES, TEACHERS,
                        submitted_by="a",
                        at=LedgerPosition(3, "2026-09-10T09:00:00", 0))
        registry.approve(SCHOOL, SEMESTER, 2,
                         at=LedgerPosition(4, "2026-09-11T18:00:00", 1))
        registry.submit(build_plan(teacher="T-LI"), VENUES, TEACHERS,
                        submitted_by="a",
                        at=LedgerPosition(5, "2026-09-17T09:00:00", 0))
        registry.approve(SCHOOL, SEMESTER, 3,
                         at=LedgerPosition(6, "2026-09-18T18:00:00", 1))

        def v(at: str) -> int:
            return registry.version_effective_at(
                SCHOOL, SEMESTER, LedgerPosition(9, at, 2)).version

        self.assertEqual(v("2026-09-01T10:00:00"), 1)  # 第1周
        self.assertEqual(v("2026-09-10T10:00:00"), 1)  # 第2周周四，v2 未批
        self.assertEqual(v("2026-09-14T10:00:00"), 2)  # 第3周
        self.assertEqual(v("2026-09-21T10:00:00"), 3)  # 第4周

    def test_affected_slots_detects_four_dimensions_only(self):
        v1 = build_plan()
        venue_plan = build_plan(venue="V-FIELD-2")
        teacher_plan = build_plan(teacher="T-LI")
        skill_plan = build_plan(skill="TJ")
        alt_plan = build_plan(alts=(RAIN_ROOM, HEAT_ROOM))
        # 无关变化（版本号/状态）不影响任何 slot
        self.assertEqual(affected_slots(v1, venue_plan),
                         {f"C1-PE-{d}" for d in range(1, 6)})
        self.assertEqual(affected_dimensions(v1, venue_plan), frozenset({"venue"}))
        self.assertEqual(affected_dimensions(v1, teacher_plan), frozenset({"teacher"}))
        self.assertEqual(affected_dimensions(v1, skill_plan), frozenset({"skill"}))
        self.assertEqual(affected_dimensions(v1, alt_plan), frozenset({"weather_alt"}))
        self.assertEqual(affected_slots(v1, v1.with_status(version=7)), frozenset())

    def test_registry_journal_rebuild_preserves_selection(self):
        registry = PlanRegistry()
        registry.submit(build_plan(), VENUES, TEACHERS, submitted_by="a",
                        at=LedgerPosition(1, "2026-08-25T09:00:00", 0))
        registry.approve(SCHOOL, SEMESTER, 1,
                         at=LedgerPosition(2, "2026-08-28T09:00:00", 1))
        registry.submit(build_plan(venue="V-FIELD-2"), VENUES, TEACHERS,
                        submitted_by="a",
                        at=LedgerPosition(3, "2026-09-10T09:00:00", 0))
        registry.approve(SCHOOL, SEMESTER, 2,
                         at=LedgerPosition(4, "2026-09-11T18:00:00", 1))
        restored = PlanRegistry.from_journal(registry.journal(), VENUES, TEACHERS)
        for at, expected in (("2026-09-01T10:00:00", 1), ("2026-09-14T10:00:00", 2)):
            pos = LedgerPosition(9, at, 2)
            self.assertEqual(
                restored.version_effective_at(SCHOOL, SEMESTER, pos).version,
                registry.version_effective_at(SCHOOL, SEMESTER, pos).version,
            )
        self.assertEqual(
            restored.get(SCHOOL, SEMESTER, 1).superseded_at,
            registry.get(SCHOOL, SEMESTER, 1).superseded_at,
        )


# ================================================================ 版本化重放

class VersionedReplayTest(unittest.TestCase):
    def _two_version_runtime(self, *, v2_approved_at="2026-09-11T18:00:00"):
        """v1=FIELD/BB 学期前批准；v2=FIELD2 第2周周五晚批准（第3周起生效）。"""
        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        rt.submit_plan(build_plan(venue="V-FIELD-2"), submitted_by="admin",
                       occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at=v2_approved_at)
        return rt

    def test_occasions_bind_to_version_at_occurrence(self):
        rt = self._two_version_runtime()
        record_full_week(rt, 1)                                 # v1 期间
        record_full_week(rt, 3, venue="V-FIELD-2")             # v2 期间
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        versions = rebuilt["plan_versions"]
        self.assertEqual({versions[f"C1-PE-{d}#w1"] for d in range(1, 6)}, {1})
        self.assertEqual({versions[f"C1-PE-{d}#w3"] for d in range(1, 6)}, {2})

    def test_later_version_does_not_flag_early_venue_arrangement(self):
        """开篇事故：v1 在 FIELD 上课，v2 改 FIELD2 后，旧场次不得被判异常。"""
        rt = self._two_version_runtime()
        record_full_week(rt, 1)                                 # 旧场地 FIELD
        record_full_week(rt, 3, venue="V-FIELD-2")             # 新场地 FIELD2
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        anomalies = detect_anomalies("C1", rebuilt)
        self.assertFalse(
            [a for a in anomalies if a.kind is AnomalyKind.PLAN_DEVIATION],
            f"旧场地安排被新版本回写误判：{anomalies}",
        )

    def test_later_version_does_not_flag_early_teacher_or_skill(self):
        """v2 同时改教师与技能目标：第1周的王老师/BB 仍按 v1 核对，不判偏离。"""
        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        rt.submit_plan(build_plan(teacher="T-LI", skill="TJ"),
                       submitted_by="admin", occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        record_full_week(rt, 1)                                   # 王老师 / BB
        record_full_week(rt, 3, venue="V-FIELD", teacher="T-LI", skill="TJ")
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        self.assertEqual({rebuilt["plan_versions"][f"C1-PE-{d}#w1"]
                          for d in range(1, 6)}, {1})
        self.assertFalse(
            [a for a in detect_anomalies("C1", rebuilt)
             if a.kind is AnomalyKind.PLAN_DEVIATION],
            "旧教师/旧技能安排被第二版课表回写误判",
        )

    def test_deviation_under_new_version_is_flagged_against_new_version(self):
        rt = self._two_version_runtime()
        record_full_week(rt, 1)                                 # v1：FIELD，合规
        # v2 生效后仍在旧场地 FIELD 上课（第3周周一）→ 相对 v2 偏离
        record_session(rt, "C1-PE-1", 3, venue="V-FIELD")
        for d in range(2, 6):
            record_session(rt, f"C1-PE-{d}", 3, venue="V-FIELD-2")
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        deviations = [a for a in detect_anomalies("C1", rebuilt)
                      if a.kind is AnomalyKind.PLAN_DEVIATION]
        self.assertEqual(len(deviations), 1)
        self.assertEqual(deviations[0].evidence, ("C1-PE-1#w3",))
        self.assertEqual(deviations[0].plan_versions, (2,))

    def test_late_upload_matches_actual_occurrence_not_receive_order(self):
        """第3周才补传的第1周证据：按第1周位置匹配 v1。

        接收顺序（append 顺序）故意安排为：先写第3周 v2 的课，再补传第1周
        的教师/场地/签到（模拟服务恢复后补送）。
        """
        rt = self._two_version_runtime()
        record_full_week(rt, 3, venue="V-FIELD-2")
        # 迟到的补传：发生位置仍是第1周（append 在最后 = 接收最晚）
        late_occ = record_session(rt, "C1-PE-1", 1)
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        self.assertEqual(rebuilt["plan_versions"]["C1-PE-1#w1"], 1)
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "completed")
        session = next(s for s in rebuilt["sessions"] if s.occasion == late_occ)
        self.assertEqual(session.plan_version, 1)
        self.assertTrue(session.confirmed)

    def test_late_approval_never_rewrites_early_conclusions(self):
        """v1 第2周周三才批准：第1-2周周一二的场次处批准间隙，状态 no_plan。"""
        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at=ts(2, 3, 12))  # 第2周周三中午
        record_full_week(rt, 1)
        record_session(rt, "C1-PE-1", 2)   # 第2周周一：批准前
        record_session(rt, "C1-PE-4", 2)   # 第2周周四：批准后
        rebuilt = rt.rebuild("C1", HEADCOUNT, 2)
        self.assertEqual(rebuilt["states"]["C1-PE-1#w1"], "no_plan")
        self.assertEqual(rebuilt["states"]["C1-PE-1#w2"], "no_plan")
        self.assertEqual(rebuilt["states"]["C1-PE-4#w2"], "completed")
        self.assertEqual(rebuilt["plan_versions"]["C1-PE-4#w2"], 1)
        # no_plan 场次不计阴阳课表（不能用后批准的版本回写）；
        # 批准（周三）之后、周五无事件，才是真正的缺口且引用 v1
        anomalies = detect_anomalies("C1", rebuilt)
        yin_yang = [a for a in anomalies if a.kind is AnomalyKind.YIN_YANG]
        self.assertEqual([a.evidence for a in yin_yang], [("C1-PE-5#w2",)])
        self.assertEqual(yin_yang[0].plan_versions, (1,))
        gap_keys = {k for k, v in rebuilt["states"].items() if v == "no_plan"}
        self.assertNotIn("C1-PE-5#w2", gap_keys)
        self.assertIn("C1-PE-1#w1", gap_keys)
        self.assertIn("C1-PE-1#w2", gap_keys)

    def test_out_of_order_ingest_normalizes_to_same_selection(self):
        rt = self._two_version_runtime()
        record_full_week(rt, 1)
        record_full_week(rt, 3, venue="V-FIELD-2")

        restored = SchoolRuntime.from_journal(
            tuple(reversed(rt.journal())),  # 完全逆接收顺序恢复
            SCHOOL, SEMESTER, VENUES, TEACHERS,
        )
        a = rt.rebuild("C1", HEADCOUNT, 3)
        b = restored.rebuild("C1", HEADCOUNT, 3)
        self.assertEqual(b["plan_versions"], a["plan_versions"])
        self.assertEqual(b["states"], a["states"])
        self.assertEqual(
            [(s.occasion.key(), s.confirmed, s.plan_version) for s in b["sessions"]],
            [(s.occasion.key(), s.confirmed, s.plan_version) for s in a["sessions"]],
        )

    def test_shuffled_replay_and_normalized_are_identical(self):
        rt = self._two_version_runtime()
        record_full_week(rt, 1)
        record_full_week(rt, 3, venue="V-FIELD-2")
        rng = random.Random(20260925)
        items = list(rt.journal())
        rng.shuffle(items)
        restored = SchoolRuntime.from_journal(
            tuple(items), SCHOOL, SEMESTER, VENUES, TEACHERS)
        normalized = rt.normalized()
        for candidate in (restored, normalized):
            rb = candidate.rebuild("C1", HEADCOUNT, 3)
            base = rt.rebuild("C1", HEADCOUNT, 3)
            self.assertEqual(rb["plan_versions"], base["plan_versions"])
            anomalies_a = {(a.kind, a.evidence) for a in detect_anomalies("C1", rb)}
            anomalies_b = {(a.kind, a.evidence) for a in detect_anomalies("C1", base)}
            self.assertEqual(anomalies_a, anomalies_b)

    def test_weather_alt_revision_only_changes_new_interval_judgement(self):
        """v2 把降雨替代改到 ROOM2：第1周用 GYM 合规，第3周用 GYM 偏离 v2。"""
        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        rt.submit_plan(build_plan(alts=(RAIN_ROOM, HEAT_ROOM)),
                       submitted_by="admin", occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        # 第1周降雨：按 v1 在 GYM，引用 v1 预案
        record_session(rt, "C1-PE-1", 1, venue="V-GYM",
                       mode=SessionMode.RAIN_ALT, basis_ref="POL-RAIN-01")
        # 第3周降雨：仍跑 GYM，但 v2 要求 ROOM2
        record_session(rt, "C1-PE-1", 3, venue="V-GYM",
                       mode=SessionMode.RAIN_ALT, basis_ref="POL-RAIN-01")
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        deviations = [a for a in detect_anomalies("C1", rebuilt)
                      if a.kind is AnomalyKind.PLAN_DEVIATION]
        self.assertEqual([a.evidence for a in deviations], [("C1-PE-1#w3",)])
        self.assertEqual(deviations[0].plan_versions, (2,))

    def test_cross_version_makeup_backfills_original_version_week(self):
        """v1 期间的占课缺口，在 v2 期间补上：原场次回填，覆盖归 v1 周。"""
        rt = self._two_version_runtime()
        original = Occasion("C1-PE-2", 1)
        rt.record("takeover",
                  {"slot_id": "C1-PE-2", "week": 1, "subject": "数学", "ref": ""},
                  occurred_at=ts(1, 2))
        makeup = Occasion("C1-PE-1", 3)
        rt.record("makeup_plan", {"occasion": makeup, "makeup_for": original},
                  occurred_at=ts(3, 1, 8))
        record_session(rt, "C1-PE-1", 3, venue="V-FIELD-2",
                       mode=SessionMode.MAKEUP, basis_ref="MK-01",
                       makeup_for=original)
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        self.assertEqual(rebuilt["states"]["C1-PE-2#w1"], "completed")
        self.assertIn("C1-PE-2#w1", rebuilt["made_up"])
        cov = compute_class_coverage("C1", rebuilt, rt.goals_by_version())
        bb_v1 = next(r for r in cov.skill_coverage
                     if r.skill_code == "BB" and r.plan_version == 1)
        self.assertTrue(bb_v1.taught)  # 补课按原场次（v1 第1周）兑现教会目标


    def test_shared_school_ledger_isolates_classes(self):
        """学校级账本含两个班：重建 C1 不得被 C2 事件污染版本绑定。"""
        rt = new_runtime()
        # v1 含 C1、C2 两个班
        slots_c1 = build_plan().slots
        slots_c2 = tuple(
            PlanSlot(f"C2-PE-{d}", "C2", d, ActivityKind.PE_CLASS, "all",
                     "V-FIELD-2", "T-LI", "BB", HEADCOUNT, (RAIN_GYM, HEAT_GYM))
            for d in range(1, 6))
        plan2 = SemesterPlan(SCHOOL, SEMESTER, 0, slots_c1 + slots_c2, GOALS)
        rt.submit_plan(plan2, submitted_by="admin", occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        record_full_week(rt, 1)  # C1
        # C2 第1周的课（slot_id 前缀 C2）
        for d in range(1, 6):
            record_session(rt, f"C2-PE-{d}", 1, venue="V-FIELD-2", teacher="T-LI")
        c1 = rt.rebuild("C1", HEADCOUNT, 1)
        self.assertEqual(set(c1["plan_versions"]),
                         {f"C1-PE-{d}#w1" for d in range(1, 6)})
        c2 = rt.rebuild("C2", HEADCOUNT, 1)
        self.assertEqual(set(c2["plan_versions"]),
                         {f"C2-PE-{d}#w1" for d in range(1, 6)})


# ================================================================ 影响区间与复核

class RevisionRecomputeTest(unittest.TestCase):
    def _runtime_with_resolved_v1_gap(self):
        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        # 第1周周一至四有课，周五完全无事件 -> 阴阳课表
        for d in range(1, 5):
            record_session(rt, f"C1-PE-{d}", 1)
        rebuilt = rt.rebuild("C1", HEADCOUNT, 1)
        anomalies = detect_anomalies("C1", rebuilt)
        self.assertTrue([a for a in anomalies if a.kind is AnomalyKind.YIN_YANG])
        case = rt.board.open_case("C1", anomalies)
        rt.board.resolve(case.case_id, ReviewStatus.MAKEUP_ORDERED, "教研员周",
                         "周五缺口属实，令第3周补课", basis_versions=(1,))
        return rt, case

    def test_recompute_without_history_and_clean_is_noop(self):
        """区间内无异常且无历史案件：安静无操作，不凭空建案。"""
        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        rt.submit_plan(build_plan(venue="V-FIELD-2"), submitted_by="admin",
                       occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        record_full_week(rt, 1)
        record_full_week(rt, 3, venue="V-FIELD-2")
        result = rt.recompute_for_revision("C1", HEADCOUNT, 3, 2)
        self.assertIsNone(result["case"])
        self.assertIsNone(result["correction"])
        self.assertEqual(rt.board.cases_for("C1"), ())
        self.assertEqual(rt.board.open_classes(), ())

    def test_resolved_history_keeps_basis_and_unchanged_by_out_of_scope_revision(self):
        rt, case = self._runtime_with_resolved_v1_gap()
        # v2 只改周一 slot 的场地，第2周周五晚批准
        rt.submit_plan(build_plan(venue="V-FIELD-2"), submitted_by="admin",
                       occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        record_full_week(rt, 3, venue="V-FIELD-2")  # 第3周全部合规
        result = rt.recompute_for_revision("C1", HEADCOUNT, 3, 2)

        self.assertIsNone(result["correction"])          # 区间内无新差异
        kept = rt.board.get(case.case_id)
        self.assertEqual(kept.status, ReviewStatus.MAKEUP_ORDERED)
        self.assertEqual(kept.conclusion, "周五缺口属实，令第3周补课")
        self.assertEqual(kept.reviewer, "教研员周")
        self.assertEqual(kept.basis_versions, (1,))      # 原依据版本保留
        # 已结案仍不可二次裁定
        with self.assertRaises(ValueError):
            rt.board.resolve(case.case_id, ReviewStatus.CLEARED, "教研员吴", "改判")

    def test_new_interval_anomaly_is_appended_as_correction(self):
        rt, case = self._runtime_with_resolved_v1_gap()
        rt.submit_plan(build_plan(venue="V-FIELD-2"), submitted_by="admin",
                       occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        # 第3周周一仍用旧场地 FIELD -> 相对 v2 偏离；其余合规
        record_session(rt, "C1-PE-1", 3, venue="V-FIELD")
        for d in range(2, 6):
            record_session(rt, f"C1-PE-{d}", 3, venue="V-FIELD-2")
        result = rt.recompute_for_revision("C1", HEADCOUNT, 3, 2)

        correction = result["correction"]
        self.assertIsNotNone(correction)
        self.assertEqual(correction.basis_versions, (2,))
        self.assertEqual(
            [(a.kind, a.evidence) for a in correction.added],
            [(AnomalyKind.PLAN_DEVIATION, ("C1-PE-1#w3",))],
        )
        self.assertEqual(correction.withdrawn, ())
        kept = rt.board.get(case.case_id)
        # 原结论原依据不动；当前有效异常 = 旧阴阳 + 新追加偏离
        self.assertEqual(kept.status, ReviewStatus.MAKEUP_ORDERED)
        self.assertEqual(kept.basis_versions, (1,))
        kinds = {a.kind for a in kept.effective_anomalies()}
        self.assertEqual(kinds, {AnomalyKind.YIN_YANG, AnomalyKind.PLAN_DEVIATION})
        # 更正只追加、幂等
        with self.assertRaises(ValueError):
            kept.append_correction(correction)

    def test_recompute_scope_excludes_history_outside_interval(self):
        rt, case = self._runtime_with_resolved_v1_gap()
        # v2 改场地；第3周完全合规
        rt.submit_plan(build_plan(venue="V-FIELD-2"), submitted_by="admin",
                       occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        record_full_week(rt, 3, venue="V-FIELD-2")
        result = rt.recompute_for_revision("C1", HEADCOUNT, 3, 2)
        # 旧的 v1 阴阳课表不在影响区间，绝不允许被登记为“消解”
        self.assertNotIn(
            AnomalyKind.YIN_YANG,
            [a.kind for c in [result["correction"]] if c for a in c.withdrawn],
        )
        # v2 在第 2 周周五 18:00 批准：第 2 周场次已全部发生，区间自第 3 周起
        self.assertTrue(all(
            int(k.rsplit("w", 1)[1]) >= 3 for k in result["scope"]
        ))

    def test_late_evidence_recompute_appends_correction_without_rewrite(self):
        """迟到上传补证了历史场次：按发生位置重算该场，已结案只追加更正。"""
        rt, case = self._runtime_with_resolved_v1_gap()
        # 第3周才收到第1周周五的三方证据（服务恢复补送，发生位置在第1周）
        late = record_session(
            rt, "C1-PE-5", 1,
            at=f"{(MONDAY_W1 + datetime.timedelta(days=4)).isoformat()}T10:00:00",
        )
        # 模拟迟到接收：append 已在最新位置，但 occurred_at 仍是第1周
        result = rt.recompute(
            "C1", HEADCOUNT, 3, frozenset(["C1-PE-5#w1"]),
            reason="迟到上传补证，按实际发生位置重算",
        )
        rebuilt = result["rebuilt"]
        self.assertEqual(rebuilt["plan_versions"]["C1-PE-5#w1"], 1)
        self.assertEqual(rebuilt["states"]["C1-PE-5#w1"], "completed")
        correction = result["correction"]
        self.assertIsNotNone(correction)
        self.assertEqual(
            [(a.kind, a.evidence) for a in correction.withdrawn],
            [(AnomalyKind.YIN_YANG, ("C1-PE-5#w1",))],
        )
        # 原结论仍是“令补课”，只是追加了一条补证更正；不抹除历史
        self.assertEqual(rt.board.get(case.case_id).basis_versions, (1,))


# ================================================================ 三视图同版本

class SharedVersionAcrossViewsTest(unittest.TestCase):
    def test_parent_coverage_researcher_reference_same_version(self):
        vault = IdentityVault("salt-2026-1")
        vault.enroll("stu-1")
        vault.link_parent("parent-1", "stu-1")
        child_token = vault.token_for("stu-1")  # 账本里只有假名 token

        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        rt.submit_plan(build_plan(venue="V-FIELD-2", skill="TJ"),
                       submitted_by="admin", occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        sample_ids = [child_token, "tk1", "tk2", "tk3"]
        record_session(rt, "C1-PE-1", 1, token_ids=sample_ids)            # v1 / BB
        record_session(rt, "C1-PE-1", 3, venue="V-FIELD-2", skill="TJ",
                       token_ids=sample_ids)                              # v2 / TJ

        # 唯一一次重放，三个视图都从它派生
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        sessions = rebuilt["sessions"]
        coverage = compute_class_coverage("C1", rebuilt, rt.goals_by_version())
        anomalies = detect_anomalies("C1", rebuilt)

        view = build_parent_view("parent-1", vault, {}, sessions)
        minutes = {k: (m, v) for k, m, _mode, v in view.recent_minutes}

        # 家长视图（记录带版本，且不回传真实学号）
        self.assertEqual(view.student_label, "本人子女")
        self.assertEqual(minutes["C1-PE-1#w1"], (40, 1))
        self.assertEqual(minutes["C1-PE-1#w3"], (40, 2))
        # 班级覆盖率
        rows = {(r.skill_code, r.plan_version): r for r in coverage.skill_coverage}
        self.assertTrue(rows[("BB", 1)].taught)
        self.assertTrue(rows[("TJ", 2)].taught)
        self.assertEqual(set(coverage.plan_versions), {1, 2})
        # 教研异常口径：两场都与各自版本一致，无偏离
        self.assertFalse([a for a in anomalies if a.kind is AnomalyKind.PLAN_DEVIATION])

    def test_researcher_anomaly_carries_version_used_for_check(self):
        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        rt.submit_plan(build_plan(teacher="T-LI"), submitted_by="admin",
                       occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        record_full_week(rt, 1)
        # 第3周仍由王老师上（v2 要求李老师）→ 教师偏离，证据引用 v2
        record_full_week(rt, 3)
        rebuilt = rt.rebuild("C1", HEADCOUNT, 3)
        deviations = [a for a in detect_anomalies("C1", rebuilt)
                      if a.kind is AnomalyKind.PLAN_DEVIATION]
        self.assertTrue(deviations)
        self.assertEqual({v for a in deviations for v in a.plan_versions}, {2})

    def test_public_summary_carries_same_versions_as_class_coverages(self):
        from pe_domain.coverage import ClassCoverage, SkillCoverage

        def cov(cid, versions):
            return ClassCoverage(
                class_id=cid, total_occasions=20, completed=18, pending_makeup=2,
                in_review=0,
                skill_coverage=(SkillCoverage("BB", True, 1.0, 1.0, (), (),
                                              plan_version=versions[-1]),),
                plan_versions=tuple(versions),
            )
        coverages = {"C1": cov("C1", (1,)), "C2": cov("C2", (1, 2)),
                     "C3": cov("C3", (2,))}
        summary = build_public_summary(SCHOOL, coverages, {})
        self.assertTrue(summary.published)
        self.assertEqual(summary.plan_versions, (1, 2))

    def test_recovery_produces_identical_views(self):
        """服务恢复后：家长/覆盖/异常三视图与恢复前逐字段一致。"""
        vault = IdentityVault("salt-2026-1")
        vault.enroll("stu-1")
        vault.link_parent("p", "stu-1")
        sample_ids = [vault.token_for("stu-1"), "tk1", "tk2", "tk3"]
        rt = new_runtime()
        rt.submit_plan(build_plan(), submitted_by="admin",
                       occurred_at="2026-08-25T09:00:00")
        rt.approve_plan(1, occurred_at="2026-08-28T09:00:00")
        rt.submit_plan(build_plan(venue="V-FIELD-2"), submitted_by="admin",
                       occurred_at="2026-09-10T09:00:00")
        rt.approve_plan(2, occurred_at="2026-09-11T18:00:00")
        record_full_week(rt, 1)
        record_session(rt, "C1-PE-1", 3, venue="V-FIELD", token_ids=sample_ids)
        for d in range(2, 6):
            record_session(rt, f"C1-PE-{d}", 3, venue="V-FIELD-2",
                           token_ids=sample_ids)

        def views(runtime):
            rebuilt = runtime.rebuild("C1", HEADCOUNT, 3)
            coverage = compute_class_coverage("C1", rebuilt, runtime.goals_by_version())
            anomalies = tuple(
                (a.kind.value, a.evidence, a.plan_versions)
                for a in detect_anomalies("C1", rebuilt)
            )
            parent = build_parent_view("p", vault, {}, rebuilt["sessions"])
            return (
                rebuilt["plan_versions"], rebuilt["states"],
                tuple((r.skill_code, r.plan_version, r.taught, r.practiced_ratio)
                      for r in coverage.skill_coverage),
                anomalies, parent.recent_minutes,
            )

        items = list(rt.journal())
        random.Random(7).shuffle(items)
        restored = SchoolRuntime.from_journal(
            tuple(items), SCHOOL, SEMESTER, VENUES, TEACHERS)
        self.assertEqual(views(restored), views(rt))


if __name__ == "__main__":
    unittest.main()
