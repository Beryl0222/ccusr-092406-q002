"""分级可见性与数据最小化。

三层视图：

1. 公众视图：只发布学校级聚合（完成率、勤练/常赛达成比例、待补赛场次数），
   不出班级排名、不出学生数据；班级数过少时整体抑制（k-匿名），
   防止反推出某个班级或个人。复核未结案的班级不计入分母，
   也不单独披露，避免“未审先判”。
2. 家长视图：凭亲子关系凭据只能读取自己孩子的适配记录与该生（假名化的）
   有效时长，看不到其他学生，也看不到原始签到明细。
3. 教研视图：可看到班级级证据与事件序号，但学生身份仍以 token 呈现，
   需要实名时走单独授权流程（本模块只表达边界）。

身份保管处 IdentityVault 与业务账本物理分离：账本里只有 token，
即使账本泄露也无法还原学生身份；跨学期换盐，token 不可链接。
"""

from __future__ import annotations

from dataclasses import dataclass

from .events import pseudonym
from .review import ReviewStatus

# 公众聚合的最小班级数：少于该值则抑制发布
PUBLIC_MIN_CLASSES = 3
# 公众聚合中任一数值的最小计数（单元格抑制）
PUBLIC_MIN_CELL = 5


class AccessDenied(PermissionError):
    """越权访问（如家长试图查阅非本人子女记录）。"""


class IdentityVault:
    """学生身份 <-> 学期假名映射。与账本分开存储、分开授权。"""

    def __init__(self, semester_salt: str):
        self._salt = semester_salt
        self._student_to_token: dict[str, str] = {}
        self._token_to_student: dict[str, str] = {}
        # 家长凭据 -> 其子女 student_id
        self._parent_links: dict[str, str] = {}

    def enroll(self, student_id: str) -> str:
        token = pseudonym(student_id, self._salt)
        self._student_to_token[student_id] = token
        self._token_to_student[token] = student_id
        return token

    def link_parent(self, parent_credential: str, student_id: str) -> None:
        if student_id not in self._student_to_token:
            raise ValueError("学生未建档")
        self._parent_links[parent_credential] = student_id

    def token_for(self, student_id: str) -> str:
        return self._student_to_token[student_id]

    def resolve_parent(self, parent_credential: str) -> str:
        """校验家长凭据并返回其子女 student_id；失败即拒绝。"""
        student_id = self._parent_links.get(parent_credential)
        if student_id is None:
            raise AccessDenied("未找到亲子关系")
        return student_id

    @property
    def token_to_student(self) -> dict[str, str]:
        return dict(self._token_to_student)


@dataclass(frozen=True)
class PublicSchoolSummary:
    school_id: str
    published: bool
    classes_counted: int
    occasions_completed_ratio: float
    skill_taught_ratio: float
    practice_ratio: float
    match_ratio: float
    pending_makeup: int
    note: str = ""


def build_public_summary(
    school_id: str,
    class_coverages: dict[str, "ClassCoverage"],
    review_cases: dict[str, "ReviewCase"],
) -> PublicSchoolSummary:
    """生成公众聚合。

    class_coverages: {class_id: coverage.compute_class_coverage(...)}
    review_cases: {class_id: ReviewCase|None}，未结案的班级整班排除。
    """
    counted = {
        cid: cov for cid, cov in class_coverages.items()
        if (case := review_cases.get(cid)) is None
        or case.status in (ReviewStatus.CLEARED, ReviewStatus.MAKEUP_ORDERED)
    }
    n = len(counted)
    if n < PUBLIC_MIN_CLASSES:
        return PublicSchoolSummary(
            school_id=school_id, published=False, classes_counted=n,
            occasions_completed_ratio=0.0, skill_taught_ratio=0.0,
            practice_ratio=0.0, match_ratio=0.0, pending_makeup=0,
            note=f"计入班级不足 {PUBLIC_MIN_CLASSES} 个，按最小聚合规则抑制发布",
        )

    total = sum(c.total_occasions for c in counted.values())
    completed = sum(c.completed for c in counted.values())
    pending = sum(c.pending_makeup for c in counted.values())
    skills = [s for c in counted.values() for s in c.skill_coverage]
    taught = sum(1 for s in skills if s.taught)
    practice = sum(s.practiced_ratio for s in skills)
    match = sum(s.matched_ratio for s in skills)

    summary = PublicSchoolSummary(
        school_id=school_id,
        published=True,
        classes_counted=n,
        occasions_completed_ratio=round(completed / total, 3) if total else 0.0,
        skill_taught_ratio=round(taught / len(skills), 3) if skills else 0.0,
        practice_ratio=round(practice / len(skills), 3) if skills else 0.0,
        match_ratio=round(match / len(skills), 3) if skills else 0.0,
        # 单元格抑制：待补赛场次过少时不报具体数，防止反推单一班级
        pending_makeup=pending if pending >= PUBLIC_MIN_CELL else 0,
    )
    return summary


@dataclass(frozen=True)
class ParentView:
    """家长可见的本人子女记录：不含任何他人信息、不含签到明细。"""

    student_label: str  # 不回传真实学号，仅回传“本人子女”语义标签
    adaptations: tuple
    recent_minutes: tuple  # (occasion_key, minutes, mode)


def build_parent_view(
    parent_credential: str,
    vault: IdentityVault,
    adaptations_by_student: dict,
    sessions,
) -> ParentView:
    """家长视图。凭据无效直接 AccessDenied。"""
    student_id = vault.resolve_parent(parent_credential)
    token = vault.token_for(student_id)

    adaptations = tuple(
        a for a in adaptations_by_student.get(student_id, ())
    )
    # 只返回该生经确认场次的时长；未确认场次不展示结论，避免先入为主
    minutes = tuple(
        (s.occasion.key(), s.per_student.get(token, 0), s.mode.value)
        for s in sessions
        if s.confirmed and token in s.per_student
    )
    return ParentView(
        student_label="本人子女",
        adaptations=adaptations,
        recent_minutes=minutes,
    )
