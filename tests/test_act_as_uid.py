"""Tests for the session-wide impersonation pin (ODOO_ACT_AS_UID).

Before this, `user_id` was an optional per-call argument with no default: a session
that simply never passed it ran every operation as the connection account — silently,
with no log line. claude-service exported ODOO_ACT_AS_UID and the .mcp-*.json configs
passed it through, but nothing read it, so the pin was inert.

These tests pin the three things that make it real: the env var is parsed, it applies
without the model's cooperation, and schema introspection stays on the connection
account (ir.model.fields read is base.group_erp_manager-only, so pinning it to an
ordinary employee would break field discovery for that user).
"""

import os
from unittest.mock import Mock, patch

import pytest

from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.tools import OdooToolHandler


class TestActAsUidConfig:
    """ODOO_ACT_AS_UID parsing and validation."""

    def _load(self, value):
        env = {"ODOO_URL": "http://localhost:8069", "ODOO_API_KEY": "k"}
        if value is not None:
            env["ODOO_ACT_AS_UID"] = value
        with patch.dict(os.environ, env, clear=True):
            return load_config()

    def test_unset_is_unpinned(self):
        assert self._load(None).act_as_uid is None

    def test_empty_string_is_unpinned(self):
        """claude-service always exports the variable, using "" for "not pinned"
        (stream_runner.run_streaming). Empty must not raise."""
        assert self._load("").act_as_uid is None
        assert self._load("   ").act_as_uid is None

    def test_numeric_value_is_parsed(self):
        assert self._load("42").act_as_uid == 42

    def test_non_numeric_raises(self):
        """A wiring mistake must fail at startup. Silently ignoring it would run the
        session as the connection account while the operator believed it was pinned."""
        with pytest.raises(ValueError, match="ODOO_ACT_AS_UID"):
            self._load("not-a-uid")

    @pytest.mark.parametrize("bad", [0, -1])
    def test_non_positive_uid_raises(self, bad):
        with pytest.raises(ValueError, match="positive integer"):
            OdooConfig(url="http://localhost:8069", api_key="k", act_as_uid=bad)


class TestEffectiveUserId:
    """Resolution order: explicit argument wins, then the session pin."""

    @pytest.fixture
    def handler(self):
        app = Mock()
        app.tool = Mock(side_effect=lambda **kwargs: lambda func: func)
        conn = Mock()
        conn.is_authenticated = True
        return lambda pin: OdooToolHandler(app, conn, Mock(), Mock(act_as_uid=pin))

    def test_explicit_user_id_wins(self, handler):
        assert handler(99)._effective_user_id(7) == 7

    def test_pin_applies_when_argument_omitted(self, handler):
        assert handler(99)._effective_user_id(None) == 99

    def test_unpinned_stays_none(self, handler):
        """Trusted automation with no end user behind it runs as the connection
        account — unchanged behaviour."""
        assert handler(None)._effective_user_id(None) is None


class TestPinAppliesWithoutModelCooperation:
    """The point of the fix: the model never has to pass user_id."""

    @pytest.fixture
    def parts(self):
        app = Mock()
        app.tool = Mock(side_effect=lambda **kwargs: lambda func: func)
        conn = Mock()
        conn.is_authenticated = True
        conn.fields_get.return_value = {"name": {"type": "char", "string": "Name"}}
        access = Mock()
        access.validate_model_access = Mock()
        return app, conn, access

    def _handler(self, parts, pin):
        app, conn, access = parts
        config = Mock(act_as_uid=pin, max_limit=100, default_limit=10, max_smart_fields=15)
        return OdooToolHandler(app, conn, access, config), conn

    @pytest.mark.asyncio
    async def test_search_routes_through_impersonation_when_pinned(self, parts):
        handler, conn = self._handler(parts, 42)
        conn.execute_kw_as_user.side_effect = [1, [5], [{"id": 5, "name": "x"}]]

        await handler._handle_search_tool("project.task", [], ["name"], 10, 0, None)

        assert conn.execute_kw_as_user.called, "pinned search must impersonate"
        assert all(c.args[0] == 42 for c in conn.execute_kw_as_user.call_args_list)
        assert not conn.search_count.called, "must not fall back to the admin path"

    @pytest.mark.asyncio
    async def test_search_uses_connection_account_when_unpinned(self, parts):
        handler, conn = self._handler(parts, None)
        conn.search_count.return_value = 1
        conn.search.return_value = [5]
        conn.read.return_value = [{"id": 5, "name": "x"}]

        await handler._handle_search_tool("project.task", [], ["name"], 10, 0, None)

        assert conn.search_count.called
        assert not conn.execute_kw_as_user.called

    @pytest.mark.asyncio
    async def test_field_metadata_stays_on_connection_account(self, parts):
        """fields_get must NOT be pinned: ir.model.fields read is erp_manager-only,
        so routing it through an ordinary employee breaks the discovery protocol."""
        handler, conn = self._handler(parts, 42)
        conn.execute_kw_as_user.side_effect = [1, [5], [{"id": 5, "name": "x"}]]

        await handler._handle_search_tool("project.task", [], None, 10, 0, None)

        for call in conn.execute_kw_as_user.call_args_list:
            assert call.args[1] != "ir.model.fields", "field metadata must not be pinned"
