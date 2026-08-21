"""Tests for error message sanitization."""

from mcp_server_odoo.error_sanitizer import ErrorSanitizer


class TestErrorSanitizer:
    """Test error message sanitization functionality."""

    def test_sanitize_file_paths(self):
        """Test that file paths are removed."""
        message = 'File "/home/user/odoo/models.py", line 123, in execute'
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "/home/user" not in sanitized
        assert "line 123" not in sanitized
        assert ".py" not in sanitized

    def test_sanitize_module_paths(self):
        """Test that module paths are removed."""
        message = "mcp_server_odoo.odoo_connection: Connection failed"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "mcp_server_odoo." not in sanitized
        assert "Connection failed" in sanitized

    def test_sanitize_class_names(self):
        """Test that class names are removed."""
        message = "Error: <class 'xmlrpc.client.Fault'> occurred"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "<class" not in sanitized
        assert "xmlrpc.client" not in sanitized

    def test_sanitize_memory_addresses(self):
        """Test that memory addresses are removed."""
        message = "Object at 0x7f8b8c0d5f40 not found"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "0x7f8b8c0d5f40" not in sanitized
        assert "Object at" not in sanitized

    def test_sanitize_traceback(self):
        """Test that traceback information is removed."""
        message = """Traceback (most recent call last):
          File "test.py", line 10, in <module>
            raise ValueError("Test error")
        ValueError: Test error"""
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "Traceback" not in sanitized
        assert 'File "test.py"' not in sanitized
        assert "Test error" in sanitized

    def test_field_error_mapping(self):
        """Test specific field error mappings."""
        message = "Invalid field res.partner.invalid_field in leaf ('invalid_field', '=', True)"
        sanitized = ErrorSanitizer.sanitize_message(message)
        # The sanitizer extracts just the field name, not the full model.field path
        assert sanitized == "Invalid field 'invalid_field' in search criteria"

        message = "Field bogus_field does not exist"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Field 'bogus_field' does not exist on this model"

    def test_model_error_mapping(self):
        """Test model error mappings."""
        message = "Model sale.order does not exist"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Model 'sale.order' is not available"

    def test_connection_error_mapping(self):
        """Test connection error mappings."""
        message = "Connection refused"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Cannot connect to Odoo server"

        message = "Operation timeout after 30 seconds"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized == "Request timed out"

    def test_xmlrpc_fault_sanitization(self):
        """Test XML-RPC fault message sanitization."""
        fault = "Access Denied: Invalid API key or insufficient permissions"
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "Access denied: Invalid credentials or insufficient permissions"

        fault = "ValidationError: Field 'vat' is required"
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "Validation error: Please check your input"

        fault = "UserError('Cannot delete record that has dependencies')"
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "Cannot delete record that has dependencies"

    def test_sanitize_missing_error(self):
        """Test that MissingError fault is sanitized to a user-friendly message."""
        fault = "MissingError: Record does not exist or has been deleted."
        sanitized = ErrorSanitizer.sanitize_xmlrpc_fault(fault)
        assert sanitized == "The requested record was not found"

    def test_sanitize_error_details(self):
        """Test error details sanitization."""
        details = {
            "error_type": "ValidationError",
            "traceback": "Full traceback here...",
            "model": "res.partner",
            "operation": "create",
            "internal_path": "/opt/odoo/addons",
        }

        sanitized = ErrorSanitizer.sanitize_error_details(details)

        assert "traceback" not in sanitized
        assert "internal_path" not in sanitized
        assert sanitized["model"] == "res.partner"
        assert sanitized["operation"] == "create"
        assert sanitized["category"] == "validation_error"

    def test_error_type_mapping(self):
        """Test internal error type mapping."""
        assert ErrorSanitizer._map_error_type("ValidationError") == "validation_error"
        assert ErrorSanitizer._map_error_type("OdooConnectionError") == "connection_error"
        assert ErrorSanitizer._map_error_type("NotFoundError") == "not_found"
        assert ErrorSanitizer._map_error_type("UnknownError") == "error"

    def test_empty_message_handling(self):
        """Test handling of empty messages."""
        assert ErrorSanitizer.sanitize_message("") == "An error occurred"
        assert ErrorSanitizer.sanitize_message(None) == "An error occurred"

    def test_preserve_useful_information(self):
        """Test that useful information is preserved."""
        message = "Cannot find partner with email test@example.com"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "test@example.com" in sanitized

        message = "Invalid value 'abc' for integer field"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "'abc'" in sanitized
        assert "integer" in sanitized

    def test_capitalization(self):
        """Test that messages are properly capitalized."""
        message = "connection failed"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert sanitized[0].isupper()

    def test_internal_details_removal(self):
        """Test removal of internal implementation details."""
        message = "MCPObjectController: Invalid field res.partner.test_field"
        sanitized = ErrorSanitizer.sanitize_message(message)
        assert "MCPObjectController:" not in sanitized
        assert "Invalid field" in sanitized

    def test_complex_error_message(self):
        """Test sanitization of complex real-world error."""
        message = """Error executing tool search_records: Connection error: Failed to execute search_count on res.partner: Internal Server Error in MCPObjectController: Invalid field res.partner.invalid_field in leaf ('invalid_field', '=', True)
        File "/opt/odoo/addons/mcp_server/controllers/xmlrpc.py", line 123"""

        sanitized = ErrorSanitizer.sanitize_message(message)

        # Should not contain internal details
        assert "MCPObjectController" not in sanitized
        assert "/opt/odoo" not in sanitized
        assert "line 123" not in sanitized
        assert "search_count" not in sanitized

        # Should contain useful information
        assert "Invalid field" in sanitized


class TestNewlineFreeAndPsycopgTails:
    """Both bugs that let a 1600-character traceback reach a supervisor's phone.

    An Odoo XML-RPC fault string arrives with its newlines already collapsed to
    spaces, and psycopg2's exception names end in neither "Error" nor
    "Exception". Either one alone defeats the tail extractor; together they made
    every database-schema failure opaque.
    """

    REAL = (
        "File, in xmlrpc_2 response = self._xmlrpc(service) file, in _xmlrpc "
        "result = dispatch_rpc(service, method, params) file, in execute_kw "
        "return execute(db, uid, obj, method, *args, **kw or {}) "
        'File "<decorator-gen-503>", in create file, in _model_create_multi '
        "return create(self, [arg]) file, in _get_or_create_metric_capture "
        '"project_id": self.site_id.main_project_id.id, file, in execute '
        "res = self._obj.execute(query, params) "
        "psycopg2.errors.UndefinedColumn: column "
        "site_management_site.weather_display_type does not exist "
        'LINE 1: ..., "site_management_site"."display_weather_graph", "site_mana... ^'
    )

    def test_collapses_the_real_payload(self):
        assert ErrorSanitizer.extract_exception_tail(self.REAL) == (
            "UndefinedColumn: column site_management_site.weather_display_type "
            "does not exist"
        )

    def test_sql_echo_and_caret_are_dropped(self):
        tail = ErrorSanitizer.extract_exception_tail(self.REAL)
        assert "LINE 1" not in tail
        assert not tail.endswith("^")

    def test_dotted_psycopg_type_is_recognised_with_newlines_too(self):
        assert ErrorSanitizer.extract_exception_tail(
            'Traceback (most recent call last):\n  File "x.py", line 1\n'
            "psycopg2.errors.NotNullViolation: null value in column \"name\""
        ) == 'NotNullViolation: null value in column "name"'

    def test_last_exception_wins_when_several_appear(self):
        # Frames can quote an earlier exception; the one the traceback ended on
        # is the cause.
        assert ErrorSanitizer.extract_exception_tail(
            "file, in dispatch_rpc raise ValueError: inner "
            "file, in execute_kw psycopg2.errors.UndefinedTable: relation x does not exist"
        ) == "UndefinedTable: relation x does not exist"

    def test_call_frames_are_not_mistaken_for_exceptions(self):
        # odoo.api.call_kw is a dotted path too — lowercase after the last dot is
        # what keeps the dotted branch from matching it.
        assert ErrorSanitizer.extract_exception_tail(
            "file, in dispatch_rpc\nfile, in odoo.api.call_kw: recs, method"
        ) is None

    def test_non_traceback_text_still_passes_through(self):
        assert ErrorSanitizer.extract_exception_tail("Invalid field 'date' in request") is None
