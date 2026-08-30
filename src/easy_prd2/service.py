from __future__ import annotations

import asyncio
import json
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .history import HistoryStore
from .models import (
    CopyRequest,
    CreatedObject,
    JobSummary,
    PlanSummary,
    RollbackPlan,
    RunRecord,
)
from .prd2_adapter import PreparedDeployment, Prd2Adapter
from .rossum import Connection, ConnectionStore, RossumError, RossumGateway
from .security import redact


@dataclass
class StoredPlan:
    summary: PlanSummary
    prepared: PreparedDeployment


class EasyPrd2Service:
    RESOURCE_PATHS = {
        "workspace": "workspaces",
        "queue": "queues",
        "schema": "schemas",
        "inbox": "inboxes",
        "hook": "hooks",
        "rule": "rules",
        "label": "labels",
        "email_template": "email_templates",
        "engine": "engines",
        "engine_field": "engine_fields",
    }

    def __init__(
        self,
        history: HistoryStore | None = None,
        gateway: RossumGateway | None = None,
        connections: ConnectionStore | None = None,
        adapter: Prd2Adapter | None = None,
    ):
        self.history = history or HistoryStore()
        self.gateway = gateway or RossumGateway()
        self.connections = connections or ConnectionStore()
        self.adapter = adapter or Prd2Adapter(self.gateway)
        self.plans: dict[str, StoredPlan] = {}
        self.jobs: dict[str, JobSummary] = {}
        self.operation_lock = asyncio.Lock()

    async def connect(self, api_base: str, token: str):
        normalized, user = await self.gateway.connect(api_base, token)
        return self.connections.add(normalized, token.strip(), user)

    async def scoped(self, connection_id: str, organization_id: int) -> Connection:
        connection = await self.gateway.scoped_connection(self.connections.get(connection_id), organization_id)
        return self.connections.remember(connection)

    def _expire_plans(self) -> None:
        now = datetime.now(UTC)
        for plan_id, stored in list(self.plans.items()):
            if stored.summary.expires_at <= now:
                stored.prepared.cleanup()
                self.plans.pop(plan_id, None)

    async def create_plan(self, request: CopyRequest) -> PlanSummary:
        self._expire_plans()
        source = await self.scoped(request.source_connection_id, request.source_organization_id)
        target = await self.scoped(request.target_connection_id, request.target_organization_id)
        source_org = await self.gateway.raw_organization(source, request.source_organization_id)
        target_org = await self.gateway.raw_organization(target, request.target_organization_id)
        if request.target_hook_owner_id is None:
            request.target_hook_owner_id = target.user.get("id")
        prepared = await self.adapter.prepare(request, source, target, source_org, target_org)

        duplicate_names: list[str] = []
        target_workspaces = await self.gateway.workspaces(target, request.target_organization_id)
        existing_workspace_names = {item.name.casefold() for item in target_workspaces}
        if request.target_workspace_name.casefold() in existing_workspace_names:
            duplicate_names.append(f"Workspace ‘{request.target_workspace_name}’ already exists; a separate copy will be created.")
        existing_queue_names: set[str] = set()
        for workspace in target_workspaces:
            for queue in await self.gateway.queues(target, workspace.id):
                existing_queue_names.add(queue.name.casefold())
        for target_name in request.target_queue_names.values():
            if target_name.casefold() in existing_queue_names:
                duplicate_names.append(f"Queue ‘{target_name}’ already exists; a separate copy will be created.")

        plan_id = str(uuid.uuid4())
        counts: dict[str, int] = {}
        for item in prepared.items:
            counts[item.type] = counts.get(item.type, 0) + 1
        summary = PlanSummary(
            id=plan_id,
            target_organization={"id": target_org["id"], "name": target_org["name"], "url": target_org["url"]},
            expires_at=datetime.now(UTC) + timedelta(minutes=30),
            items=prepared.items,
            counts=counts,
            warnings=prepared.warnings,
            manual_follow_ups=prepared.follow_ups,
            duplicate_names=duplicate_names,
        )
        self.plans[plan_id] = StoredPlan(summary, prepared)
        return summary

    def get_plan(self, plan_id: str) -> StoredPlan:
        self._expire_plans()
        try:
            return self.plans[plan_id]
        except KeyError as exc:
            raise RossumError("This deployment plan expired. Generate a fresh preview.") from exc

    def start_deploy(self, plan_id: str) -> JobSummary:
        stored = self.get_plan(plan_id)
        run_id = str(uuid.uuid4())
        prepared = stored.prepared
        workspace_item = next(item for item in prepared.items if item.type == "workspace")
        run = RunRecord(
            id=run_id,
            status="planned",
            source_api_base=prepared.source_connection.api_base,
            target_api_base=prepared.target_connection.api_base,
            source_organization_id=prepared.source_org["id"],
            source_organization_name=prepared.source_org["name"],
            target_organization_id=prepared.target_org["id"],
            target_organization_name=prepared.target_org["name"],
            workspace_id=prepared.request.workspace_id,
            workspace_name=workspace_item.target_name,
            queue_ids=prepared.request.queue_ids,
            warnings=[*stored.summary.warnings, *stored.summary.manual_follow_ups, *stored.summary.duplicate_names],
        )
        self.history.save(run)
        job = JobSummary(id=str(uuid.uuid4()), kind="deploy", status="queued", run_id=run_id)
        self.jobs[job.id] = job
        asyncio.create_task(self._deploy(job.id, plan_id))
        return job

    async def _deploy(self, job_id: str, plan_id: str) -> None:
        job = self.jobs[job_id]
        stored = self.plans.get(plan_id)
        run = self.history.get(job.run_id)
        if not stored or not run:
            job.status = "failed"
            job.error = "Deployment plan is no longer available"
            return
        prepared = stored.prepared
        async with self.operation_lock:
            job.status = "running"
            job.phase = "Preparing deployment"
            run.status = "running"
            self.history.save(run)
            try:
                created, logs, error = await self.adapter.execute(
                    prepared, lambda phase: setattr(job, "phase", phase)
                )
                run.created_objects = await self._refresh_created(created, prepared.target_connection)
                run.error = error
                if error and run.created_objects:
                    run.status = "partial"
                    job.status = "partial"
                elif error:
                    run.status = "failed"
                    job.status = "failed"
                else:
                    run.status = "succeeded"
                    job.status = "succeeded"
                run.rollback_eligible = bool(run.created_objects)
                manifest = prepared.manifest_path.read_text(encoding="utf-8")
                self.history.write_artifact(run.id, "deploy.yaml", manifest)
                safe_logs = redact(logs, (prepared.source_connection.token, prepared.target_connection.token))
                self.history.write_artifact(run.id, "run.log", safe_logs)
            except Exception as exc:
                run.status = "failed"
                job.status = "failed"
                run.error = redact(exc, (prepared.source_connection.token, prepared.target_connection.token))
            finally:
                job.phase = "Complete"
                job.error = run.error
                self.history.save(run)
                prepared.cleanup()
                self.plans.pop(plan_id, None)

    async def _refresh_created(self, objects: list[CreatedObject], connection: Connection) -> list[CreatedObject]:
        refreshed: list[CreatedObject] = []
        for item in objects:
            resource = self.RESOURCE_PATHS.get(item.type)
            if not resource:
                refreshed.append(item)
                continue
            try:
                remote = await self.gateway.get_raw(connection, resource, item.id)
                refreshed.append(
                    CreatedObject(
                        type=item.type,
                        id=item.id,
                        name=remote.get("name", item.name),
                        modified_at=remote.get("modified_at", item.modified_at),
                    )
                )
            except Exception:
                refreshed.append(item)
        return refreshed

    def job(self, job_id: str) -> JobSummary:
        try:
            return self.jobs[job_id]
        except KeyError as exc:
            raise RossumError("Job not found") from exc

    async def rollback_plan(self, run_id: str, connection_id: str) -> RollbackPlan:
        run = self.history.get(run_id)
        if not run:
            raise RossumError("Run not found")
        connection = await self.scoped(connection_id, run.target_organization_id)
        if connection.api_base != run.target_api_base:
            raise RossumError("Reconnect to the original target Rossum environment")
        blocked: list[CreatedObject] = []
        present: list[CreatedObject] = []
        for item in run.created_objects:
            resource = self.RESOURCE_PATHS.get(item.type)
            if not resource:
                continue
            try:
                remote = await self.gateway.get_raw(connection, resource, item.id)
            except Exception as exc:
                if "not found" in str(exc).casefold() or "404" in str(exc):
                    continue
                raise
            current = remote.get("modified_at")
            present.append(item)
            if item.modified_at and current and item.modified_at != current:
                blocked.append(item)
        return RollbackPlan(
            run_id=run.id,
            target_organization_name=run.target_organization_name,
            objects=present,
            blocked_changes=blocked,
        )

    async def start_rollback(self, run_id: str, connection_id: str) -> JobSummary:
        plan = await self.rollback_plan(run_id, connection_id)
        if plan.blocked_changes:
            raise RossumError("Rollback is blocked because copied objects changed after deployment")
        connection = await self.scoped(connection_id, self.history.get(run_id).target_organization_id)  # type: ignore[union-attr]
        job = JobSummary(id=str(uuid.uuid4()), kind="rollback", status="queued", run_id=run_id)
        self.jobs[job.id] = job
        asyncio.create_task(self._rollback(job.id, connection))
        return job

    async def _rollback(self, job_id: str, connection: Connection) -> None:
        job = self.jobs[job_id]
        run = self.history.get(job.run_id)
        if not run:
            job.status = "failed"
            job.error = "Run not found"
            return
        manifest_path = self.history.artifacts_dir / run.id / "deploy.yaml"
        if not manifest_path.exists():
            job.status = "failed"
            job.error = "The deployment manifest is unavailable"
            return
        async with self.operation_lock:
            job.status = "running"
            run.status = "rolling_back"
            self.history.save(run)
            logs, error = await self.adapter.rollback(
                manifest_path.read_text(encoding="utf-8"),
                connection,
                lambda phase: setattr(job, "phase", phase),
            )
            existing_log = self.history.artifacts_dir / run.id / "run.log"
            previous = existing_log.read_text(encoding="utf-8") if existing_log.exists() else ""
            self.history.write_artifact(run.id, "run.log", previous + "\n\nROLLBACK\n" + redact(logs, (connection.token,)))
            run.rollback_error = error
            if error:
                run.status = "rollback_failed"
                job.status = "failed"
                job.error = error
            else:
                run.status = "rolled_back"
                run.rollback_eligible = False
                job.status = "succeeded"
            job.phase = "Complete"
            self.history.save(run)

    def delete_run(self, run_id: str) -> None:
        run = self.history.get(run_id)
        if run and run.status in {"running", "rolling_back"}:
            raise RossumError("Wait for the active operation to finish before deleting it")
        self.history.delete(run_id)

    def shutdown(self) -> None:
        for stored in self.plans.values():
            stored.prepared.cleanup()
        self.plans.clear()
        self.connections.clear()
