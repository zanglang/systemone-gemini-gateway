"""Gateway exception types and their HTTP mapping (BRIEF section 4).

Every error subclasses Starlette's ``HTTPException`` so FastAPI's default handler renders
``{"detail": ...}`` (the TypeSafe/FastAPI error shape) and also passes headers through.
``outcome`` feeds the ``s1gw.requests`` metric; ``error_type`` feeds ``error.type`` on the
upstream duration metric. Both are fixed enums, never free text.
"""

from __future__ import annotations

from typing import Any

from starlette.exceptions import HTTPException

Detail = str | dict[str, Any]


class GatewayError(HTTPException):
    """Base class for every error the gateway raises deliberately."""

    status_code_default = 500
    outcome = "upstream_error"
    error_type = "error"

    def __init__(self, detail: Detail, headers: dict[str, str] | None = None) -> None:
        # Starlette types detail as str | None, but any JSON-able value is rendered as-is.
        super().__init__(
            status_code=self.status_code_default,
            detail=detail,  # type: ignore[arg-type]
            headers=headers,
        )


class TranslationError(GatewayError):
    """A translation rule from BRIEF section 3 was violated."""

    status_code_default = 400
    outcome = "client_error"
    error_type = "translation"


class UnsupportedMediaTypeError(GatewayError):
    status_code_default = 415
    outcome = "client_error"

    def __init__(self) -> None:
        super().__init__("multipart requests are not supported; send assets as JSON")


class PayloadTooLargeError(GatewayError):
    status_code_default = 413
    outcome = "client_error"

    def __init__(self) -> None:
        super().__init__("request body too large")


class UnauthorizedError(GatewayError):
    status_code_default = 401
    outcome = "client_error"

    def __init__(self) -> None:
        super().__init__("invalid or missing bearer token", {"WWW-Authenticate": "Bearer"})


class UpstreamSafetyError(GatewayError):
    """Upstream blocked the content (safety, prohibited_content, ...)."""

    status_code_default = 400
    outcome = "blocked"

    def __init__(self, code: str, message: str) -> None:
        super().__init__({"code": code, "message": message})
        self.error_type = code


class UpstreamRejectedError(GatewayError):
    """Upstream answered 400 for a non-safety reason (limits, key validation)."""

    status_code_default = 400
    outcome = "client_error"
    error_type = "rejected"

    def __init__(self, message: str) -> None:
        super().__init__(f"upstream rejected request: {message}")


class UpstreamConfigError(GatewayError):
    """Upstream 401/403/404: the gateway itself is misconfigured."""

    status_code_default = 502
    error_type = "config"

    def __init__(self) -> None:
        super().__init__("upstream configuration error")


class UpstreamRateLimitedError(GatewayError):
    status_code_default = 429
    error_type = "rate_limited"

    def __init__(self, retry_after: str | None = None) -> None:
        headers = {"Retry-After": retry_after} if retry_after else None
        super().__init__("rate limited by upstream", headers)


class UpstreamOverloadedError(GatewayError):
    status_code_default = 529
    error_type = "overloaded"

    def __init__(self) -> None:
        super().__init__("upstream overloaded")


class UpstreamServerError(GatewayError):
    status_code_default = 502
    error_type = "server_error"

    def __init__(self) -> None:
        super().__init__("upstream error")


class UpstreamTimeoutError(GatewayError):
    status_code_default = 504
    outcome = "timeout"
    error_type = "timeout"

    def __init__(self) -> None:
        super().__init__("upstream timeout")


class InvalidUpstreamResponseError(GatewayError):
    status_code_default = 502
    error_type = "invalid_response"

    def __init__(self, detail: str = "invalid upstream response") -> None:
        super().__init__(detail)
