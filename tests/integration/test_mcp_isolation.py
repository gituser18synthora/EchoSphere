"""MCP tool tenant isolation — tools called in-process with a set auth context."""

import pytest

from backend.mcp_server.server import (
    _current_auth,
    get_document_context,
    list_authorized_knowledge_bases,
    search_knowledge,
)

pytestmark = pytest.mark.integration


@pytest.fixture()
def as_tenant():
    """Sets the MCP auth contextvar; cleared by value (tokens are not portable
    across pytest-asyncio task contexts)."""

    def set_auth(tenant_id: str | None, user_id: str = "usr_test"):
        _current_auth.set(
            {"sub": user_id, "tenant_id": tenant_id, "role": "tenant_admin"}
        )

    yield set_auth
    _current_auth.set(None)


class TestMCPTenantIsolation:
    async def test_unauthenticated_rejected(self):
        result = await search_knowledge(query="anything")
        assert result["error"] == "request_error"

    async def test_list_scopes_to_tenant(self, as_tenant, control_plane):
        tenant_a = control_plane.tenant()
        tenant_b = control_plane.tenant()
        kb_a = control_plane.knowledge_source(tenant_a)
        kb_b = control_plane.knowledge_source(tenant_b)
        as_tenant(tenant_a)
        result = await list_authorized_knowledge_bases()
        ids = {row["kb_id"] for row in result["knowledge_bases"]}
        assert kb_a in ids and kb_b not in ids

    async def test_cross_tenant_search_not_found(self, as_tenant, control_plane):
        tenant_a = control_plane.tenant()
        tenant_b = control_plane.tenant()
        kb_b = control_plane.knowledge_source(tenant_b)
        as_tenant(tenant_a)
        result = await search_knowledge(query="secrets", kb_id=kb_b)
        assert result["error"] == "not_found"
        assert "kb" not in result["message"].lower() or "not found" in result["message"].lower()

    async def test_input_validation(self, as_tenant, control_plane):
        as_tenant(control_plane.tenant())
        assert (await search_knowledge(query="   "))["error"] == "request_error"
        assert (await search_knowledge(query="x", top_k=99))["error"] == "request_error"

    async def test_document_context_requires_ownership(self, as_tenant, control_plane):
        as_tenant(control_plane.tenant())
        result = await get_document_context(
            document_id="kdoc_not_yours", chunk_id="chk_x"
        )
        assert result["error"] == "not_found"
