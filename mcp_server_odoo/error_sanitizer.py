"""Error message sanitizer for Odoo MCP Server.

This module provides utilities to sanitize error messages before they are
returned to users, removing internal implementation details while maintaining
useful information for debugging.
"""

import os
import re
from typing import Any, Dict, Optional

# A traceback that has had its file paths and line numbers stripped is not a
# sanitized error — it is the same forty frames with the useful parts removed.
# Production callers received an average of 1,400 characters of `dispatch_rpc` /
# `execute_kw` frames with the one sentence that named the problem at the very
# end, and acted on the frames. Detect that shape and keep only the tail.
_TRACEBACK_MARKERS = (
    "Traceback (most recent call last)",
    "dispatch_rpc",
    "execute_kw",
    'File "',
)

# An exception type is either a bare name with a conventional suffix
# (ValidationError, AccessError) or a dotted path ending in a CamelCase class —
# psycopg2 raises psycopg2.errors.UndefinedColumn, whose name ends in neither
# "Error" nor "Exception", so a suffix list alone silently misses every database
# schema failure. The dotted branch requires an uppercase initial after the last
# dot, which is what keeps it from matching call frames like odoo.api.call_kw.
_EXCEPTION_TYPE = (
    r"(?:[A-Za-z_][\w]*\.)+[A-Z][\w]*"
    r"|[A-Za-z_][\w.]*(?:Error|Exception|Warning|Fault|Exit|Interrupt)"
)

_EXCEPTION_LINE = re.compile(
    r"^(?P<type>" + _EXCEPTION_TYPE + r")"
    r"\s*:\s*(?P<message>.*)$"
)

# For tracebacks that arrive with their newlines already collapsed to spaces —
# the shape Odoo's XML-RPC fault strings actually have by the time they reach a
# caller. Without this the line scan sees one line beginning "File, in xmlrpc_2"
# and reports no exception at all.
#
# Only the TYPE and its colon are matched here, never the message: a pattern
# ending in ".*$" is greedy, so its first match swallows every later exception
# line and finditer then finds nothing after it — which silently returns the
# EARLIEST exception, the opposite of what a traceback tail means. Positions
# first, then read the message from the last one.
_EXCEPTION_ANYWHERE = re.compile(r"(?:" + _EXCEPTION_TYPE + r")\s*:\s*")

# psycopg2 appends the offending SQL and a caret to its message. Useful in a
# server log, noise in a one-line answer, and it hides the real sentence.
_SQL_ECHO = re.compile(r"\s*LINE\s+\d+:.*$", re.DOTALL)

_DEBUG_TRACES_ENV = "ODOO_MCP_DEBUG_TRACES"


class ErrorSanitizer:
    """Sanitizes error messages to remove internal implementation details."""

    # Patterns to detect and remove
    PATTERNS_TO_REMOVE = [
        # File paths
        (r'(File|file)\s*"[^"]+\.py"', "file"),
        (r"(/[^/\s]+)+/[^/\s]+\.py", ""),
        # Line numbers
        (r",?\s*line\s+\d+", ""),
        # Python internals
        (r"Traceback \(most recent call last\):", ""),
        (r'^\s*File "[^"]+", line \d+.*$', ""),
        # Module paths
        (r"mcp_server_odoo\.[a-zA-Z_\.]+:", ""),
        (r"odoo\.[a-zA-Z_\.]+:", ""),
        # Class names
        (r"<class \'[^\']+\'>", ""),
        (r"MCPObjectController:", ""),
        (r"OdooConnectionError:", ""),
        # Memory addresses and object references
        (r"\s+at\s+0x[0-9a-fA-F]+", ""),
        (r"Object at\s+0x[0-9a-fA-F]+", "Object"),
        # Stack traces
        (r"in\s+<[^>]+>", ""),
        (r"in\s+[a-zA-Z_]+\(\)", ""),
    ]

    # Specific error message mappings
    ERROR_MAPPINGS = {
        # Field errors
        r"Invalid field .+ in leaf": "Invalid field '{}' in search criteria",
        r"Field\s+(\w+)\s+does not exist": "Field '{}' does not exist on this model",
        r"Unknown field .+ in domain": "Unknown field '{}' in search criteria",
        # Model errors
        r"Model .+ does not exist": "Model '{}' is not available",
        r"Access denied on model": "You don't have permission to access this model",
        # Connection errors
        r"Failed to execute .+ on .+: .+": "Operation failed: {}",
        r"Connection refused": "Cannot connect to Odoo server",
        r"Operation timeout after \d+ seconds": "Request timed out",
        # Authentication errors
        r"Invalid API key": "Authentication failed: Invalid API key",
        r"Access denied": "Permission denied for this operation",
        # Record errors
        r"Record not found": "The requested record does not exist",
        r"Record .+ does not exist": "Record ID {} not found",
        # Domain errors
        r"Invalid domain": "Invalid search criteria format",
        r"Malformed domain": "Search criteria is not properly formatted",
    }

    @classmethod
    def sanitize_message(cls, message: str) -> str:
        """Sanitize an error message by removing internal details.

        Args:
            message: The original error message

        Returns:
            Sanitized error message safe for user consumption
        """
        if not message:
            return "An error occurred"

        # Collapse a traceback to its final exception line before anything else.
        # The tail still goes through the mappings below, so an "Invalid field"
        # buried in a stack is reported exactly as one arriving on its own.
        tail = cls.extract_exception_tail(message)
        message = tail if tail else message

        sanitized = message

        # First, try to match against known error patterns
        for pattern, replacement in cls.ERROR_MAPPINGS.items():
            match = re.search(pattern, message, re.IGNORECASE)
            if match:
                # Extract any captured groups (like field names)
                if match.groups():
                    return replacement.format(*match.groups())
                elif "{}" in replacement:
                    # Try to extract relevant info from the message
                    extracted = cls._extract_relevant_info(message, pattern)
                    if extracted:
                        return replacement.format(extracted)
                return replacement

        # Remove patterns that expose internals
        for pattern, replacement in cls.PATTERNS_TO_REMOVE:
            sanitized = re.sub(pattern, replacement, sanitized, flags=re.MULTILINE)

        # Clean up multiple spaces and newlines
        sanitized = re.sub(r"\s+", " ", sanitized).strip()

        # If the message is now too generic or empty, provide a better default
        if not sanitized or sanitized == "file" or len(sanitized) < 10:
            return "An error occurred while processing your request"

        # Ensure the message starts with a capital letter
        if sanitized and sanitized[0].islower():
            sanitized = sanitized[0].upper() + sanitized[1:]

        return sanitized

    @classmethod
    def extract_exception_tail(cls, message: str) -> Optional[str]:
        """Return the final ``ExcType: message`` line of a traceback, if any.

        Returns None when *message* is not traceback-shaped, or when
        ODOO_MCP_DEBUG_TRACES is set — full frames stay available for debugging,
        they just stop being the default answer to a caller.
        """
        if not message or os.environ.get(_DEBUG_TRACES_ENV):
            return None
        if not any(marker in message for marker in _TRACEBACK_MARKERS):
            return None

        for line in reversed([ln.strip() for ln in message.splitlines() if ln.strip()]):
            match = _EXCEPTION_LINE.match(line)
            if match:
                return cls._format_exception_tail(match)

        # Newline-free traceback: keep the LAST type position, which is the
        # exception the frames above it led to, and read from there.
        starts = [m.start() for m in _EXCEPTION_ANYWHERE.finditer(message)]
        if not starts:
            return None
        match = _EXCEPTION_LINE.match(message[starts[-1]:].strip())
        return cls._format_exception_tail(match) if match else None

    @staticmethod
    def _format_exception_tail(match: "re.Match") -> str:
        """Render one matched exception line as ``ShortType: message``."""
        exception_type = match.group("type").rsplit(".", 1)[-1]
        detail = _SQL_ECHO.sub("", match.group("message")).strip().rstrip("^").strip()
        return f"{exception_type}: {detail}" if detail else exception_type

    @classmethod
    def _extract_relevant_info(cls, message: str, pattern: str) -> Optional[str]:
        """Extract relevant information from error message.

        Args:
            message: The error message
            pattern: The pattern that matched

        Returns:
            Extracted information or None
        """
        # Try to extract field names - look for the actual field name after model prefix
        if "field" in pattern.lower():
            # First try to find field after model name (e.g., res.partner.field_name)
            full_field_match = re.search(
                r"[a-zA-Z_][a-zA-Z0-9_]*\.[a-zA-Z_][a-zA-Z0-9_.]*\.([a-zA-Z_][a-zA-Z0-9_]*)",
                message,
            )
            if full_field_match:
                return full_field_match.group(1)
            # Otherwise try to find any quoted field name
            field_match = re.search(r"['\"]([a-zA-Z_][a-zA-Z0-9_]*)['\"]", message)
            if field_match:
                return field_match.group(1)

        # Try to extract model names
        model_match = re.search(
            r"model\s+['\"]?([a-zA-Z_][a-zA-Z0-9_.]*)['\"]?", message, re.IGNORECASE
        )
        if model_match and "model" in pattern.lower():
            return model_match.group(1)

        # Try to extract record IDs
        id_match = re.search(r"ID\s+(\d+)", message, re.IGNORECASE)
        if id_match and "record" in pattern.lower():
            return id_match.group(1)

        return None

    @classmethod
    def sanitize_error_details(cls, details: Dict[str, Any]) -> Dict[str, Any]:
        """Sanitize error details dictionary.

        Args:
            details: Original error details

        Returns:
            Sanitized error details
        """
        if not details:
            return {}

        sanitized = {}

        # Only include safe fields
        safe_fields = {"model", "operation", "record_id", "field", "domain"}

        for key, value in details.items():
            if key in safe_fields:
                sanitized[key] = value
            elif key == "error_type":
                # Map internal error types to user-friendly categories
                sanitized["category"] = cls._map_error_type(value)

        # Remove any traceback information
        sanitized.pop("traceback", None)

        return sanitized

    @classmethod
    def _map_error_type(cls, error_type: str) -> str:
        """Map internal error type to user-friendly category.

        Args:
            error_type: Internal Python error type name

        Returns:
            User-friendly error category
        """
        mappings = {
            "ValidationError": "validation_error",
            "ValueError": "invalid_input",
            "TypeError": "invalid_type",
            "KeyError": "not_found",
            "NotFoundError": "not_found",
            "PermissionError": "permission_denied",
            "AccessControlError": "access_denied",
            "AuthenticationError": "authentication_failed",
            "ConnectionError": "connection_error",
            "OdooConnectionError": "connection_error",
            "TimeoutError": "timeout",
            "SystemError": "internal_error",
        }

        return mappings.get(error_type, "error")

    @classmethod
    def sanitize_xmlrpc_fault(cls, fault_string: str) -> str:
        """Sanitize XML-RPC fault messages from Odoo.

        Args:
            fault_string: Raw fault string from XML-RPC

        Returns:
            Sanitized error message
        """
        # Common Odoo XML-RPC faults
        if "Only system administrators may use MCP user impersonation" in fault_string:
            # The bare policy statement gave the caller no exit, and it was
            # retried verbatim twelve times in one production window.
            return (
                "This connection may not impersonate another user: user_id is "
                "restricted to system administrators. Retry without user_id to run "
                "as the service account."
            )
        if "Access Denied" in fault_string:
            return "Access denied: Invalid credentials or insufficient permissions"
        elif "Object does not exist" in fault_string:
            return "The requested resource does not exist"
        elif "Invalid field" in fault_string:
            # Try to extract field name
            field_match = re.search(
                r"field\s+['\"]?([a-zA-Z_][a-zA-Z0-9_\.]*)['\"]?", fault_string, re.IGNORECASE
            )
            if field_match:
                return f"Invalid field '{field_match.group(1)}' in request"
            return "Invalid field in request"
        elif "MissingError" in fault_string:
            return "The requested record was not found"
        elif "ValidationError" in fault_string:
            return "Validation error: Please check your input"
        elif "UserError" in fault_string:
            # Try to extract the user-friendly part of UserError
            user_msg_match = re.search(r'UserError\(["\']([^"\']+)["\']', fault_string)
            if user_msg_match:
                return user_msg_match.group(1)
            return "Operation failed due to business rule violation"
        else:
            # Generic sanitization
            return cls.sanitize_message(fault_string)
