"""Pydantic v2 API schemas for authentication, organizations, and standard envelopes."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


class HealthResponse(BaseModel):
    status: str
    env: str
    database: str
    object_store: str


class UserSummary(BaseModel):
    id: str
    email: str
    full_name: str
    is_verified: bool
    is_platform_operator: bool
    created_at: datetime


class TenantSummary(BaseModel):
    id: str
    slug: str
    name: str
    data_region: str
    status: str
    retention_days: int
    role: str | None = None


class RegisterRequest(BaseModel):
    email: str
    password: str = Field(min_length=8)
    full_name: str = Field(min_length=1, max_length=120)
    org_name: str | None = Field(default=None, max_length=150)
    org_slug: str | None = Field(default=None, max_length=64)
    data_region: str = "us-east"


class AuthResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    session_id: str
    user: UserSummary
    active_org: TenantSummary | None = None
    verification_token: str | None = None


class VerifyEmailRequest(BaseModel):
    token: str


class LoginRequest(BaseModel):
    email: str
    password: str
    org_id: str | None = None


class PasswordResetRequest(BaseModel):
    email: str


class PasswordResetConfirmRequest(BaseModel):
    token: str
    new_password: str = Field(min_length=8)


class SessionSummary(BaseModel):
    id: str
    active_tenant_id: str | None
    user_agent: str
    revoked_at: datetime | None
    expires_at: datetime
    created_at: datetime


class MeResponse(BaseModel):
    user: UserSummary
    active_org: TenantSummary | None
    permissions: list[str]
    orgs: list[TenantSummary]


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=2, max_length=150)
    slug: str | None = Field(default=None, max_length=64)
    data_region: str = "us-east"


class SwitchOrgRequest(BaseModel):
    tenant_id: str


class UpdateOrgRequest(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=150)
    retention_days: int | None = Field(default=None, ge=7, le=3650)


class MemberSummary(BaseModel):
    membership_id: str
    user_id: str
    email: str
    full_name: str
    role: str
    status: str
    created_at: datetime


class CreateInvitationRequest(BaseModel):
    email: str
    role: str = "developer"
    expires_hours: int = Field(default=72, ge=1, le=720)


class InvitationResponse(BaseModel):
    id: str
    email: str
    role: str
    status: str
    expires_at: datetime
    invite_token: str | None = None


class AcceptInvitationRequest(BaseModel):
    token: str


class UpdateMemberRoleRequest(BaseModel):
    role: str


class TransferOwnershipRequest(BaseModel):
    new_owner_user_id: str
