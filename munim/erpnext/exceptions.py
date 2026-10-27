from __future__ import annotations


class ERPNextError(RuntimeError):
    pass


class ERPNextAuthError(ERPNextError):
    pass


class ERPNextPermissionError(ERPNextError):
    pass


class ERPNextValidationError(ERPNextError):
    pass


class ERPNextTransientError(ERPNextError):
    pass


class ERPNextMutationUncertain(ERPNextError):
    pass


class ERPNextMutationConflict(ERPNextError):
    pass


class ERPNextAlreadyCompleted(ERPNextError):
    pass


class ERPNextMutationNotFound(ERPNextError):
    pass


class ERPNextPDFDownloadError(ERPNextError):
    def __init__(
        self,
        message: str,
        *,
        upstream_status: int,
        upstream_content_type: str,
    ) -> None:
        super().__init__(message)
        self.upstream_status = upstream_status
        self.upstream_content_type = upstream_content_type
