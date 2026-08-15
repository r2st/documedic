"""Auth request/response schemas."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field

# Ceiling on any field carrying a refresh token. ``generate_refresh_token`` emits 64 hex
# characters, so this is generous by a factor of four and still bounds what has to be hashed.
#
# The three fields it applies to were the only unbounded strings left in a request body, and two
# of the routes reading them (``/auth/refresh``, ``/auth/logout``) take no credential — so the
# only ceiling on them was the 24 MB whole-body limit. Every one of those bytes was read,
# validated, SHA-256'd and used as a lookup key before the token could be found not to exist.
# The work is small per request and the point is that it was unbounded per request, on the two
# routes where nothing identifies the caller well enough to charge it to an account.
#
# Rejecting at the schema is what makes it free: Pydantic refuses on length before the value
# reaches ``hash_token``. A real client cannot hit it — a token this API issued is 64 characters
# and one it did not issue is refused whatever its length.
MAX_REFRESH_TOKEN_CHARS = 256


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=8, max_length=128)
    display_name: str | None = Field(default=None, max_length=255)


class LoginRequest(BaseModel):
    email: EmailStr
    password: str = Field(..., min_length=1, max_length=128)


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1, max_length=MAX_REFRESH_TOKEN_CHARS)


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # seconds until access token expiry


class AccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: EmailStr
    display_name: str | None
    created_at: datetime


class LogoutAllRequest(BaseModel):
    # Optional: pass the caller's own current refresh token to keep that one session alive
    # while revoking every other one ("log out all other devices").
    keep_current_refresh_token: str | None = Field(default=None, max_length=MAX_REFRESH_TOKEN_CHARS)


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(..., min_length=1, max_length=128)
    new_password: str = Field(..., min_length=8, max_length=128)
    # The caller's own refresh token, so the device doing the change is not signed out along
    # with every other one. Omit it to be signed out everywhere including here.
    keep_current_refresh_token: str | None = Field(default=None, max_length=MAX_REFRESH_TOKEN_CHARS)


class PasswordResetRequest(BaseModel):
    email: EmailStr


class PasswordResetResponse(BaseModel):
    """Fixed-shape acknowledgement. ``message`` never varies with whether the address exists."""

    message: str
    # Present only when PASSWORD_RESET_DELIVERY=response, which production refuses. Null in
    # every other case, including for an address that has no account — a client cannot read
    # this field to enumerate accounts.
    reset_token: str | None = None


class PasswordResetConfirm(BaseModel):
    token: str = Field(..., min_length=1, max_length=256)
    new_password: str = Field(..., min_length=8, max_length=128)


class SessionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    ip_address: str | None
    user_agent: str | None
    created_at: datetime
    last_used_at: datetime
    expires_at: datetime
