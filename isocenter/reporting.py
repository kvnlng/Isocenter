import datetime
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol


@dataclass
class ComplianceReport:
    """
    Data Transfer Object holding all information required for a compliance report.

    Attributes:
        generated_at (datetime.datetime): Timestamp of generation.
        isocenter_version (str): Version of the system.
        project_name (str): Name of the session/project.
        privacy_profile (str): The profile that was actually applied, or a
            statement that none was.
        deid_method (str): Factual description of what was configured --
            profile, tag-rule count, pixel-rule count. Never the name of a
            compliance standard: whether the output satisfies one is a
            determination for the data steward, and this report carries a
            DPO signature line beneath whatever it claims.
        total_patients (int): Total patients processed.
        total_studies (int): Total studies processed.
        total_series (int): Total series processed.
        total_instances (int): Total instances processed.
        instances_written (int, optional): How many instances the last
            export in this session actually wrote. None when no export
            has run here -- which is not the same as zero, so the row is
            omitted rather than rendered as one.
        instances_requested (int, optional): How many that export asked
            for. The pair is what stops `total_instances` -- a count of
            the object graph -- from reading as a count of delivered
            files (#181).
        audit_summary (Dict[str, int]): Aggregated counts of audit actions.
        exceptions (list): List of error tuples (timestamp, action, details).
        data_losses (list): Elements present in the source and not in the
            output, as (timestamp, entity_uid, details, loss_scope).
        validation_status (str): Overall status -- `PENDING` until a
            report is generated, then `PASS` or `REVIEW_REQUIRED`.
            Nothing emits `FAIL`: this report describes a run, and a run
            that fails raises rather than grading itself.
        validation_issues (int): Count of validation issues found.
        verification_details (str): Additional context on verification.
    """
    generated_at: datetime.datetime = field(default_factory=datetime.datetime.now)
    isocenter_version: str = "Unknown"
    project_name: str = "Isocenter Session"

    # Configuration / Context
    privacy_profile: str = "Unknown"
    # Not a compliance claim. This used to default to "Safe Harbor (Basic
    # Profile)" and no caller ever assigned it, so every report asserted
    # HIPAA Safe Harbor -- including for a session whose PHI scan covers
    # the six shipped default tags. DicomSession.generate_report now
    # always passes a description derived from the live configuration.
    deid_method: str = "Not recorded"

    # Cohort Statistics
    total_patients: int = 0
    total_studies: int = 0
    total_series: int = 0
    total_instances: int = 0

    # Export Delivery
    #
    # `total_instances` counts the graph, and used to be the only count
    # in the Executive Summary: a run that wrote none of its three
    # instances reported "Total Instances | 3" beneath a PASS. These two
    # say what was written, and are None when this session has not
    # exported (#181).
    instances_written: Optional[int] = None
    instances_requested: Optional[int] = None

    # Audit / Processing Statistics
    # e.g., {'ANONYMIZE_METADATA': 1200, 'REDACT_PIXELS': 50, 'EXPORT': 1200}
    audit_summary: Dict[str, int] = field(default_factory=dict)

    # Exceptions & Errors
    # List of "ERROR" or "WARNING" logs: (timestamp, action, details)
    exceptions: list = field(default_factory=list)

    # Data Loss
    # List of "DATA_LOSS" logs: (timestamp, entity_uid, details,
    # loss_scope), where loss_scope is PRIVATE, STANDARD, or None for a
    # row written before the column existed.
    #
    # Its own field rather than more `exceptions` (#146): nothing
    # failed, and an overlay dropped from an ordinary image belongs
    # nowhere near a section headed "Exceptions & Errors". The grade is
    # answered per row, on `loss_scope`, which is what lets the report
    # say *which* losses were graded rather than grading them alike.
    data_losses: list = field(default_factory=list)

    # Validation
    # PENDING until graded; then PASS or REVIEW_REQUIRED. No FAIL --
    # see the class docstring.
    validation_status: str = "PENDING"
    validation_issues: int = 0
    verification_details: str = ""


class ReportRenderer(Protocol):
    """Protocol for a report renderer."""

    def render(self, report: ComplianceReport, output_path: str) -> None:
        """
        Renders the report to the specified output path.

        Args:
            report (ComplianceReport): The report object to render.
            output_path (str): The file path to write to.
        """
        ...


class MarkdownRenderer:
    """Renders the ComplianceReport as a formatted Markdown document."""

    def render(self, report: ComplianceReport, output_path: str) -> None:
        """
        Renders the report as a Markdown file.

        Includes an Executive Summary, Processing Audit table, Exceptions log,
        and Verification details.

        Args:
            report (ComplianceReport): The data to render.
            output_path (str): Path to write the .md file.
        """
        # Rendered only when an export ran in this session. An absent
        # row says "not answered here"; a zero would say "nothing was
        # written", and the report cannot tell those apart for a session
        # that never exported (#181).
        written_row = ""
        if report.instances_written is not None:
            written_row = (
                f"| Instances Written | {report.instances_written} of "
                f"{report.instances_requested} requested |\n")

        md_content = f"""# Compliance Report

**Generated At:** {report.generated_at.strftime('%Y-%m-%d %H:%M:%S')}
**Project:** {report.project_name}
**System Version:** Isocenter v{report.isocenter_version}

## 1. Executive Summary

| Metric | Value |
| :--- | :--- |
| **Validation Status** | **{report.validation_status}** |
| Total Patients | {report.total_patients} |
| Total Instances | {report.total_instances} |
{written_row}| Privacy Profile | {report.privacy_profile} |
| De-ID Method | {report.deid_method} |

## 2. Processing Audit

The following actions were recorded in the secure audit trail:

| Action Type | Count |
| :--- | :--- |
"""
        # Add audit rows
        if report.audit_summary:
            for action, count in sorted(report.audit_summary.items()):
                md_content += f"| {action} | {count} |\n"
        else:
            md_content += "| *No audit logs found* | 0 |\n"

        # Data Loss Section
        #
        # Ahead of Exceptions because it is a property of the data that
        # was written, not of the run: nothing failed, and a reader
        # skimming for problems would otherwise stop at "no exceptions".
        # Each row names the element and its VR -- "a tag was dropped" is
        # not actionable; the VR is what says whether it was a four-byte
        # serial number or a megabyte of vendor telemetry (#146).
        if report.data_losses:
            md_content += "\n## 3. Data Loss\n\n> [!WARNING]\n> Elements below were present in the source and are **not** in the exported data:\n\n"
            md_content += "| Timestamp | Instance | Element | Scope |\n| :--- | :--- | :--- | :--- |\n"
            for loss in report.data_losses:
                # (timestamp, entity_uid, details, loss_scope)
                #
                # The scope is printed because it decides the grade: a
                # report that reads REVIEW_REQUIRED with no way to see
                # which row caused it is the same defect as the bare
                # `DATA_LOSS: 3` this section replaced. "unrecorded" is
                # a row from a store older than the column, not a third
                # kind of loss.
                scope = loss[3] or "unrecorded"
                md_content += f"| {loss[0]} | {loss[1]} | {loss[2]} | {scope} |\n"
        else:
            md_content += "\n## 3. Data Loss\n\n*No data loss was recorded.*\n"

        # Exceptions Section
        if report.exceptions:
            md_content += f"\n## 4. Exceptions & Errors\n\n> [!WARNING]\n> The following issues were encountered during processing:\n\n"
            md_content += "| Timestamp | Action | Details |\n| :--- | :--- | :--- |\n"
            for exc in report.exceptions:
                # exc is expected to be (timestamp, action, details)
                # truncate details if too long?
                md_content += f"| {exc[0]} | {exc[1]} | {exc[2]} |\n"
        else:
            md_content += f"\n## 4. Exceptions & Errors\n\n*No exceptions or errors were recorded.*\n"

        md_content += f"""
## 5. Validation & Verification

*   **Identified Issues:** {report.validation_issues}
*   **Methodology:** {report.deid_method}. Metadata was remediated according to the tag policy in force; pixel data was scanned against the configured machine redaction zones. This section records what the tooling was configured to do and what it logged doing -- whether the result meets HIPAA Safe Harbor, a Limited Data Set, or any other standard is a determination for the data steward, not for Isocenter.
*   **Verification Details:** {report.verification_details if report.verification_details else "Standard automated checks performed."}

---
**Data Protection Officer Signature:**

__________________________________________________
*(Date)*
"""

        with open(output_path, "w", encoding="utf-8") as f:
            f.write(md_content)


def get_renderer(format_type: str) -> ReportRenderer:
    if format_type.lower() in ["md", "markdown"]:
        return MarkdownRenderer()
    raise ValueError(f"Unsupported report format: {format_type}")
