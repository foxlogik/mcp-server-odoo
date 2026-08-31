"""Tests for the post_message tool (chatter posting that returns an ID)."""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.odoo_connection import OdooConnectionError
from mcp_server_odoo.tools import OdooToolHandler


class TestPostMessageTool:
    """Test the post_message tool."""

    @pytest.fixture
    def mock_app(self):
        app = Mock()
        app.tool = Mock(side_effect=lambda **kwargs: lambda func: func)
        return app

    @pytest.fixture
    def mock_connection(self):
        conn = Mock()
        conn.is_authenticated = True
        conn.execute_kw.return_value = 4242
        conn.build_record_url.return_value = "http://localhost:8069/odoo/project.task/3661"
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
        config.act_as_uid = None
        return config

    @pytest.fixture
    def tool_handler(self, mock_app, mock_connection, mock_access_controller, mock_config):
        return OdooToolHandler(mock_app, mock_connection, mock_access_controller, mock_config)

    @pytest.mark.asyncio
    async def test_posts_through_proxy_and_returns_message_id(
        self, tool_handler, mock_connection
    ):
        """The call goes to the proxy helper, not to message_post directly."""
        result = await tool_handler._handle_post_message_tool(
            "project.task", 3661, "<p>hello</p>"
        )

        assert result["success"] is True
        assert result["message_id"] == 4242
        assert result["record_id"] == 3661
        mock_connection.execute_kw.assert_called_once_with(
            "res.users",
            "mcp_post_message",
            [
                "project.task",
                3661,
                {
                    "body": "<p>hello</p>",
                    "message_type": "comment",
                    "subtype_xmlid": "comment",
                },
            ],
            {},
        )

    @pytest.mark.asyncio
    async def test_optional_fields_are_forwarded(self, tool_handler, mock_connection):
        await tool_handler._handle_post_message_tool(
            "project.task",
            3661,
            "hi",
            subtype="note",
            message_type="notification",
            subject="Heads up",
            partner_ids=[399],
            attachment_ids=[7],
            author_id=399,
            user_id=12,
        )

        args, kwargs = mock_connection.execute_kw.call_args
        values = args[2][2]
        assert values["subtype_xmlid"] == "note"
        assert values["message_type"] == "notification"
        assert values["subject"] == "Heads up"
        assert values["partner_ids"] == [399]
        assert values["attachment_ids"] == [7]
        assert values["author_id"] == 399
        assert args[3] == {"user_id": 12}

    @pytest.mark.asyncio
    async def test_omitted_optionals_are_absent_not_null(
        self, tool_handler, mock_connection
    ):
        """message_post treats an explicit None differently from an absent key."""
        await tool_handler._handle_post_message_tool("project.task", 3661, "hi")

        values = mock_connection.execute_kw.call_args[0][2][2]
        assert "subject" not in values
        assert "partner_ids" not in values
        assert "author_id" not in values

    @pytest.mark.asyncio
    async def test_empty_body_rejected_before_the_call(
        self, tool_handler, mock_connection
    ):
        with pytest.raises(ValidationError):
            await tool_handler._handle_post_message_tool("project.task", 3661, "   ")
        mock_connection.execute_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_unauthenticated_rejected(self, tool_handler, mock_connection):
        mock_connection.is_authenticated = False
        with pytest.raises(ValidationError):
            await tool_handler._handle_post_message_tool("project.task", 3661, "hi")

    @pytest.mark.asyncio
    async def test_missing_proxy_module_names_the_fix(
        self, tool_handler, mock_connection
    ):
        mock_connection.execute_kw.side_effect = OdooConnectionError(
            "Operation failed: 'res.users' object has no attribute 'mcp_post_message'"
        )

        with pytest.raises(ValidationError) as exc:
            await tool_handler._handle_post_message_tool("project.task", 3661, "hi")

        assert "foxlogik_mcp_proxy" in str(exc.value)
