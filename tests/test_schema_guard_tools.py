"""Tool-layer behaviour added alongside the schema guards.

Covers the three things a caller experiences directly: the res_id alias, what an
access refusal now says, and the collapse of an XML-RPC traceback to its final
line. Each one corresponds to a class of production failure captured in
``tests/fixtures/mcp_failures.json``.
"""

from unittest.mock import Mock

import pytest

from mcp_server_odoo.error_handling import ValidationError
from mcp_server_odoo.error_sanitizer import ErrorSanitizer
from mcp_server_odoo.tools import OdooToolHandler

# Verbatim shape of a captured production failure: paths and line numbers already
# stripped by the sanitizer, forty frames of context, one useful line at the end.
XMLRPC_DUMP = """Failed to execute search_read on daily_construction.report:
  file, in xmlrpc_2 response = self._xmlrpc(service)
  file, in _xmlrpc result = dispatch_rpc(service, method, params)
  file, in dispatch_rpc return dispatch(method, params)
  file, in dispatch res = execute_kw(db, uid, *params[3:])
  file, in _anyfy_leaves left, operator, right = item = tuple(item)
ValueError: not enough values to unpack (expected 3, got 2)"""


@pytest.fixture
def handler():
    app = Mock()
    connection = Mock()
    access_controller = Mock()
    config = Mock()
    config.default_limit = 10
    config.max_limit = 100
    config.max_smart_fields = 30
    config.is_yolo_enabled = False
    config.yolo_mode = "off"
    config.act_as_uid = None
    return OdooToolHandler(app, connection, access_controller, config)


class TestRecordIdAlias:
    def test_record_id_used(self, handler):
        assert handler._resolve_record_id(39, None) == 39

    def test_res_id_accepted(self, handler):
        assert handler._resolve_record_id(None, 39) == 39

    def test_matching_values_accepted(self, handler):
        assert handler._resolve_record_id(39, 39) == 39

    def test_conflict_rejected(self, handler):
        with pytest.raises(ValidationError, match="disagree"):
            handler._resolve_record_id(39, 52)

    def test_neither_given_rejected(self, handler):
        with pytest.raises(ValidationError, match="record_id is required"):
            handler._resolve_record_id(None, None)


class TestAccessHint:
    def test_all_fields_refusal_names_the_real_culprit(self, handler):
        message = (
            "Connection error: You are not allowed to access 'Metrics Capture' "
            "(project.metric.capture) records."
        )
        hint = handler._access_hint("project.task", ["__all__"], message)
        assert "project.metric.capture" in hint
        assert 'fields=["__all__"]' in hint
        assert "explicit field list" in hint

    def test_plain_refusal_says_retrying_will_not_help(self, handler):
        message = "Access denied: You are not allowed to access 'Sites' (site_management.site)."
        hint = handler._access_hint("site_management.site", None, message)
        assert "fail identically" in hint

    def test_unrelated_message_untouched(self, handler):
        message = "Connection error: Operation timed out"
        assert handler._access_hint("res.partner", None, message) == message


class TestExceptionTail:
    def test_traceback_collapses_to_final_line(self):
        tail = ErrorSanitizer.extract_exception_tail(XMLRPC_DUMP)
        assert tail == "ValueError: not enough values to unpack (expected 3, got 2)"

    def test_plain_message_is_not_a_traceback(self):
        assert ErrorSanitizer.extract_exception_tail("Record not found") is None

    def test_debug_env_keeps_full_trace(self, monkeypatch):
        monkeypatch.setenv("ODOO_MCP_DEBUG_TRACES", "1")
        assert ErrorSanitizer.extract_exception_tail(XMLRPC_DUMP) is None

    def test_sanitize_message_is_short(self):
        sanitized = ErrorSanitizer.sanitize_message(XMLRPC_DUMP)
        assert "dispatch_rpc" not in sanitized
        assert "xmlrpc_2" not in sanitized
        assert len(sanitized) < 120

    def test_mappings_still_apply_to_the_tail(self):
        dump = 'file, in dispatch_rpc\nValueError: Invalid field \'date\' in leaf'
        sanitized = ErrorSanitizer.sanitize_message(dump)
        assert "Invalid field 'date' in search criteria" == sanitized

    def test_impersonation_refusal_names_the_way_out(self):
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(
            "Only system administrators may use MCP user impersonation."
        )
        assert "Retry without user_id" in sanitized


class TestSearchGuards:
    @pytest.mark.asyncio
    async def test_malformed_leaf_rejected_before_rpc(self, handler):
        handler.connection.is_authenticated = True
        await _assert_rejected(
            handler,
            domain=[["model", "=", "discuss.channel"], ["res_id", 762]],
            expected="2 elements",
        )
        handler.connection.search_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_unknown_domain_field_rejected_with_candidate(self, handler):
        handler.connection.is_authenticated = True
        handler.connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "report_date": {"type": "date", "string": "Date"},
        }
        await _assert_rejected(
            handler,
            domain=[["date", "=", "2026-08-20"]],
            expected="report_date (Date)",
            model="daily_construction.report",
        )
        handler.connection.search_count.assert_not_called()

    @pytest.mark.asyncio
    async def test_valid_call_is_untouched(self, handler):
        handler.connection.is_authenticated = True
        handler.connection.fields_get.return_value = {
            "id": {"type": "integer", "string": "ID"},
            "report_date": {"type": "date", "string": "Date"},
        }
        handler.connection.search_count.return_value = 0
        handler.connection.search.return_value = []
        result = await handler._handle_search_tool(
            "daily_construction.report",
            [["report_date", "=", "2026-08-20"]],
            ["id", "report_date"],
            10,
            0,
            None,
        )
        assert result["total"] == 0
        handler.connection.search_count.assert_called_once()


async def _assert_rejected(handler, domain, expected, model="mail.message"):
    with pytest.raises(ValidationError) as excinfo:
        await handler._handle_search_tool(model, domain, None, 10, 0, None)
    assert expected in str(excinfo.value)
