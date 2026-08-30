import httpx
import pytest

from easy_prd2.rossum import Connection, RossumError, RossumGateway


@pytest.mark.asyncio
async def test_connect_and_paginated_organization_discovery():
    def handler(request: httpx.Request):
        assert request.headers["authorization"] == "Bearer token-value"
        if request.url.path.endswith("/auth/user"):
            return httpx.Response(200, json={"id": 7, "username": "presales@example.com"})
        if request.url.params.get("page") == "2":
            return httpx.Response(
                200,
                json={
                    "results": [{"id": 2, "name": "Second", "url": "https://demo.rossum.app/api/v1/organizations/2"}],
                    "pagination": {"next": None},
                },
            )
        return httpx.Response(
            200,
            json={
                "results": [{"id": 1, "name": "First", "url": "https://demo.rossum.app/api/v1/organizations/1"}],
                "pagination": {"next": "https://demo.rossum.app/api/v1/organizations?page=2"},
            },
        )

    gateway = RossumGateway(transport=httpx.MockTransport(handler))
    base, user = await gateway.connect("demo.rossum.app", "token-value")
    assert base == "https://demo.rossum.app/api/v1"
    assert user["id"] == 7
    organizations = await gateway.organizations(Connection("id", base, "token-value", user))
    assert [item.id for item in organizations] == [1, 2]


@pytest.mark.asyncio
async def test_invalid_token_is_reported_without_echoing_token():
    gateway = RossumGateway(transport=httpx.MockTransport(lambda _: httpx.Response(401)))
    with pytest.raises(RossumError, match="invalid or expired") as error:
        await gateway.connect("demo.rossum.app", "do-not-echo")
    assert "do-not-echo" not in str(error.value)


@pytest.mark.asyncio
async def test_membership_token_creates_scoped_connection():
    def handler(request: httpx.Request):
        if request.url.path.endswith("/auth/membership_token"):
            assert request.headers["authorization"] == "Bearer group-token"
            return httpx.Response(200, json={"key": "scoped-token"})
        if request.url.path.endswith("/auth/user"):
            assert request.headers["authorization"] == "Bearer scoped-token"
            return httpx.Response(200, json={"id": 9, "username": "admin@example.com"})
        return httpx.Response(404)

    gateway = RossumGateway(transport=httpx.MockTransport(handler))
    scoped = await gateway.scoped_connection(
        Connection("group", "https://demo.rossum.app/api/v1", "group-token", {"id": 9}), 42
    )
    assert scoped.token == "scoped-token"
    assert scoped.user["id"] == 9

