from __future__ import annotations

import contextlib
import io
import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

import yaml

from .models import CopyRequest, CreatedObject, PlanItem
from .rossum import Connection, RossumGateway
from .security import redact


class Prd2Unavailable(RuntimeError):
    pass


class UnresolvedHookTemplates(RuntimeError):
    def __init__(self, requirements: list[dict[str, Any]]):
        super().__init__("Some private hooks need a target hook template")
        self.requirements = requirements


@dataclass
class PreparedDeployment:
    request: CopyRequest
    workdir: Path
    manifest_path: Path
    yaml_object: Any
    orchestrator: Any
    source_connection: Connection
    target_connection: Connection
    source_org: dict[str, Any]
    target_org: dict[str, Any]
    items: list[PlanItem]
    warnings: list[str]
    follow_ups: list[str]
    logs: list[str]

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _imports() -> dict[str, Any]:
    try:
        import deployment_manager
        from anyio import Path as AnyPath
        from deployment_manager.commands.deploy.subcommands.run.deploy_orchestrator.deploy_orchestrator import (
            DeployOrchestrator,
        )
        from deployment_manager.commands.deploy.subcommands.run.helpers import DeployYaml
        from deployment_manager.commands.download.directory import DownloadOrganizationDirectory
        from deployment_manager.common.rossum_client import CustomAsyncAPIClient
        from rossum_api.dtos import Token
        from rossum_api.models.organization import Organization
    except Exception as exc:  # pragma: no cover - exercised by packaged smoke test
        raise Prd2Unavailable(
            "PRD2 v2.18.3 is not installed correctly. Reinstall Easy PRD2 with pipx."
        ) from exc
    return locals()


def assert_prd2_contract() -> None:
    imports = _imports()
    orchestrator = imports["DeployOrchestrator"]
    required = {
        "initialize_deploy_objects",
        "initialize_target_objects",
        "compare_object_versions",
        "run_deploy",
        "save_deploy_state",
        "save_auto_mappings",
    }
    missing = sorted(name for name in required if not hasattr(orchestrator, name))
    if missing:
        raise Prd2Unavailable("The installed PRD2 version is incompatible: " + ", ".join(missing))


class Prd2Adapter:
    def __init__(self, gateway: RossumGateway):
        self.gateway = gateway

    @staticmethod
    def _id_from_url(value: str | None) -> int | None:
        if not value:
            return None
        try:
            return int(str(value).rstrip("/").rsplit("/", 1)[-1])
        except ValueError:
            return None

    @staticmethod
    def _find_json(root: Path, filename: str, object_id: int) -> tuple[Path, dict[str, Any]]:
        for path in root.rglob(filename):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("id") == object_id:
                return path, value
        raise RuntimeError(f"PRD2 did not download {filename} for object {object_id}")

    @staticmethod
    def _all_json(root: Path, filename: str) -> list[tuple[Path, dict[str, Any]]]:
        result = []
        for path in root.rglob(filename):
            try:
                result.append((path, json.loads(path.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue
        return result

    @staticmethod
    def _target(name: str | None = None) -> list[dict[str, Any]]:
        target: dict[str, Any] = {"id": None}
        if name is not None:
            target["attribute_override"] = {"name": name}
        return [target]

    async def _download_source(
        self,
        workdir: Path,
        source_connection: Connection,
        source_org_id: int,
    ) -> tuple[Any, Path]:
        imports = _imports()
        client = imports["CustomAsyncAPIClient"](
            base_url=source_connection.api_base,
            credentials=imports["Token"](token=source_connection.token),
        )
        source_path = workdir / "source" / "selected"
        source_path.mkdir(parents=True, exist_ok=True)
        class WebDownloadDirectory(imports["DownloadOrganizationDirectory"]):
            async def initialize(self) -> None:
                # PRD2's CLI downloader consults Git to protect a user's local
                # checkout. Easy PRD2 uses a fresh disposable directory, so
                # there can be no local edits to protect and no reason to touch
                # the process working directory's Git configuration.
                self.changed_files = []

        directory = WebDownloadDirectory(
            name="source",
            org_id=source_org_id,
            api_base=source_connection.api_base,
            subdirectories={"selected": {"regex": "", "include": True}},
            client=client,
            project_path=imports["AnyPath"](workdir),
            download_all=True,
            skip_objects_without_subdir=False,
            ignore_changed_file_warnings=True,
        )
        await directory.download_organization()
        return client, source_path

    async def _resolve_hook_templates(
        self,
        hooks: list[dict[str, Any]],
        source: Connection,
        target: Connection,
        overrides: dict[int, str],
    ) -> dict[int, str]:
        target_templates = await self.gateway.list_raw(target, "hook_templates")
        by_name = {item.get("name"): item.get("url") for item in target_templates}
        resolved: dict[int, str] = dict(overrides)
        unresolved: list[dict[str, Any]] = []
        for hook in hooks:
            if hook.get("type") == "function" or not hook.get("config", {}).get("private"):
                continue
            hook_id = int(hook["id"])
            if hook_id in resolved:
                continue
            template_id = self._id_from_url(hook.get("hook_template"))
            source_name = None
            if template_id:
                source_template = await self.gateway.get_raw(source, "hook_templates", template_id)
                source_name = source_template.get("name")
            if source_name and by_name.get(source_name):
                resolved[hook_id] = by_name[source_name]
                continue
            unresolved.append(
                {
                    "hook_id": hook_id,
                    "hook_name": hook.get("name", str(hook_id)),
                    "source_template_name": source_name,
                    "options": [
                        {"name": item.get("name", item.get("url", "Template")), "url": item.get("url")}
                        for item in target_templates
                        if item.get("url")
                    ],
                }
            )
        if unresolved:
            raise UnresolvedHookTemplates(unresolved)
        return resolved

    def _build_manifest(
        self,
        request: CopyRequest,
        source_path: Path,
        source_url: str,
        target_url: str,
        hook_templates: dict[int, str],
    ) -> tuple[dict[str, Any], list[PlanItem], list[str], list[str]]:
        workspace_path, workspace = self._find_json(source_path, "workspace.json", request.workspace_id)
        workspace_entry = {
            "id": workspace["id"],
            "name": workspace["name"],
            "targets": self._target(request.target_workspace_name),
        }
        items = [
            PlanItem(
                type="workspace",
                source_id=workspace["id"],
                source_name=workspace["name"],
                target_name=request.target_workspace_name,
            )
        ]
        warnings: list[str] = []
        follow_ups: list[str] = []
        queues: list[dict[str, Any]] = []
        queue_data: list[dict[str, Any]] = []
        attached_hook_ids: set[int] = set()
        engine_ids: set[int] = set()

        for queue_id in request.queue_ids:
            queue_path, queue = self._find_json(source_path, "queue.json", queue_id)
            queue_data.append(queue)
            target_name = request.target_queue_names.get(queue_id, queue["name"]).strip()
            if not target_name:
                raise RuntimeError(f"Target name is missing for queue {queue['name']}")
            base_path = queue_path.parent.parent.parent
            schema_path = queue_path.parent / "schema.json"
            if not schema_path.exists():
                raise RuntimeError(f"Queue {queue['name']} has no downloaded schema and cannot be copied")
            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            entry: dict[str, Any] = {
                "id": queue["id"],
                "name": queue["name"],
                "base_path": str(base_path),
                "targets": self._target(target_name),
                "ignore_deploy_warnings": True,
                "schema": {"id": schema["id"], "targets": self._target()},
            }
            inbox_path = queue_path.parent / "inbox.json"
            if inbox_path.exists():
                inbox = json.loads(inbox_path.read_text(encoding="utf-8"))
                entry["inbox"] = {"id": inbox["id"], "targets": self._target()}
            queues.append(entry)
            dependencies = ["schema"] + (["inbox"] if inbox_path.exists() else [])
            items.append(PlanItem(type="queue", source_id=queue_id, source_name=queue["name"], target_name=target_name, dependencies=dependencies))
            attached_hook_ids.update(filter(None, (self._id_from_url(url) for url in queue.get("hooks", []))))
            for attribute in ("engine", "generic_engine", "dedicated_engine"):
                engine_id = self._id_from_url(queue.get(attribute))
                if engine_id:
                    engine_ids.add(engine_id)
            if queue.get("workflows"):
                follow_ups.append(f"Assign workflows manually to target queue ‘{target_name}’.")
            if any(queue.get(key) for key in ("automation_enabled", "automation_level", "default_score_threshold")):
                follow_ups.append(f"Review automation settings on target queue ‘{target_name}’.")

        hooks_path = source_path / "hooks"
        all_hooks = {
            item["id"]: (path, item)
            for path, item in (self._all_json(hooks_path, "*.json") if hooks_path.exists() else [])
            if "id" in item
        }
        selected_hooks: dict[int, tuple[Path, dict[str, Any]]] = {}
        pending = list(attached_hook_ids)
        while pending:
            hook_id = pending.pop()
            found = all_hooks.get(hook_id)
            if not found or "events" not in found[1] or "config" not in found[1]:
                warnings.append(f"Attached hook {hook_id} was not available in the source pull.")
                continue
            if hook_id in selected_hooks:
                continue
            selected_hooks[hook_id] = found
            pending.extend(filter(None, (self._id_from_url(url) for url in found[1].get("run_after", []))))

        hooks = [
            {"id": data["id"], "name": data["name"], "targets": self._target()}
            for _, data in selected_hooks.values()
        ]
        for _, hook in selected_hooks.values():
            items.append(PlanItem(type="hook", source_id=hook["id"], source_name=hook["name"], target_name=hook["name"], dependencies=[]))
            follow_ups.append(f"Configure target secrets for hook ‘{hook['name']}’ if it uses secrets.")

        rules: list[dict[str, Any]] = []
        selected_queue_ids = set(request.queue_ids)
        for _, rule in self._all_json(source_path / "rules", "*.json") if (source_path / "rules").exists() else []:
            if rule.get("schema"):
                warnings.append(f"Rule ‘{rule.get('name', rule.get('id'))}’ uses deprecated schema assignment and will not be copied.")
                continue
            referenced = {self._id_from_url(url) for url in rule.get("queues", [])}
            if referenced.intersection(selected_queue_ids):
                rules.append({"id": rule["id"], "name": rule["name"], "targets": self._target()})
                items.append(PlanItem(type="rule", source_id=rule["id"], source_name=rule["name"], target_name=rule["name"], dependencies=["queues"]))

        engines: list[dict[str, Any]] = []
        for engine_id in engine_ids:
            try:
                engine_path, engine = self._find_json(source_path / "engines", "engine.json", engine_id)
            except RuntimeError:
                follow_ups.append(f"Assign source engine {engine_id} manually; it is not deployable with the current token.")
                continue
            fields = []
            for field_path in engine_path.parent.rglob("*.json"):
                if field_path.name == "engine.json":
                    continue
                field = json.loads(field_path.read_text(encoding="utf-8"))
                if "id" in field:
                    fields.append({"id": field["id"], "name": field.get("name", ""), "targets": self._target()})
            engines.append(
                {
                    "id": engine["id"],
                    "name": engine["name"],
                    "base_path": str(engine_path.parent),
                    "targets": self._target(),
                    "engine_fields": fields,
                }
            )
            items.append(PlanItem(type="engine", source_id=engine["id"], source_name=engine["name"], target_name=engine["name"], dependencies=["queues"]))

        manifest = {
            "target_url": target_url,
            "source_dir": "source/selected",
            "target_dir": "",
            "source_url": source_url,
            "token_owner_id": request.target_hook_owner_id,
            "deployed_org_id": None,
            "patch_target_org": False,
            "ignore_all_deploy_warnings": True,
            "workspaces": [workspace_entry],
            "queues": queues,
            "hooks": hooks,
            "engines": engines,
            "rules": rules,
            "labels": [],
            "email_templates": [],
            "unselected_hooks": [],
            "hook_templates": hook_templates,
            "secrets_file": "",
            "deploy_state_file": "deploy_state.json",
            "reverse_mapping_after_deploy": False,
        }
        return manifest, items, list(dict.fromkeys(warnings)), list(dict.fromkeys(follow_ups))

    async def prepare(
        self,
        request: CopyRequest,
        source_connection: Connection,
        target_connection: Connection,
        source_org: dict[str, Any],
        target_org: dict[str, Any],
    ) -> PreparedDeployment:
        assert_prd2_contract()
        imports = _imports()
        workdir = Path(tempfile.mkdtemp(prefix="easy-prd2-"))
        logs: list[str] = []
        try:
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                source_client, source_path = await self._download_source(workdir, source_connection, source_org["id"])
            logs.extend(line for line in output.getvalue().splitlines() if line.strip())

            # Discover selected hooks before resolving private templates.
            selected_queue_hooks: set[int] = set()
            for queue_id in request.queue_ids:
                _, queue = self._find_json(source_path, "queue.json", queue_id)
                selected_queue_hooks.update(filter(None, (self._id_from_url(url) for url in queue.get("hooks", []))))
            hook_payloads = []
            for hook_id in selected_queue_hooks:
                try:
                    _, hook = self._find_json(source_path / "hooks", "*.json", hook_id)
                    hook_payloads.append(hook)
                except RuntimeError:
                    continue
            hook_templates = await self._resolve_hook_templates(
                hook_payloads, source_connection, target_connection, request.hook_template_overrides
            )
            manifest, items, warnings, follow_ups = self._build_manifest(
                request, source_path, source_connection.api_base, target_connection.api_base, hook_templates
            )
            manifest_path = workdir / "deploy.yaml"
            manifest_path.write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
            yaml_object = imports["DeployYaml"](file=manifest_path.read_text(encoding="utf-8"))
            target_client = imports["CustomAsyncAPIClient"](
                base_url=target_connection.api_base,
                credentials=imports["Token"](token=target_connection.token),
            )
            allowed_fields = set(imports["Organization"].__dataclass_fields__)
            source_model = imports["Organization"](**{key: value for key, value in source_org.items() if key in allowed_fields})
            target_model = imports["Organization"](**{key: value for key, value in target_org.items() if key in allowed_fields})
            orchestrator = imports["DeployOrchestrator"](
                **yaml_object.data,
                client=target_client,
                source_client=source_client,
                source_dir_path=imports["AnyPath"](source_path),
                yaml=yaml_object,
                source_org=source_model,
                target_org=target_model,
                prefer="source",
                no_rebase=True,
                deploy_file_path=imports["AnyPath"](manifest_path),
            )
            output = io.StringIO()
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                await orchestrator.initialize_deploy_objects()
                await orchestrator.initialize_target_objects()
                await orchestrator.compare_object_versions()
            logs.extend(line for line in output.getvalue().splitlines() if line.strip())
            for queue in orchestrator.queues:
                warnings.extend(str(item) for item in getattr(queue, "pending_warnings", []))
            existing_items = {(item.type, item.source_id) for item in items}
            dependency_labels = {
                "schema": ["queue"],
                "inbox": ["queue"],
                "label": ["rules"],
                "email_template": ["rules", "queue"],
                "engine_field": ["engine"],
            }
            for object_type, deploy_object in self._deploy_objects(orchestrator):
                source_id = getattr(deploy_object, "id", None)
                if not source_id or (object_type, int(source_id)) in existing_items:
                    continue
                targets = getattr(deploy_object, "targets", [])
                target_name = getattr(deploy_object, "name", "")
                if targets and getattr(targets[0], "pre_reference_replace_data", None):
                    target_name = targets[0].pre_reference_replace_data.get("name", target_name)
                items.append(
                    PlanItem(
                        type=object_type,
                        source_id=int(source_id),
                        source_name=getattr(deploy_object, "name", "") or object_type.replace("_", " ").title(),
                        target_name=target_name or object_type.replace("_", " ").title(),
                        dependencies=dependency_labels.get(object_type, []),
                    )
                )
                existing_items.add((object_type, int(source_id)))
            return PreparedDeployment(
                request=request,
                workdir=workdir,
                manifest_path=manifest_path,
                yaml_object=yaml_object,
                orchestrator=orchestrator,
                source_connection=source_connection,
                target_connection=target_connection,
                source_org=source_org,
                target_org=target_org,
                items=items,
                warnings=list(dict.fromkeys(warnings)),
                follow_ups=list(dict.fromkeys(follow_ups)),
                logs=logs,
            )
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    @staticmethod
    def _deploy_objects(orchestrator: Any) -> Iterable[tuple[str, Any]]:
        for object_type, objects in (
            ("hook", orchestrator.hooks),
            ("label", orchestrator.labels),
            ("email_template", orchestrator.email_templates),
            ("rule", orchestrator.rules),
            ("engine", orchestrator.engines),
            ("workspace", orchestrator.workspaces),
            ("queue", orchestrator.queues),
        ):
            for item in objects:
                yield object_type, item
                if object_type == "engine":
                    for field in getattr(item, "engine_field_deploy_objects", []):
                        yield "engine_field", field
                if object_type == "queue":
                    yield "inbox", item.inbox_deploy_object
                    yield "schema", item.schema_deploy_object

    def created_objects(self, prepared: PreparedDeployment) -> list[CreatedObject]:
        created: list[CreatedObject] = []
        seen: set[tuple[str, int]] = set()
        for object_type, item in self._deploy_objects(prepared.orchestrator):
            for target in getattr(item, "targets", []):
                remote = getattr(target, "data_from_remote", None) or {}
                object_id = remote.get("id")
                if not object_id:
                    continue
                key = (object_type, int(object_id))
                if key in seen:
                    continue
                seen.add(key)
                created.append(
                    CreatedObject(
                        type=object_type,
                        id=int(object_id),
                        name=remote.get("name", getattr(item, "name", "")),
                        modified_at=remote.get("modified_at"),
                    )
                )
        return created

    async def execute(
        self,
        prepared: PreparedDeployment,
        on_phase: Callable[[str], None] | None = None,
    ) -> tuple[list[CreatedObject], str, str | None]:
        output = io.StringIO()
        error: str | None = None
        try:
            if on_phase:
                on_phase("Creating dependencies")
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                await prepared.orchestrator.run_deploy(is_first=True)
            if on_phase:
                on_phase("Linking copied configuration")
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                await prepared.orchestrator.run_deploy(is_first=False)
                await prepared.orchestrator.save_deploy_state()
                await prepared.orchestrator.save_auto_mappings()
            failed = [
                f"{kind} {getattr(item, 'name', getattr(item, 'id', 'unknown'))}"
                for kind, item in self._deploy_objects(prepared.orchestrator)
                if getattr(item, "deploy_failed", False)
            ]
            if failed:
                error = "PRD2 reported failures for: " + ", ".join(failed)
        except Exception as exc:
            error = redact(exc, (prepared.source_connection.token, prepared.target_connection.token))
        finally:
            # PRD2 may auto-load rule dependencies that were not explicit in
            # the input manifest. Persist them so history and rollback retain
            # every created ID, even after the in-memory orchestrator is gone.
            for key, objects in (
                ("labels", prepared.orchestrator.labels),
                ("email_templates", prepared.orchestrator.email_templates),
            ):
                explicit = {item.get("id") for item in prepared.yaml_object.data.get(key, [])}
                for deploy_object in objects:
                    if deploy_object.id in explicit:
                        continue
                    targets = []
                    for target in deploy_object.targets:
                        remote = target.data_from_remote or {}
                        targets.append({"id": remote.get("id") or target.id})
                    prepared.yaml_object.data.setdefault(key, []).append(
                        {"id": deploy_object.id, "name": deploy_object.name, "targets": targets}
                    )
            await prepared.yaml_object.save_to_file(prepared.manifest_path)
        log_text = "\n".join([*prepared.logs, output.getvalue()])
        return self.created_objects(prepared), log_text, error

    async def rollback(
        self,
        manifest_text: str,
        target_connection: Connection,
        on_phase: Callable[[str], None] | None = None,
    ) -> tuple[str, str | None]:
        imports = _imports()
        from deployment_manager.commands.deploy.subcommands.revert.revert_deploy_file import RevertDeployFile

        target_client = imports["CustomAsyncAPIClient"](
            base_url=target_connection.api_base,
            credentials=imports["Token"](token=target_connection.token),
        )
        yaml_object = imports["DeployYaml"](file=manifest_text)
        release = RevertDeployFile(**yaml_object.data, client=target_client, yaml=yaml_object, plan_only=False)
        output = io.StringIO()
        error = None
        phases = (
            ("Deleting copied hooks", release.revert_hooks),
            ("Deleting copied rules", release.revert_rules),
            ("Deleting copied queues", release.revert_queues),
            ("Deleting copied engines", release.revert_engines),
            ("Deleting copied labels", release.revert_labels),
            ("Deleting copied workspaces", release.revert_workspaces),
        )
        try:
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
                for label, operation in phases:
                    if on_phase:
                        on_phase(label)
                    await operation()
        except Exception as exc:
            error = redact(exc, (target_connection.token,))
        return output.getvalue(), error


def artifact_manifest_is_create_only(manifest_text: str) -> bool:
    data = yaml.safe_load(manifest_text)
    if data.get("patch_target_org"):
        return False

    def check(value: Any) -> bool:
        if isinstance(value, dict):
            if "targets" in value:
                for target in value["targets"] or []:
                    if target.get("id") not in {None, ""}:
                        return False
            return all(check(child) for child in value.values())
        if isinstance(value, list):
            return all(check(child) for child in value)
        return True

    return check(data)
