"""Independent tester regressions derived from the shared specification and corrected pack."""

import base64
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tester.phase4 import checks, policy
from tester.phase4.package import compare_packages, differences, parse_xml
from tester.phase4.workbooks import phase4_fixtures
from tester.store import TesterStore as Store
from tester.suite.checks import CheckFailed, SuiteContext
from tester.suite.manifest import SuiteConfigurationError, load_suite, restore_suite, snapshot_suite
from tester.suite.registry import names
from tests.support.fake_phase4 import FakePhase4
from tests.unit.test_phase4_tester import (
    MANIFEST,
    _decisions,
    _effects,
    _rewrite,
    _sub,
)


@pytest.mark.parametrize(
    "rgb,tint,expected",
    [
        ((51, 102, 153), -0.5, (26, 51, 77)),
        ((51, 102, 153), 0.5, (140, 179, 217)),
        ((1, 1, 1), -0.5, (1, 1, 1)),
        ((5, 5, 5), -0.5, (3, 3, 3)),
    ],
)
def test_tint_mathematical_half_up(rgb, tint, expected):
    assert policy.apply_tint(rgb, tint) == expected


def test_tint_does_not_push_genuinely_below_half_values_upward():
    assert policy.apply_tint((5, 5, 5), -0.50000000000001) == (2, 2, 2)


def test_comparator_respects_inherited_preserved_text_whitespace():
    source = parse_xml(b'<is xml:space="preserve"><t> leading text </t></is>')
    output = parse_xml(b"<is><t> leading text </t></is>")
    assert differences(source, output)


@pytest.mark.parametrize("attribute", ['cm="1"', 'ph="1"', 'custom="retained"'])
def test_link_comparator_rejects_loss_of_unselected_cell_metadata(attribute):
    original = phase4_fixtures()["link_supported.xlsx"]
    source = _rewrite(
        original, _sub("xl/worksheets/sheet1.xml", '<c r="A1"', f'<c r="A1" {attribute}')
    )
    candidate = FakePhase4().edit(original, _decisions("link_supported", "link"))
    assert compare_packages(source, candidate, _effects("link_supported", "link"))


def test_enabling_header_does_not_authorize_adding_filter():
    source = phase4_fixtures()["header_disabled_table.xlsx"]
    output = FakePhase4().edit(source, _decisions("header_disabled_table", "header_table"))
    output = _rewrite(
        output,
        _sub("xl/tables/table1.xml", "</tableColumns>", '</tableColumns><autoFilter ref="A1:B4"/>'),
    )
    assert compare_packages(source, output, _effects("header_disabled_table", "header_table"))


def test_phase4_snapshot_restores_original_bytes_without_fixture_files(tmp_path: Path):
    suite = load_suite(MANIFEST, names())
    snapshot = json.loads(snapshot_suite(suite, MANIFEST))
    snapshot["fixture_root"] = str(tmp_path / "deleted-or-replaced-by-new-release")
    restored = restore_suite(json.dumps(snapshot))
    assert restored.data == suite.data


def test_fixture_snapshot_is_durable_immutable_and_separate_from_progress(tmp_path: Path):
    suite = load_suite(MANIFEST, names())
    accepted = snapshot_suite(suite, MANIFEST)
    path = tmp_path / "tester.sqlite3"
    store = Store(path)
    store.create("run", "operator", "http://127.0.0.1:8000", datetime.now(UTC), plan=accepted)
    store.finish_setup("run", True)
    store.begin_run("run", "operator")
    store.save_progress("run", [], {})
    reopened = Store(path)
    assert reopened.get("run").plan == accepted
    assert restore_suite(reopened.get("run").plan).data == suite.data
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT plan FROM tester_sessions").fetchone()[0] is None
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE tester_plan_snapshots SET plan='{}'")


def test_restore_rejects_corrupted_snapshotted_bytes():
    suite = load_suite(MANIFEST, names())
    snapshot = json.loads(snapshot_suite(suite, MANIFEST))
    snapshot["fixture_bytes"]["clean_all_six"] = base64.b64encode(b"replacement").decode("ascii")
    with pytest.raises(SuiteConfigurationError):
        restore_suite(json.dumps(snapshot))


def test_arbitrary_limitation_text_cannot_substitute_for_required_scope_facts():
    suite = load_suite(MANIFEST, names())
    fixture = suite.accepted("clean_all_six")
    with pytest.raises(CheckFailed):
        checks.check_limitations({"limitations": ["There are limitations."]}, fixture, "audit")


def test_equivalent_omitted_color_display_assumptions_are_accepted():
    from tester.phase4.disclosures import missing_facts

    fact = "Missing fill is treated as white and missing font color as black (display assumption)."
    assert (
        missing_facts(
            [fact],
            ["A cell without fill is displayed as white, and text without font color as black."],
        )
        == []
    )


def test_conditional_scope_disclosure_cannot_be_replaced_by_unrelated_limitations():
    from tester.phase4.disclosures import missing_facts

    fact = (
        "Visual conditional formatting was not evaluated; affected ranges require "
        "manual contrast review."
    )
    assert missing_facts([fact], ["Only single cells are not inferred as tables."]) == [fact]
    assert (
        missing_facts(
            [fact], ["Conditional formatting is not rendered; review every state manually."]
        )
        == []
    )


def test_rename_negative_oracles_distinguish_case_only_and_source_collision():
    suite = load_suite(MANIFEST, names())
    ctx = SuiteContext(client=None, suite=suite, session_id="test")
    requests = checks._requests(ctx, "mixed_decisions", "audit", "name_invalid")
    expected = {label: code for label, _, _, code in requests}
    assert expected["name 'sheet1'"] == "UNSUPPORTED_CORRECTION"
    assert expected["name 'notes'"] == "CONFLICTING_CORRECTIONS"
