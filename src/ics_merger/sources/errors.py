from enum import StrEnum


class SourceErrorCode(StrEnum):
    INVALID_URL = "invalid_url"
    BLOCKED_ADDRESS = "blocked_address"
    FETCH_FAILED = "fetch_failed"
    REDIRECT_REJECTED = "redirect_rejected"
    RESPONSE_TOO_LARGE = "response_too_large"
    INVALID_CONTENT_TYPE = "invalid_content_type"
    MALFORMED_CALENDAR = "malformed_calendar"
    AUTH_REQUIRED = "auth_required"
    PROVIDER_FETCH_FAILED = "provider_fetch_failed"
    MALFORMED_PROVIDER_RESPONSE = "malformed_provider_response"
    INVALID_PAGINATION = "invalid_pagination"


class SourceError(Exception):
    """A source failure safe to expose without source URL or calendar content."""

    def __init__(self, code: SourceErrorCode) -> None:
        self.code = code
        super().__init__(code.value)
