"""execute_api's api_name loses a leaked "functions." namespace before validation."""

from services.conversation.tools.orchestrator import strip_api_namespace


def test_prefix_dropped_for_execute_api():
    args = {"api_name": "functions.clinic_check_availability", "inputs": {"department": "ORTH"}}
    assert strip_api_namespace("execute_api", args) == {
        "api_name": "clinic_check_availability", "inputs": {"department": "ORTH"},
    }


def test_untouched_otherwise():
    clean = {"api_name": "clinic_check_availability", "inputs": {}}
    assert strip_api_namespace("execute_api", clean) is clean
    other = {"query": "functions.x"}
    assert strip_api_namespace("search_knowledge", other) is other
    assert strip_api_namespace("execute_api", "not-a-dict") == "not-a-dict"
