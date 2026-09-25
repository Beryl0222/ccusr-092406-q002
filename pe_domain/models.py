"""不可变领域对象：场地、教师、技能目标、方案版本、替代与适配依据。

设计原则：

- 方案对象带版本号与冻结状态，审批通过的方案不可修改，只能提交新版本；
- 降雨/高温替代方案与伤病适配必须引用依据（预案条款 / 医嘱 / 家长申请编号），
  不允许授课时临时口头决定后无追溯；
- 场地安全容量是硬约束，方案校验与会话重建都会检查。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Optional


class ActivityKind(str, Enum):
    """课程活动四类，对应需求中的“体育课、大课间、课后服务和班级赛事”。"""

    PE_CLASS = "pe_class"        # 体育课
    DAILY_BREAK = "daily_break"  # 大课间
    AFTER_SCHOOL = "after_school"  # 课后服务（体育类）
    CLASS_MATCH = "class_match"  # 班级赛事


# 各活动类型的法定/校本单次时长基准（分钟）
STANDARD_MINUTES = {
    ActivityKind.PE_CLASS: 40,
    ActivityKind.DAILY_BREAK: 30,
    ActivityKind.AFTER_SCHOOL: 60,
    ActivityKind.CLASS_MATCH: 40,
}


class VenueType(str, Enum):
    OUTDOOR = "outdoor"  # 室外（受降雨/高温影响）
    INDOOR = "indoor"    # 室内（体育馆等可作为替代场地）


@dataclass(frozen=True)
class Venue:
    venue_id: str
    name: str
    kind: VenueType
    safe_capacity: int  # 同时在场的安全容量（人）

    def __post_init__(self):
        if self.safe_capacity <= 0:
            raise ValueError("场地安全容量必须为正数")


@dataclass(frozen=True)
class Teacher:
    teacher_id: str
    name: str
    qualifications: frozenset[str]  # 持有的教学资质，如 {"田径", "篮球", "急救"}

    def qualified_for(self, skill_code: str) -> bool:
        return skill_code in self.qualifications


@dataclass(frozen=True)
class SkillGoal:
    """技能目标：skill_code 与教师资质码共用同一套编码。"""

    skill_code: str
    name: str
    teach_weeks: tuple[int, ...]   # “教会”计划周次
    practice_weeks: tuple[int, ...]  # “勤练”计划周次
    match_weeks: tuple[int, ...]   # “常赛”计划周次

    def __post_init__(self):
        for weeks in (self.teach_weeks, self.practice_weeks, self.match_weeks):
            if any(w <= 0 for w in weeks):
                raise ValueError("教学周次必须为正整数")


@dataclass(frozen=True)
class WeatherAlternative:
    """降雨/高温替代安排。condition 取 "rain" 或 "heat"。"""

    condition: str
    venue_id: str
    content: str
    policy_ref: str  # 依据：学校预案条款编号

    def __post_init__(self):
        if self.condition not in ("rain", "heat"):
            raise ValueError("天气替代仅支持 rain / heat")
        if not self.policy_ref:
            raise ValueError("天气替代必须引用预案依据")


@dataclass(frozen=True)
class InjuryAdaptation:
    """单个学生的伤病适配。

    由医嘱或书面家长申请驱动；adjusted_minutes 为该生在对应活动中的
    建议参与时长（分钟），0 表示见习。适配按学生隔离存储（见 visibility）。
    """

    student_id: str
    skill_code: str
    adjusted_minutes: int
    basis_ref: str  # 医嘱编号 / 家长申请编号
    note: str = ""

    def __post_init__(self):
        if self.adjusted_minutes < 0:
            raise ValueError("适配时长不能为负")
        if not self.basis_ref:
            raise ValueError("伤病适配必须引用依据")


@dataclass(frozen=True)
class PlanSlot:
    """方案中的一个固定时段（某班、某星期几、某类型）。"""

    slot_id: str
    class_id: str
    weekday: int            # 1-7
    kind: ActivityKind
    week_parity: str        # "all" / "odd" / "even"
    venue_id: str
    teacher_id: str
    skill_code: str
    headcount: int          # 班级人数（容量校验用，按适配见习生另算）
    weather_alternatives: tuple[WeatherAlternative, ...] = ()

    def active_in_week(self, week: int) -> bool:
        if self.week_parity == "all":
            return True
        if self.week_parity == "odd":
            return week % 2 == 1
        return week % 2 == 0

    def __post_init__(self):
        if not 1 <= self.weekday <= 7:
            raise ValueError("星期取值应为 1-7")
        if self.week_parity not in ("all", "odd", "even"):
            raise ValueError("周次奇偶取值非法")
        if self.headcount <= 0:
            raise ValueError("班级人数必须为正数")


@dataclass(frozen=True, order=True)
class LedgerPosition:
    """可比较的账本位置。

    业务事件必须回答两个不同的问题：

    - **什么时候发生**（occurred_at）：迟到上传的离线事件，发生位置仍是实际
      授课时刻，而不是服务器接收时刻（received_at 只用于离线受理时限判断）；
    - **处在账本什么位置**（seq）：重放乱序到达、服务恢复后按账本重放时的
      稳定全序依据。seq 按 occurred_at 分配，同刻由 phase 决定先后
      （方案提交 → 方案批准 → 业务事件），保证“同一位置”边界确定。

    seq 必须严格递增、无空洞地覆盖重放集合；运行时协调层在回放/恢复时
    重编号，使乱序重放与服务恢复得到完全相同的版本选择。
    """

    seq: int
    occurred_at: str
    phase: int = 2  # 0=plan_submitted 1=plan_approved 2=业务事件

    @staticmethod
    def sort_key(position: "LedgerPosition") -> tuple:
        return (position.occurred_at, position.phase, position.seq)


@dataclass(frozen=True)
class SemesterPlan:
    """学期方案（带版本）。

    版本由 plans 模块在提交时统一编号；frozen 版本进入审批后只读，
    任何调整必须以新版本承载，旧版本永久保留以便对照“阴阳课表”。

    三个账本位置是版本生效时点的唯一依据：

    - submitted_at：提交位置（已入库但尚未批准，不能核对任何事件）；
    - approved_at：生效起点（闭区间）；
    - superseded_at：被新版本取代的位置（开区间）。版本 v 对满足
      ``approved_at <= 事件位置 < superseded_at`` 的事件生效。
      ``None`` 表示仍在生效。较晚批准的版本只影响取代点之后的事件，
      绝不回写早期核对结论。
    """

    school_id: str
    semester: str  # 如 "2026-2027-1"
    version: int
    slots: tuple[PlanSlot, ...]
    skill_goals: tuple[SkillGoal, ...]
    status: str = "draft"  # draft / submitted / approved / superseded
    submitted_by: Optional[str] = None
    submitted_at: Optional[LedgerPosition] = None
    approved_at: Optional[LedgerPosition] = None
    superseded_at: Optional[LedgerPosition] = None

    def with_status(self, **changes) -> "SemesterPlan":
        return replace(self, **changes)

    def slots_for(self, class_id: str) -> tuple[PlanSlot, ...]:
        return tuple(s for s in self.slots if s.class_id == class_id)

    def effective_at(self, position: LedgerPosition) -> bool:
        """该版本在给定账本位置是否为“已批准且尚未被取代”。

        边界：批准同刻的业务事件（phase=2）在批准（phase=1）之后，引用本版本；
        取代同刻的业务事件在新批准之后，应引用新版本——故取代点开区间。
        """
        if self.approved_at is None:
            return False
        if LedgerPosition.sort_key(position) < LedgerPosition.sort_key(self.approved_at):
            return False
        if self.superseded_at is not None and \
                LedgerPosition.sort_key(position) >= LedgerPosition.sort_key(self.superseded_at):
            return False
        return True
