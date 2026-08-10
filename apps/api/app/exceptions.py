"""Domain exceptions, mapped to HTTP responses by handlers in main.py."""

from __future__ import annotations


class AetherError(Exception):
    """Base class for all domain errors."""

    status_code: int = 400
    code: str = "error"

    def __init__(self, message: str | None = None) -> None:
        self.message = message or self.__class__.__doc__ or "Error"
        super().__init__(self.message)


class NotFoundError(AetherError):
    status_code = 404
    code = "not_found"


class PatientNotFoundError(NotFoundError):
    """Patient not found."""

    code = "patient_not_found"


class DocumentNotFoundError(NotFoundError):
    """Document not found."""

    code = "document_not_found"


class PathwayNotFoundError(NotFoundError):
    """No curated clinical pathway exists for this condition."""

    code = "pathway_not_found"


class AuthError(AetherError):
    status_code = 401
    code = "unauthorized"


class InvalidCredentialsError(AuthError):
    """Invalid email or password."""

    code = "invalid_credentials"


class TokenError(AuthError):
    """Invalid or expired token."""

    code = "invalid_token"


class ForbiddenError(AetherError):
    status_code = 403
    code = "forbidden"


class TooManyAttemptsError(AetherError):
    """Too many failed attempts. Try again later."""

    status_code = 429
    code = "too_many_attempts"


class ConflictError(AetherError):
    status_code = 409
    code = "conflict"


class EmailAlreadyExistsError(ConflictError):
    """An account with this email already exists."""

    code = "email_exists"


class ConsentRequiredError(AetherError):
    """Patient consent is required before clinical data can be stored (DPDP Act)."""

    status_code = 422
    code = "consent_required"


class ValidationError(AetherError):
    status_code = 422
    code = "validation_error"


class UnsupportedFileTypeError(ValidationError):
    """Unsupported file type."""

    code = "unsupported_file_type"


class FileTooLargeError(ValidationError):
    """File exceeds the maximum allowed size."""

    code = "file_too_large"


class HardBlockError(AetherError):
    """A safety hard block prevents this action and cannot be overridden."""

    status_code = 409
    code = "hard_block"
