"""学校体育实课运行领域核心。

模块划分：

- models    不可变领域对象与枚举（含可比较的账本位置 LedgerPosition）
- plans     学期方案版本与提交前校验（提交/批准/取代位置、生效区间、journal 重建）
- events    实课事件、三方最小化交叉确认、签到与有效运动时长
- coverage  “教会、勤练、常赛”可解释覆盖规则（按场次绑定版本）
- review    异常识别与教研复核闭环（不做自动处罚；已结案只追加更正）
- ledger    只追加事件账本与班级实况还原（按发生位置版本化重放）
- runtime   运行时协调层：摄入、乱序归一/恢复、影响区间重算
- visibility 公众/家长/教研员分级可见性（同一版本口径）
"""

from .models import (
    ActivityKind,
    InjuryAdaptation,
    LedgerPosition,
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
    "LedgerPosition",
    "PlanSlot",
    "SemesterPlan",
    "SkillGoal",
    "Teacher",
    "Venue",
    "WeatherAlternative",
]
