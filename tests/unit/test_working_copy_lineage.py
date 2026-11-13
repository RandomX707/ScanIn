from packages.api.compare import lineage_classes


def test_persisted_working_copy_classification_survives_missing_and_later_audit_events():
    versions = [
        {"version_id": "signed", "parent_version_id": None, "kind": "original", "lineage_class": "original"},
        {"version_id": "copy", "parent_version_id": "signed", "kind": "derived", "lineage_class": "unsigned_working_copy"},
        {"version_id": "child", "parent_version_id": "copy", "kind": "derived", "lineage_class": "unsigned_working_copy"},
        {"version_id": "branch", "parent_version_id": "copy", "kind": "derived", "lineage_class": "unsigned_working_copy"},
        {"version_id": "native", "parent_version_id": "signed", "kind": "derived", "lineage_class": "standard"},
    ]
    # The newest 500-event page no longer contains the creation record, even after restart.
    classes = lineage_classes(versions, [])
    assert classes == {"signed": "original", "copy": "unsigned_working_copy", "child": "unsigned_working_copy",
                       "branch": "unsigned_working_copy", "native": "standard"}


def test_unknown_legacy_lineage_is_not_downgraded_to_an_ordinary_derived_version():
    versions = [{"version_id": "legacy", "parent_version_id": None, "kind": "derived"}]
    assert lineage_classes(versions, [{"payload": {"unsigned_working_copy": True, "version_id": "legacy"}}]) == {"legacy": "unknown"}


def test_lineage_classification_does_not_change_signature_verification_facts():
    versions = [{"version_id": "copy", "parent_version_id": "signed", "kind": "derived",
                 "lineage_class": "unsigned_working_copy", "verification_outcome": "VERIFIED"}]
    assert lineage_classes(versions, []) == {"copy": "unsigned_working_copy"}
    assert versions[0]["verification_outcome"] == "VERIFIED"
