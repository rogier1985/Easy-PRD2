from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, AsyncIterator
from urllib.parse import urljoin, urlparse

import httpx

from .models import AdminSummary, OrganizationSummary, QueueSummary, WorkspaceSummary
from .security import normalize_api_base


class RossumError(RuntimeError):
    pass


@dataclass
class Connection:
    id: str
    api_base: str
    token: str
    user: dict[str, Any]


class ConnectionStore:
    def __init__(self) -> None:
        self._connections: dict[str, Connection] = {}

    def add(self, api_base: str, token: str, user: dict[str, Any]) -> Connection:
        connection = Connection(str(uuid.uuid4()), api_base, token, user)
        self._connections[connection.id] = connection
        return connection

    def get(self, connection_id: str) -> Connection:
        try:
            return self._connections[connection_id]
        except KeyError as exc:
            raise RossumError("Connection expired. Reconnect and try again.") from exc

    def remember(self, connection: Connection) -> Connection:
        self._connections[connection.id] = connection
        return connection

    @property
    def tokens(self) -> tuple[str, ...]:
        return tuple(item.token for item in self._connections.values())

    def clear(self) -> None:
        self._connections.clear()


class RossumGateway:
    def __init__(self, timeout: float = 30.0, transport: httpx.AsyncBaseTransport | None = None):
        self.timeout = timeout
        self.transport = transport

    def _client(self, connection: Connection) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=connection.api_base.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {connection.token}", "Accept": "application/json"},
            timeout=self.timeout,
            follow_redirects=False,
            transport=self.transport,
        )

    async def connect(self, api_base: str, token: str) -> tuple[str, dict[str, Any]]:
        normalized = normalize_api_base(api_base)
        temporary = Connection("", normalized, token.strip(), {})
        async with self._client(temporary) as client:
            response = await client.get("auth/user")
            self._raise(response, "Could not validate this Rossum connection")
            return normalized, response.json()

    async def _pages(self, connection: Connection, path: str, params: dict[str, Any] | None = None) -> AsyncIterator[dict]:
        async with self._client(connection) as client:
            next_url: str | None = path
            first = True
            while next_url:
                response = await client.get(next_url, params=params if first else None)
                self._raise(response, "Could not load Rossum data")
                body = response.json()
                for item in body.get("results", []):
                    yield item
                next_url = body.get("pagination", {}).get("next")
                if next_url:
                    parsed = urlparse(next_url)
                    if parsed.netloc and parsed.netloc != urlparse(connection.api_base).netloc:
                        raise RossumError("Rossum returned an unsafe pagination URL")
                first = False

    async def organizations(self, connection: Connection) -> list[OrganizationSummary]:
        items = [item async for item in self._pages(connection, "organizations", {"include_membership_organizations": "true"})]
        return [OrganizationSummary(id=item["id"], name=item.get("name") or f"Organization {item['id']}", url=item["url"]) for item in items]

    async def scoped_connection(self, connection: Connection, organization_id: int) -> Connection:
        org_url = f"{connection.api_base}/organizations/{organization_id}"
        async with self._client(connection) as client:
            response = await client.post("auth/membership_token", json={"organization": org_url})
            if response.status_code in {400, 403, 404, 405}:
                # A token may already be scoped to its primary organization.
                org_response = await client.get(f"organizations/{organization_id}")
                self._raise(org_response, "You do not have access to the selected organization")
                user_response = await client.get("auth/user")
                self._raise(user_response, "Could not verify the selected organization")
                return Connection(str(uuid.uuid4()), connection.api_base, connection.token, user_response.json())
            self._raise(response, "Could not create an organization-scoped token")
            scoped_token = response.json()["key"]
        scoped = Connection(str(uuid.uuid4()), connection.api_base, scoped_token, {})
        async with self._client(scoped) as client:
            user_response = await client.get("auth/user")
            self._raise(user_response, "Could not validate organization access")
            scoped.user = user_response.json()
        return scoped

    async def organization(self, connection: Connection, organization_id: int) -> OrganizationSummary:
        async with self._client(connection) as client:
            response = await client.get(f"organizations/{organization_id}")
            self._raise(response, "Could not load the organization")
            item = response.json()
        return OrganizationSummary(id=item["id"], name=item.get("name") or f"Organization {item['id']}", url=item["url"])

    async def raw_organization(self, connection: Connection, organization_id: int) -> dict[str, Any]:
        async with self._client(connection) as client:
            response = await client.get(f"organizations/{organization_id}")
            self._raise(response, "Could not load the organization")
            return response.json()

    async def workspaces(self, connection: Connection, organization_id: int) -> list[WorkspaceSummary]:
        items = [item async for item in self._pages(connection, "workspaces", {"organization": organization_id})]
        return [WorkspaceSummary(id=item["id"], name=item.get("name") or f"Workspace {item['id']}", url=item["url"]) for item in items]

    async def queues(self, connection: Connection, workspace_id: int) -> list[QueueSummary]:
        items = [item async for item in self._pages(connection, "queues", {"workspace": workspace_id})]
        return [
            QueueSummary(
                id=item["id"],
                name=item.get("name") or f"Queue {item['id']}",
                url=item["url"],
                workspace_id=workspace_id,
            )
            for item in items
        ]

    async def admins(self, connection: Connection) -> list[AdminSummary]:
        users = [item async for item in self._pages(connection, "users")]
        roles = [item async for item in self._pages(connection, "groups")]
        admin_urls = {role.get("url") for role in roles if role.get("name") in {"admin", "organization_group_admin"}}
        result = [
            AdminSummary(id=user["id"], username=user.get("username") or user.get("email") or str(user["id"]))
            for user in users
            if admin_urls.intersection(user.get("groups", []))
        ]
        return sorted(result, key=lambda item: item.username.casefold())

    async def list_raw(self, connection: Connection, resource: str, params: dict[str, Any] | None = None) -> list[dict]:
        return [item async for item in self._pages(connection, resource, params)]

    async def get_raw(self, connection: Connection, resource: str, object_id: int | str) -> dict[str, Any]:
        async with self._client(connection) as client:
            response = await client.get(f"{resource}/{object_id}")
            self._raise(response, f"Could not load {resource.rstrip('s')}")
            return response.json()

    @staticmethod
    def _raise(response: httpx.Response, message: str) -> None:
        if response.is_success:
            return
        if response.status_code == 401:
            raise RossumError("The API token is invalid or expired")
        if response.status_code == 403:
            raise RossumError("Your Rossum account does not have permission for this operation")
        detail = ""
        try:
            detail = response.json().get("detail", "")
        except Exception:
            pass
        raise RossumError(f"{message}{': ' + detail if detail else ''} ({response.status_code})")
