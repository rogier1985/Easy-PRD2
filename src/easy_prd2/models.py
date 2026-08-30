from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator


class ConnectionInput(BaseModel):
    api_base: str
    token: str = Field(min_length=1)


class ConnectionSummary(BaseModel):
    id: str
    api_base: str
    username: str = ""
    user_id: int | None = None


class OrganizationSummary(BaseModel):
    id: int
    name: str
    url: str


class WorkspaceSummary(BaseModel):
    id: int
    name: str
    url: str


class QueueSummary(BaseModel):
    id: int
    name: str
    url: str
    workspace_id: int


class AdminSummary(BaseModel):
    id: int
    username: str


class CopyRequest(BaseModel):
    source_connection_id: str
    target_connection_id: str
    source_organization_id: int
    target_organization_id: int
    workspace_id: int
    queue_ids: list[int] = Field(min_length=1)
    target_workspace_name: str = Field(min_length=1, max_length=255)
    target_queue_names: dict[int, str]
    target_hook_owner_id: int | None = None
    hook_template_overrides: dict[int, str] = {}

    @field_validator("queue_ids")
    @classmethod
    def queue_ids_are_unique(cls, value: list[int]) -> list[int]:
        if len(value) != len(set(value)):
            raise ValueError("Queue IDs must be unique")
        return value


class PlanItem(BaseModel):
    type: str
    source_id: int
    source_name: str
    target_name: str
    dependencies: list[str] = []


class PlanSummary(BaseModel):
    id: str
    target_organization: OrganizationSummary
    expires_at: datetime
    items: list[PlanItem]
    counts: dict[str, int]
    warnings: list[str] = []
    manual_follow_ups: list[str] = []
    duplicate_names: list[str] = []


class CreatedObject(BaseModel):
    type: str
    id: int
    name: str = ""
    modified_at: str | None = None


RunStatus = Literal[
    "planned", "running", "succeeded", "partial", "failed", "rolling_back", "rolled_back", "rollback_failed"
]


class RunRecord(BaseModel):
    id: str
    status: RunStatus
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source_api_base: str
    target_api_base: str
    source_organization_id: int
    source_organization_name: str
    target_organization_id: int
    target_organization_name: str
    workspace_id: int
    workspace_name: str
    queue_ids: list[int]
    warnings: list[str] = []
    created_objects: list[CreatedObject] = []
    error: str | None = None
    rollback_error: str | None = None
    rollback_eligible: bool = False


class JobSummary(BaseModel):
    id: str
    kind: Literal["deploy", "rollback"]
    status: Literal["queued", "running", "succeeded", "partial", "failed"]
    phase: str = "Queued"
    run_id: str
    error: str | None = None


class RollbackRequest(BaseModel):
    target_connection_id: str


class RollbackPlan(BaseModel):
    run_id: str
    target_organization_name: str
    objects: list[CreatedObject]
    blocked_changes: list[CreatedObject] = []


class ApiError(BaseModel):
    detail: str
    code: str | None = None
    context: dict[str, Any] | None = None

