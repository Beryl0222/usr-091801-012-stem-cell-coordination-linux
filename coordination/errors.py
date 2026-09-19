"""领域异常：调用方据此区分业务冲突与系统错误。"""

from __future__ import annotations


class CoordinationError(Exception):
    """捐献协同领域服务异常基类。"""


class IdentityError(CoordinationError):
    """身份域或身份保险库操作失败。"""


class UnsealDenied(IdentityError):
    """紧急解封被拒绝（授权不足、令牌无效或已自动到期）。"""


class ConsentError(CoordinationError):
    """同意范围或冷静期操作失败。"""


class ContactBlocked(ConsentError):
    """联络被最新同意范围或冷静期拦截。"""


class CaseStateError(CoordinationError):
    """个案流程状态不允许当前操作。"""


class DonorConflict(CaseStateError):
    """志愿者已承诺其他个案（异常信息不透露对方个案，避免跨案泄露）。"""


class CollectionConflict(CaseStateError):
    """采集排程冲突，用于防止重复采集。"""


class MilestoneError(CoordinationError):
    """里程碑规划或上报不合法。"""
