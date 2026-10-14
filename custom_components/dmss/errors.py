"""Translate client failures without exposing response bodies or credentials."""
from .client import APIError, VerificationRequired
from .const import AUTH_CODES


def is_auth_error(error: Exception) -> bool:
    return isinstance(error, VerificationRequired) or (
        isinstance(error, APIError)
        and (error.status in {401, 403} or error.session_expired or error.code in AUTH_CODES)
    )
