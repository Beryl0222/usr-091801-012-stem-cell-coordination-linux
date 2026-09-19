"""领域错误与错误码。"""


class DomainError(Exception):
    """所有可预期的业务拒绝都使用该类型，携带稳定错误码。"""

    status = 400

    def __init__(self, code, message, details=None, status=None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        if status is not None:
            self.status = status


class NotFound(DomainError):
    status = 404

    def __init__(self, what, key):
        super().__init__("not_found", f"{what}不存在: {key}", {"key": key})


class Conflict(DomainError):
    status = 409

    def __init__(self, code, message, details=None):
        super().__init__(code, message, details, status=409)


class PermissionDenied(DomainError):
    status = 403

    def __init__(self, message="无权执行该操作", details=None):
        super().__init__("forbidden", message, details, status=403)


class Unauthorized(DomainError):
    status = 401

    def __init__(self, message="缺少身份凭证"):
        super().__init__("unauthorized", message, status=401)
