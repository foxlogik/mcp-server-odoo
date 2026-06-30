"""Tests for the call_method tool (allowlisted model methods)."""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.tools import OdooToolHandler


class TestCallMethodTool:
    """Test the allowlisted call_method tool."""

    @pytest.fixture
    def mock_app(self):
        app = Mock()
        app.tool = Mock(side_effect=lambda **kwargs: lambda func: func)
        return app

    @pytest.fixture
    def mock_connection(self):
        conn = Mock()
        conn.is_authenticated = True
        return conn

    @pytest.fixture
    def mock_access_controller(self):
        controller = Mock()
        controller.validate_model_access = Mock()
        return controller

    @pytest.fixture
    def mock_config(self):
        config = Mock()
        config.url = "http://localhost:8069"
        return config

    @pytest.fixture
    def tool_handler(self, mock_app, mock_connection, mock_access_controller, mock_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, mock_config)

    @pytest.mark.asyncio
    async def test_allowlisted_method_with_user_impersonation(
        self, tool_handler, mock_connection
    ):
        """An allowlisted method routes through execute_kw_as_user when user_id is given."""
        mock_connection.execute_kw_as_user.return_value = {"exists": True, "model": "project.task"}

        result = await tool_handler._handle_call_method_tool(
            "bpm.process", "get_builder_reference", ["project.task"], None, user_id=7
        )

        assert result["success"] is True
        assert result["result"] == {"exists": True, "model": "project.task"}
        mock_connection.execute_kw_as_user.assert_called_once_with(
            7, "bpm.process", "get_builder_reference", ["project.task"], {}
        )

    @pytest.mark.asyncio
    async def test_allowlisted_method_without_user_uses_admin(self, tool_handler, mock_connection):
        """Without user_id the call goes through execute_kw (admin session)."""
        mock_connection.execute_kw.return_value = {"valid": True}

        result = await tool_handler._handle_call_method_tool(
            "bpm.process", "validate_bpmn_xml", ["<xml/>", "sale.order"], None
        )

        assert result["success"] is True
        mock_connection.execute_kw.assert_called_once_with(
            "bpm.process", "validate_bpmn_xml", ["<xml/>", "sale.order"], {}
        )

    @pytest.mark.asyncio
    async def test_non_allowlisted_method_is_rejected(self, tool_handler, mock_connection):
        """A method not on the allowlist is rejected before any Odoo call."""
        with pytest.raises(ValidationError, match="not callable"):
            await tool_handler._handle_call_method_tool("bpm.process", "unlink", [[1]], None)
        mock_connection.execute_kw.assert_not_called()
        mock_connection.execute_kw_as_user.assert_not_called()

    @pytest.mark.asyncio
    async def test_method_on_non_allowlisted_model_is_rejected(self, tool_handler, mock_connection):
        """Even a CRUD method on a model with no allowlist entry is rejected."""
        with pytest.raises(ValidationError, match="not callable"):
            await tool_handler._handle_call_method_tool("res.users", "write", [[1], {}], None)
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_args_accepts_json_string(self, tool_handler, mock_connection):
        """args/kwargs may arrive as JSON strings and are coerced to list/dict."""
        mock_connection.execute_kw.return_value = {"process_id": 99}

        await tool_handler._handle_call_method_tool(
            "bpm.process",
            "create_draft_process_from_spec",
            '[{"name": "WF", "model_name": "sale.order"}]',
            None,
        )

        mock_connection.execute_kw.assert_called_once_with(
            "bpm.process",
            "create_draft_process_from_spec",
            [{"name": "WF", "model_name": "sale.order"}],
            {},
        )

    @pytest.mark.asyncio
    async def test_bad_json_args_raises(self, tool_handler):
        """Malformed JSON in args is reported clearly."""
        with pytest.raises(ValidationError, match="not valid JSON"):
            await tool_handler._handle_call_method_tool(
                "bpm.process", "validate_bpmn_xml", "{not json", None
            )

    @pytest.mark.asyncio
    async def test_wrong_args_type_raises(self, tool_handler):
        """args that decode to a non-list are rejected."""
        with pytest.raises(ValidationError, match="must be a list"):
            await tool_handler._handle_call_method_tool(
                "bpm.process", "validate_bpmn_xml", '{"a": 1}', None
            )
