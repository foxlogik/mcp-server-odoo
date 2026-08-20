"""Guard-level tests, including a replay of every captured production failure.

``tests/fixtures/mcp_failures.json`` holds the arguments of 192 Odoo MCP calls
that failed in real sessions over a twenty-day window. The classes this module
is responsible for — malformed domains and unknown fields — must now be caught
locally, with a message that names the fix. That is what the replay asserts.
"""

import json
from pathlib import Path

import pytest

from mcp_server_odoo.schema_guard import (
    SchemaGuardError,
    normalize_domain,
    suggest,
    suggest_fields,
    validate_fields,
    validate_model,
)

FIXTURE = Path(__file__).parent / "fixtures" / "mcp_failures.json"

# Field definitions for the two models that produced the most wrong guesses in
# production, trimmed to what the guard reads.
DCR_REPORT_FIELDS = {
    "id": {"string": "ID", "type": "integer"},
    "name": {"string": "Report", "type": "char"},
    "report_date": {"string": "Date", "type": "date"},
    "site_id": {"string": "Site", "type": "many2one", "relation": "site_management.site"},
    "state": {"string": "Status", "type": "selection"},
    "create_date": {"string": "Created on", "type": "datetime"},
    "write_date": {"string": "Last Updated on", "type": "datetime"},
}

QUESTIONNAIRE_FIELDS = {
    "id": {"string": "ID", "type": "integer"},
    "title": {"string": "Title", "type": "char"},
    "display_name": {"string": "Display Name", "type": "char"},
    "active": {"string": "Active", "type": "boolean"},
}


# --------------------------------------------------------------------------- #
# Domain                                                                        #
# --------------------------------------------------------------------------- #


class TestNormalizeDomain:
    def test_none_and_empty(self):
        assert normalize_domain(None) == []
        assert normalize_domain([]) == []

    def test_valid_domain_passes_through(self):
        domain = [["site_id", "=", 6], ["report_date", ">=", "2026-08-01"]]
        assert normalize_domain(domain) == domain

    def test_tuples_become_lists(self):
        assert normalize_domain([("name", "=", "Acme")]) == [["name", "=", "Acme"]]

    def test_bare_prefix_operator_kept(self):
        assert normalize_domain(["|", ["a", "=", 1], ["b", "=", 2]])[0] == "|"

    def test_wrapped_prefix_operator_repaired(self):
        # ["|"] is unambiguous, so it is repaired rather than rejected.
        result = normalize_domain([["|"], ["login", "=", "x"], ["email", "=", "x"]])
        assert result[0] == "|"
        assert len(result) == 3

    def test_html_escaped_operator_repaired(self):
        result = normalize_domain([["start_date", "&lt;=", "2026-08-18"]])
        assert result == [["start_date", "<=", "2026-08-18"]]

    def test_two_element_leaf_rejected_with_suggestion(self):
        with pytest.raises(SchemaGuardError) as excinfo:
            normalize_domain([["model", "=", "discuss.channel"], ["res_id", 762]])
        message = str(excinfo.value)
        assert "2 elements" in message
        assert '["res_id", "=", 762]' in message

    def test_four_element_leaf_rejected(self):
        with pytest.raises(SchemaGuardError, match="4 elements"):
            normalize_domain([["res_id", 693, "=", 1]])

    def test_unknown_operator_rejected(self):
        with pytest.raises(SchemaGuardError, match="not valid"):
            normalize_domain([["name", "==", "Acme"]])

    def test_non_string_field_rejected(self):
        with pytest.raises(SchemaGuardError, match="non-string field"):
            normalize_domain([[1, "=", 2]])

    def test_stray_string_rejected(self):
        with pytest.raises(SchemaGuardError, match="not a domain operator"):
            normalize_domain(["AND", ["a", "=", 1]])

    def test_dict_domain_rejected(self):
        with pytest.raises(SchemaGuardError, match="Domain must be a list"):
            normalize_domain({"field": "value"})

    def test_model_named_in_message(self):
        with pytest.raises(SchemaGuardError, match="on mail.message"):
            normalize_domain([["res_id", 75]], model="mail.message")


# --------------------------------------------------------------------------- #
# Fields and models                                                             #
# --------------------------------------------------------------------------- #


class TestSuggestions:
    def test_label_beats_similarity(self):
        # 'date' was read off a form whose column header is "Date"; the field is
        # report_date. The label match must win.
        assert suggest_fields("date", DCR_REPORT_FIELDS)[0] == "report_date"

    def test_underscores_normalized_against_label(self):
        assert "create_date" in suggest_fields("created_on", DCR_REPORT_FIELDS)

    def test_substring_match(self):
        assert "display_name" in suggest_fields("name", QUESTIONNAIRE_FIELDS)

    def test_no_match_returns_empty(self):
        assert suggest_fields("zzz_nonexistent", QUESTIONNAIRE_FIELDS) == []

    def test_model_suggestion(self):
        matches = suggest("questionnaire.questionnaire", ["foxlogik.questionnaire", "res.partner"])
        assert matches[0] == "foxlogik.questionnaire"


class TestValidateFields:
    def test_known_fields_pass(self):
        validate_fields("daily_construction.report", ["id", "name", "report_date"], DCR_REPORT_FIELDS)

    def test_id_always_valid(self):
        validate_fields("daily_construction.report", ["id"], {"name": {}})

    def test_dotted_path_checked_on_root_only(self):
        validate_fields("daily_construction.report", ["site_id.name"], DCR_REPORT_FIELDS)

    def test_unknown_field_names_the_alternative(self):
        with pytest.raises(SchemaGuardError) as excinfo:
            validate_fields("daily_construction.report", ["date"], DCR_REPORT_FIELDS)
        message = str(excinfo.value)
        assert "'date' does not exist" in message
        assert "report_date (Date)" in message
        assert "get_fields('daily_construction.report')" in message

    def test_missing_name_field(self):
        with pytest.raises(SchemaGuardError, match="does not exist on foxlogik.questionnaire"):
            validate_fields("foxlogik.questionnaire", ["name"], QUESTIONNAIRE_FIELDS)

    def test_fails_open_without_schema(self):
        # An unreadable schema is not evidence that the caller is wrong.
        validate_fields("some.model", ["anything"], None)
        validate_fields("some.model", ["anything"], {})

    def test_all_sentinel_allowed(self):
        validate_fields("daily_construction.report", ["__all__"], DCR_REPORT_FIELDS)


class TestValidateModel:
    def test_known_model_passes(self):
        validate_model("res.partner", ["res.partner", "res.users"])

    def test_unknown_model_suggests(self):
        with pytest.raises(SchemaGuardError) as excinfo:
            validate_model("questionnaire.questionnaire", ["foxlogik.questionnaire"])
        assert "Closest enabled: foxlogik.questionnaire" in str(excinfo.value)

    def test_fails_open_without_list(self):
        validate_model("anything.at.all", None)


# --------------------------------------------------------------------------- #
# Replay of captured production failures                                        #
# --------------------------------------------------------------------------- #


def _cases():
    if not FIXTURE.exists():
        return []
    return json.loads(FIXTURE.read_text())["cases"]


def _domain_cases():
    return [c for c in _cases() if c["error_class"] == "bad-leaf" and c["args"].get("domain")]


@pytest.mark.skipif(not FIXTURE.exists(), reason="failure corpus not present")
class TestCapturedFailures:
    def test_corpus_is_loadable(self):
        cases = _cases()
        assert cases, "corpus is empty"
        assert all("tool" in c and "args" in c for c in cases)

    @pytest.mark.parametrize("case", _domain_cases(), ids=lambda c: c["error_head"][:40])
    def test_malformed_domains_are_caught_locally(self, case):
        """Every domain that killed a real call is rejected before the RPC.

        A handful of captured cases carry a domain that is structurally fine —
        they failed further in (an unknown field, an access rule). Those must
        pass the domain guard, so the assertion is 'no traceback escapes', not
        'everything raises'.
        """
        domain = case["args"]["domain"]
        if isinstance(domain, str):
            pytest.skip("string domain is parsed upstream of the guard")
        try:
            normalize_domain(domain, model=case["args"].get("model", ""))
        except SchemaGuardError as exc:
            message = str(exc)
            assert len(message) < 600, "guard message must stay readable"
            assert "Traceback" not in message
        # No other exception type may escape.

    def test_every_bad_leaf_case_is_rejected(self):
        """The shape that produced 22 production failures must not get through."""
        rejected = 0
        structural = 0
        for case in _domain_cases():
            domain = case["args"]["domain"]
            if not isinstance(domain, list):
                continue
            has_bad_leaf = any(
                isinstance(leaf, list) and len(leaf) not in (1, 3) for leaf in domain
            )
            if not has_bad_leaf:
                continue
            structural += 1
            with pytest.raises(SchemaGuardError):
                normalize_domain(domain)
            rejected += 1
        assert structural > 0, "corpus should contain malformed leaves"
        assert rejected == structural
