"""Tests for policy document front-matter parsing and section splitting."""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from app.rag.documents import (
    REQUIRED_FRONT_MATTER_FIELDS,
    PolicyDocument,
    load_policy_documents,
    parse_policy_document,
)

VALID_DOC = """---
document_id: sample-doc
title: Sample Policy
document_type: sample_policy
department: ALL
audience: student
policy_version: v1
effective_from: 2024-01-01
visibility: public
---

# Sample Policy

## First Section
This is the first section body.

## Second Section
This is the second section body, with more than one sentence to read.
"""


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_parse_valid_document(tmp_path: Path) -> None:
    path = _write(tmp_path, "sample.md", VALID_DOC)
    doc = parse_policy_document(path)

    assert isinstance(doc, PolicyDocument)
    assert doc.document_id == "sample-doc"
    assert doc.title == "Sample Policy"
    assert doc.document_type == "sample_policy"
    assert doc.department == "ALL"
    assert doc.audience == "student"
    assert doc.policy_version == "v1"
    assert doc.effective_from == date(2024, 1, 1)
    assert doc.effective_to is None
    assert doc.visibility == "public"
    assert doc.source_path == "data/policies/sample.md"
    assert [s.heading for s in doc.sections] == ["First Section", "Second Section"]
    assert doc.sections[0].order == 0
    assert doc.sections[1].order == 1


def test_parse_document_with_effective_to(tmp_path: Path) -> None:
    text = VALID_DOC.replace("effective_from: 2024-01-01", "effective_from: 2024-01-01\neffective_to: 2024-12-31")
    path = _write(tmp_path, "sample.md", text)
    doc = parse_policy_document(path)
    assert doc.effective_to == date(2024, 12, 31)


def test_missing_front_matter_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "bad.md", "# No front matter\n\n## Section\nBody text.\n")
    with pytest.raises(ValueError, match="missing YAML front matter"):
        parse_policy_document(path)


@pytest.mark.parametrize("field_name", REQUIRED_FRONT_MATTER_FIELDS)
def test_missing_required_field_rejected(tmp_path: Path, field_name: str) -> None:
    lines = VALID_DOC.splitlines(keepends=True)
    filtered = [line for line in lines if not line.startswith(f"{field_name}:")]
    path = _write(tmp_path, "bad.md", "".join(filtered))
    with pytest.raises(ValueError, match="missing required front matter fields"):
        parse_policy_document(path)


def test_unknown_visibility_rejected(tmp_path: Path) -> None:
    text = VALID_DOC.replace("visibility: public", "visibility: everyone")
    path = _write(tmp_path, "bad.md", text)
    with pytest.raises(ValueError, match="unknown visibility"):
        parse_policy_document(path)


def test_unknown_audience_rejected(tmp_path: Path) -> None:
    text = VALID_DOC.replace("audience: student", "audience: alumni")
    path = _write(tmp_path, "bad.md", text)
    with pytest.raises(ValueError, match="unknown audience"):
        parse_policy_document(path)


def test_document_with_no_sections_rejected(tmp_path: Path) -> None:
    text = (
        "---\n"
        "document_id: sample-doc\n"
        "title: Sample Policy\n"
        "document_type: sample_policy\n"
        "department: ALL\n"
        "audience: student\n"
        "policy_version: v1\n"
        "effective_from: 2024-01-01\n"
        "visibility: public\n"
        "---\n\n"
        "# Sample Policy\n"
        "Just prose, no ## headings.\n"
    )
    path = _write(tmp_path, "bad.md", text)
    with pytest.raises(ValueError, match="no '##' sections"):
        parse_policy_document(path)


def test_blank_section_body_is_skipped(tmp_path: Path) -> None:
    text = VALID_DOC.replace(
        "## Second Section\nThis is the second section body, with more than one sentence to read.\n",
        "## Empty Section\n\n## Second Section\nThis is the second section body.\n",
    )
    path = _write(tmp_path, "sample.md", text)
    doc = parse_policy_document(path)
    headings = [s.heading for s in doc.sections]
    assert "Empty Section" not in headings
    assert "Second Section" in headings


# ---------------------------------------------------------------------------
# Real corpus (data/policies/)
# ---------------------------------------------------------------------------


def test_load_real_policy_corpus(policy_dir: Path) -> None:
    documents = load_policy_documents(policy_dir)
    assert len(documents) >= 11

    doc_ids = {d.document_id for d in documents}
    for expected_id in [
        "attendance-policy-v1",
        "attendance-policy-v2",
        "exam-regulations",
        "placement-policy",
        "internship-policy",
        "event-policy",
        "grievance-sla-policy",
        "hostel-policy",
        "scholarship-policy",
        "library-policy",
        "student-handbook",
    ]:
        assert expected_id in doc_ids


def test_attendance_policy_versions_are_distinct(policy_dir: Path) -> None:
    documents = {d.document_id: d for d in load_policy_documents(policy_dir)}
    v1 = documents["attendance-policy-v1"]
    v2 = documents["attendance-policy-v2"]

    assert v1.document_type == v2.document_type == "attendance_policy"
    assert v1.policy_version == "v1"
    assert v2.policy_version == "v2"
    assert v1.effective_to is not None and v1.effective_to < v2.effective_from
    assert v2.effective_to is None


def test_admin_only_document_present(policy_dir: Path) -> None:
    documents = {d.document_id: d for d in load_policy_documents(policy_dir)}
    sop = documents["internal-case-escalation-sop"]
    assert sop.visibility == "admin_only"
    assert sop.audience == "staff"
