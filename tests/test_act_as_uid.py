"""Tests for act-as-uid session pinning (ODOO_ACT_AS_UID).

Covers:
  - config parsing: unpinned signals, valid pins, malformed values fail closed
  - _effective_user_id: pinned sessions clamp any model-supplied user_id
  - server: resources are NOT registered when the session is pinned
"""

import os
from unittest.mock import Mock, patch

import pytest

from mcp_server_odoo.config import OdooConfig, load_config
from mcp_server_odoo.tools import OdooToolHandler


def _make_config(**overrides) -> OdooConfig:
    defaults = dict(url="http://localhost:8069", api_key="test-key")
    defaults.update(overrides)
    return OdooConfig(**defaults)


def _load_config_with_act_as_uid(value):
    env = {
        "ODOO_URL": "http://localhost:8069",
        "ODOO_API_KEY": "test-key",
        "ODOO_ACT_AS_UID": value,
    }
    with patch.dict(os.environ, env, clear=False):
        return load_config()


class TestActAsUidParsing:
    """ODOO_ACT_AS_UID env parsing in load_config()."""

    def test_unset_means_unpinned(self):
        env = {"ODOO_URL": "http://localhost:8069", "ODOO_API_KEY": "test-key"}
        with patch.dict(os.environ, env, clear=False):
            os.environ.pop("ODOO_ACT_AS_UID", None)
            config = load_config()
        assert config.act_as_uid is None
        assert not config.is_user_pinned

    def test_empty_string_means_unpinned(self):
        config = _load_config_with_act_as_uid("")
        assert config.act_as_uid is None
        assert not config.is_user_pinned

    def test_unsubstituted_literal_means_unpinned(self):
        # "${ODOO_ACT_AS_UID}" survives only when the spawning environment
        # never defined the var — an interactive, deliberately unpinned session.
        config = _load_config_with_act_as_uid("${ODOO_ACT_AS_UID}")
        assert config.act_as_uid is None
        assert not config.is_user_pinned

    def test_positive_integer_pins(self):
        config = _load_config_with_act_as_uid("42")
        assert config.act_as_uid == 42
        assert config.is_user_pinned

    def test_whitespace_stripped(self):
        config = _load_config_with_act_as_uid("  7  ")
        assert config.act_as_uid == 7

    @pytest.mark.parametrize("bad", ["abc", "-5", "0", "4.2", "1; DROP", "None"])
    def test_malformed_value_fails_closed(self, bad):
        # A malformed pin must abort startup, never silently run as admin.
        with pytest.raises(ValueError, match="ODOO_ACT_AS_UID"):
            _load_config_with_act_as_uid(bad)


class TestEffectiveUserId:
    """_effective_user_id clamps model-supplied user_id in pinned sessions."""

    def _handler(self, act_as_uid=None):
        app = Mock()
        app.tool = Mock(side_effect=lambda **kwargs: lambda func: func)
        connection = Mock()
        connection.is_authenticated = True
        return OdooToolHandler(
            app, connection, Mock(), _make_config(act_as_uid=act_as_uid)
        )

    def test_unpinned_passes_user_id_through(self):
        handler = self._handler(act_as_uid=None)
        assert handler._effective_user_id(7) == 7

    def test_unpinned_none_stays_none(self):
        handler = self._handler(act_as_uid=None)
        assert handler._effective_user_id(None) is None

    def test_pinned_overrides_model_supplied_user_id(self):
        handler = self._handler(act_as_uid=42)
        assert handler._effective_user_id(7) == 42

    def test_pinned_applies_when_no_user_id_supplied(self):
        handler = self._handler(act_as_uid=42)
        assert handler._effective_user_id(None) == 42

    def test_pinned_same_uid_is_kept(self):
        handler = self._handler(act_as_uid=42)
        assert handler._effective_user_id(42) == 42


class TestPinnedResourceRegistration:
    """Pinned sessions must not expose odoo:// resources (admin-read bypass)."""

    def _server(self, config):
        from mcp_server_odoo.server import OdooMCPServer

        return OdooMCPServer(config=config)

    def test_resources_skipped_when_pinned(self):
        server = self._server(_make_config(act_as_uid=42))
        server.connection = Mock()
        server.access_controller = Mock()
        with patch("mcp_server_odoo.server.register_resources") as reg:
            server._register_resources()
        reg.assert_not_called()
        assert server.resource_handler is None

    def test_resources_registered_when_unpinned(self):
        server = self._server(_make_config())
        server.connection = Mock()
        server.access_controller = Mock()
        with patch("mcp_server_odoo.server.register_resources") as reg:
            server._register_resources()
        reg.assert_called_once()
