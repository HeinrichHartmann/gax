"""Unit tests for filter module -- no API calls needed."""

from gax.filter import (
    api_to_criteria,
    criteria_to_api,
    api_to_action,
    action_to_api,
    criteria_hash,
    generate_filter_name,
    compute_changes,
    format_diff_summary,
    parse_filters_file,
    format_filters_file,
    FilterHeader,
)


# =============================================================================
# Serialize/deserialize round-trip: criteria
# =============================================================================


SAMPLE_API_CRITERIA = {
    "from": "alice@example.com",
    "to": "bob@example.com",
    "subject": "Invoice",
    "query": "has:attachment",
    "hasAttachment": True,
}


class TestCriteriaRoundTrip:
    """api_to_criteria and criteria_to_api should round-trip cleanly."""

    def test_basic_fields(self):
        local = api_to_criteria(SAMPLE_API_CRITERIA)
        assert local["from"] == "alice@example.com"
        assert local["to"] == "bob@example.com"
        assert local["subject"] == "Invoice"
        assert local["query"] == "has:attachment"
        assert local["hasAttachment"] is True

    def test_round_trip(self):
        local = api_to_criteria(SAMPLE_API_CRITERIA)
        rebuilt = criteria_to_api(local)
        assert rebuilt == SAMPLE_API_CRITERIA

    def test_empty_criteria(self):
        local = api_to_criteria({})
        assert local == {}
        rebuilt = criteria_to_api(local)
        assert rebuilt == {}

    def test_ignores_unknown_keys(self):
        api = {"from": "x@y.com", "unknownField": "ignored"}
        local = api_to_criteria(api)
        assert "unknownField" not in local
        assert local["from"] == "x@y.com"


# =============================================================================
# Serialize/deserialize round-trip: action
# =============================================================================


LABEL_ID_TO_NAME = {
    "Label_1": "Projects",
    "Label_2": "Archive/Old",
}

LABEL_NAME_TO_ID = {v: k for k, v in LABEL_ID_TO_NAME.items()}


class TestActionRoundTrip:
    """api_to_action and action_to_api should round-trip cleanly."""

    def test_label_add(self):
        api = {"addLabelIds": ["Label_1"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["label"] == "Projects"

        rebuilt = action_to_api(local, LABEL_NAME_TO_ID)
        assert rebuilt["addLabelIds"] == ["Label_1"]

    def test_multiple_labels(self):
        api = {"addLabelIds": ["Label_1", "Label_2"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["label"] == ["Projects", "Archive/Old"]

    def test_archive(self):
        api = {"removeLabelIds": ["INBOX"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["archive"] is True

        rebuilt = action_to_api(local, LABEL_NAME_TO_ID)
        assert "INBOX" in rebuilt["removeLabelIds"]

    def test_mark_read(self):
        api = {"removeLabelIds": ["UNREAD"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["markRead"] is True

        rebuilt = action_to_api(local, LABEL_NAME_TO_ID)
        assert "UNREAD" in rebuilt["removeLabelIds"]

    def test_star(self):
        api = {"addLabelIds": ["STARRED"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["star"] is True

        rebuilt = action_to_api(local, LABEL_NAME_TO_ID)
        assert "STARRED" in rebuilt["addLabelIds"]

    def test_trash(self):
        api = {"addLabelIds": ["TRASH"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["trash"] is True

    def test_important(self):
        api = {"addLabelIds": ["IMPORTANT"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["important"] is True

    def test_never_important(self):
        api = {"removeLabelIds": ["IMPORTANT"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["neverImportant"] is True

    def test_never_spam(self):
        api = {"removeLabelIds": ["SPAM"]}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["neverSpam"] is True

    def test_forward(self):
        api = {"forward": "backup@example.com"}
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["forward"] == "backup@example.com"

        rebuilt = action_to_api(local, LABEL_NAME_TO_ID)
        assert rebuilt["forward"] == "backup@example.com"

    def test_empty_action(self):
        local = api_to_action({}, {})
        assert local == {}

        rebuilt = action_to_api(local, {})
        assert rebuilt == {}

    def test_category(self):
        local = {"category": "social"}
        rebuilt = action_to_api(local, LABEL_NAME_TO_ID)
        assert "CATEGORY_SOCIAL" in rebuilt["addLabelIds"]

    def test_category_already_prefixed(self):
        local = {"category": "CATEGORY_UPDATES"}
        rebuilt = action_to_api(local, LABEL_NAME_TO_ID)
        assert "CATEGORY_UPDATES" in rebuilt["addLabelIds"]

    def test_combined_action(self):
        """Archive + label + mark read in one filter."""
        api = {
            "addLabelIds": ["Label_1"],
            "removeLabelIds": ["INBOX", "UNREAD"],
        }
        local = api_to_action(api, LABEL_ID_TO_NAME)
        assert local["label"] == "Projects"
        assert local["archive"] is True
        assert local["markRead"] is True


# =============================================================================
# Comparison / diff
# =============================================================================


class TestComputeChanges:

    def test_no_changes(self):
        desired = [{"name": "f1", "criteria": {"from": "a@b.com"}, "action": {"archive": True}}]
        current = [{
            "id": "id1",
            "criteria": {"from": "a@b.com"},
            "action": {"removeLabelIds": ["INBOX"]},
        }]
        changes = compute_changes(desired, current, {})
        assert changes["create"] == []
        assert changes["update"] == []
        assert changes["delete"] == []

    def test_create_new_filter(self):
        desired = [{"name": "new", "criteria": {"from": "new@x.com"}, "action": {}}]
        current = []
        changes = compute_changes(desired, current, {})
        assert len(changes["create"]) == 1
        assert changes["create"][0]["name"] == "new"

    def test_delete_removed_filter(self):
        desired = []
        current = [{"id": "id1", "criteria": {"from": "old@x.com"}, "action": {}}]
        changes = compute_changes(desired, current, {})
        assert len(changes["delete"]) == 1
        assert changes["delete"][0]["id"] == "id1"

    def test_update_changed_action(self):
        desired = [{"name": "f1", "criteria": {"from": "a@b.com"}, "action": {"star": True}}]
        current = [{
            "id": "id1",
            "criteria": {"from": "a@b.com"},
            "action": {"removeLabelIds": ["INBOX"]},
        }]
        changes = compute_changes(desired, current, {})
        assert len(changes["update"]) == 1
        assert changes["update"][0]["id"] == "id1"

    def test_mixed_changes(self):
        desired = [
            {"name": "keep", "criteria": {"from": "keep@x.com"}, "action": {"archive": True}},
            {"name": "new", "criteria": {"from": "new@x.com"}, "action": {}},
        ]
        current = [
            {"id": "id1", "criteria": {"from": "keep@x.com"}, "action": {"removeLabelIds": ["INBOX"]}},
            {"id": "id2", "criteria": {"from": "delete@x.com"}, "action": {}},
        ]
        changes = compute_changes(desired, current, {})
        assert len(changes["create"]) == 1
        assert len(changes["update"]) == 0
        assert len(changes["delete"]) == 1


class TestFormatDiffSummary:

    def test_empty_changes(self):
        changes = {"create": [], "update": [], "delete": []}
        assert format_diff_summary(changes) == ""

    def test_creates(self):
        changes = {
            "create": [{"name": "from:alice@x.com"}],
            "update": [],
            "delete": [],
        }
        summary = format_diff_summary(changes)
        assert "Create: 1" in summary
        assert "+ from:alice@x.com" in summary

    def test_updates(self):
        changes = {
            "create": [],
            "update": [{"name": "from:bob@x.com"}],
            "delete": [],
        }
        summary = format_diff_summary(changes)
        assert "Update: 1" in summary
        assert "~ from:bob@x.com" in summary

    def test_deletes(self):
        changes = {
            "create": [],
            "update": [],
            "delete": [{"criteria": {"from": "old@x.com"}}],
        }
        summary = format_diff_summary(changes)
        assert "Delete: 1" in summary
        assert "- from:old@x.com" in summary


# =============================================================================
# Helper functions
# =============================================================================


class TestHelpers:

    def test_criteria_hash_deterministic(self):
        c = {"from": "a@b.com", "subject": "test"}
        assert criteria_hash(c) == criteria_hash(c)

    def test_criteria_hash_order_independent(self):
        c1 = {"from": "a@b.com", "subject": "test"}
        c2 = {"subject": "test", "from": "a@b.com"}
        assert criteria_hash(c1) == criteria_hash(c2)

    def test_criteria_hash_different_for_different_criteria(self):
        c1 = {"from": "a@b.com"}
        c2 = {"from": "x@y.com"}
        assert criteria_hash(c1) != criteria_hash(c2)

    def test_generate_filter_name_from(self):
        assert generate_filter_name({"from": "a@b.com"}) == "from:a@b.com"

    def test_generate_filter_name_combined(self):
        name = generate_filter_name({"from": "a@b.com", "subject": "test"})
        assert "from:a@b.com" in name
        assert "subject:test" in name

    def test_generate_filter_name_empty(self):
        assert generate_filter_name({}) == "filter"

    def test_generate_filter_name_attachment(self):
        name = generate_filter_name({"hasAttachment": True})
        assert "has:attachment" in name


# =============================================================================
# File format
# =============================================================================


class TestFileFormat:

    def test_header_round_trip(self, tmp_path):
        header = FilterHeader(pulled="2026-01-01T00:00:00Z")
        filters = [
            {"name": "from:alice@x.com", "criteria": {"from": "alice@x.com"}, "action": {"archive": True}},
        ]
        content = format_filters_file(header, filters)

        path = tmp_path / "test.filters.gax.md"
        path.write_text(content, encoding="utf-8")

        parsed_header, parsed_filters = parse_filters_file(path)
        assert parsed_header.pulled == "2026-01-01T00:00:00Z"
        assert len(parsed_filters) == 1
        assert parsed_filters[0]["name"] == "from:alice@x.com"
        assert parsed_filters[0]["criteria"]["from"] == "alice@x.com"
        assert parsed_filters[0]["action"]["archive"] is True

    def test_empty_filters(self, tmp_path):
        header = FilterHeader(pulled="2026-01-01T00:00:00Z")
        content = format_filters_file(header, [])

        path = tmp_path / "empty.gax.md"
        path.write_text(content, encoding="utf-8")

        parsed_header, parsed_filters = parse_filters_file(path)
        assert parsed_filters == []

    def test_comment_lines_skipped(self, tmp_path):
        content = "# Comment line\n# Another comment\n---\npulled: '2026-01-01'\ntype: gax/filters\n---\n- name: test\n"
        path = tmp_path / "commented.gax.md"
        path.write_text(content, encoding="utf-8")

        _, parsed_filters = parse_filters_file(path)
        assert len(parsed_filters) == 1
        assert parsed_filters[0]["name"] == "test"
