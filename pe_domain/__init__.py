"""学校体育实课运行领域核心。

模块划分：

- models    不可变领域对象与枚举
- plans     学期方案版本与提交前校验
- events    实课事件、三方最小化交叉确认、签到与有效运动时长
- coverage  “教会、勤练、常赛”可解释覆盖规则
- review    异常识别与教研复核闭环（不做自动处罚）
- ledger    只追加事件账本与班级实况还原
- visibility 公众/家长/教研员分级可见性
"""

from .models import (
    ActivityKind,
    InjuryAdaptation,
    PlanSlot,
    SemesterPlan,
    SkillGoal,
    Teacher,
    Venue,
    WeatherAlternative,
)

__all__ = [
    "ActivityKind",
    "InjuryAdaptation",
    "PlanSlot",
    "SemesterPlan",
    "SkillGoal",
    "Teacher",
    "Venue",
    "WeatherAlternative",
]
