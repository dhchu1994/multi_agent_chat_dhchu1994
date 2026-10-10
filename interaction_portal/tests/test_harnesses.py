"""Tests for Harness A and Harness B programmatic verification."""

import pytest
from app.harnesses.harness_a import run_harness_a
from app.harnesses.harness_b import run_harness_b


def test_harness_a_verification(tmp_path):
    report_file = tmp_path / "harness_a_test.json"
    passed, report = run_harness_a(client_id="C1", output_path=str(report_file))
    assert passed is True
    assert report["summary"]["failed"] == 0
    assert report["summary"]["pass_rate"] == 1.0


def test_harness_b_single_run(tmp_path):
    report_file = tmp_path / "harness_b_test.json"
    passed, report = run_harness_b(
        condition="EVA-TASK",
        client_id="C2",
        output_path=str(report_file),
    )
    assert passed is True
    assert report["overall_verdict"] == "PASSED"


def test_harness_b_ten_conditions(tmp_path):
    report_file = tmp_path / "harness_b_matrix.json"
    passed, report = run_harness_b(
        all_conditions=True,
        client_id="C2",
        output_path=str(report_file),
    )
    assert passed is True
    assert report["overall_verdict"] == "PASSED"
    assert report["total_runs"] == 10
