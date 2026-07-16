"""Tests for the per-user metadata tools (get_fields / get_defaults /
check_access) and user-scoped smart default field selection.

These are the discovery channels for pinned sessions: normal users cannot
read ir.model / ir.model.fields raw (admin-only ACL in core), so metadata
must flow through fields_get / default_get / check_access_rights computed
AS the pinned user.
"""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.config import OdooConfig
from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.tools import DEFAULT_FIELD_ATTRIBUTES, OdooToolHandler


def _make_handler(act_as_uid=None):
    app = Mock()
    app.tool = Mock(side_effect=lambda **kwargs: lambda func: func)
    connection = Mock()
    connection.is_authenticated = True
    config = OdooConfig(
        url="http://localhost:8069", api_key="test-key", act_as_uid=act_as_uid
    )
    handler = OdooToolHandler(app, connection, Mock(), config)
    return handler, connection


class TestGetFieldsTool:
    async def test_unpinned_uses_admin_connection(self):
        handler, conn = _make_handler()
        conn.fields_get.return_value = {"name": {"type": "char"}}

        result = await handler._handle_get_fields_tool("res.partner", None)

        conn.fields_get.assert_called_once_with(
            "res.partner", attributes=DEFAULT_FIELD_ATTRIBUTES
        )
        conn.execute_kw_as_user.assert_not_called()
        assert result["user_scoped"] is False
        assert result["total"] == 1

    async def test_user_scoped_routes_through_impersonation(self):
        handler, conn = _make_handler()
        conn.execute_kw_as_user.return_value = {"name": {"type": "char"}}

        result = await handler._handle_get_fields_tool(
            "res.partner", None, user_id=7
        )

        conn.execute_kw_as_user.assert_called_once_with(
            7, "res.partner", "fields_get", [],
            {"attributes": DEFAULT_FIELD_ATTRIBUTES},
        )
        conn.fields_get.assert_not_called()
        assert result["user_scoped"] is True

    async def test_custom_attributes_forwarded(self):
        handler, conn = _make_handler()
        conn.fields_get.return_value = {}

        await handler._handle_get_fields_tool("res.partner", ["required"])

        conn.fields_get.assert_called_once_with(
            "res.partner", attributes=["required"]
        )


class TestGetDefaultsTool:
    async def test_unpinned_uses_admin_connection(self):
        handler, conn = _make_handler()
        conn.execute_kw.return_value = {"date": "2026-07-16"}

        result = await handler._handle_get_defaults_tool(
            "account.analytic.line", ["date"]
        )

        conn.execute_kw.assert_called_once_with(
            "account.analytic.line", "default_get", [["date"]], {}
        )
        assert result["defaults"] == {"date": "2026-07-16"}
        assert result["user_scoped"] is False

    async def test_user_scoped_routes_through_impersonation(self):
        handler, conn = _make_handler()
        conn.execute_kw_as_user.return_value = {"user_id": 7}

        result = await handler._handle_get_defaults_tool(
            "account.analytic.line", ["user_id"], user_id=7
        )

        conn.execute_kw_as_user.assert_called_once_with(
            7, "account.analytic.line", "default_get", [["user_id"]], {}
        )
        assert result["user_scoped"] is True

    async def test_empty_fields_rejected(self):
        handler, _conn = _make_handler()
        with pytest.raises(ValidationError):
            await handler._handle_get_defaults_tool("res.partner", [])


class TestCheckAccessTool:
    async def test_invalid_operation_rejected(self):
        handler, _conn = _make_handler()
        with pytest.raises(ValidationError, match="Invalid operation"):
            await handler._handle_check_access_tool("res.partner", "sudo")

    async def test_user_scoped_routes_through_impersonation(self):
        handler, conn = _make_handler()
        conn.execute_kw_as_user.return_value = False

        result = await handler._handle_check_access_tool(
            "account.analytic.line", "create", user_id=7
        )

        conn.execute_kw_as_user.assert_called_once_with(
            7, "account.analytic.line", "check_access_rights",
            ["create"], {"raise_exception": False},
        )
        assert result["allowed"] is False
        assert result["user_scoped"] is True

    async def test_unpinned_uses_admin_connection(self):
        handler, conn = _make_handler()
        conn.execute_kw.return_value = True

        result = await handler._handle_check_access_tool("res.partner", "read")

        conn.execute_kw.assert_called_once_with(
            "res.partner", "check_access_rights", ["read"],
            {"raise_exception": False},
        )
        assert result["allowed"] is True


class TestSmartDefaultsUserScoped:
    """Smart default field selection must use the PINNED user's fields_get —
    the admin list can include groups=-protected fields whose explicit read
    as the user raises AccessError and kills the whole tool call."""

    FIELDS = {
        "id": {"type": "integer", "required": False, "store": True},
        "name": {"type": "char", "required": True, "store": True},
    }

    def test_pinned_selection_uses_impersonated_fields_get(self):
        handler, conn = _make_handler()
        conn.execute_kw_as_user.return_value = self.FIELDS

        fields = handler._get_smart_default_fields("res.partner", user_id=42)

        conn.execute_kw_as_user.assert_called_once_with(
            42, "res.partner", "fields_get", [], {}
        )
        assert "name" in fields

    def test_unpinned_selection_uses_admin_fields_get(self):
        handler, conn = _make_handler()
        conn.fields_get.return_value = self.FIELDS

        fields = handler._get_smart_default_fields("res.partner")

        conn.fields_get.assert_called_once_with("res.partner", attributes=None)
        conn.execute_kw_as_user.assert_not_called()
        assert "name" in fields
