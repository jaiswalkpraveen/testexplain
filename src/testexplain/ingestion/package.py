import copy
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from testexplain.ingestion.input_reader import resolve_attachment_path
from testexplain.ingestion.playwright import FAILED_STATUSES, parse_attempts
from testexplain.models import Attachment


@dataclass
class BundleResult:
    output_path: Path
    artifact_count: int
    warnings: list[str]


def _read_native_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Report file does not exist: {path}")
    try:
        report = json.loads(path.read_text())
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Input must be a native Playwright JSON report") from error

    if not (
        isinstance(report, dict)
        and isinstance(report.get("config"), dict)
        and isinstance(report.get("suites"), list)
        and "errors" in report
        and "stats" in report
    ):
        raise ValueError("Input must be a native Playwright JSON report")

    # Nested structures are only validated by the attempt parser, so a report
    # that cannot be parsed is rejected here instead of producing a bundle that
    # `analyze` would later fail to load.
    try:
        parse_attempts(path)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ValueError("Input must be a native Playwright JSON report") from error
    return report


def _attachment(value: dict[str, Any]) -> Attachment:
    path = value.get("path")
    body = value.get("body", value.get("base64"))
    return Attachment(
        name=value.get("name") if isinstance(value.get("name"), str) else "",
        content_type=(
            value.get("contentType")
            if isinstance(value.get("contentType"), str)
            else ""
        ),
        path=path if isinstance(path, str) else None,
        body_b64=body if isinstance(body, str) else None,
    )


def _unexpected_results(report: dict[str, Any]):
    def visit(suites: list[Any]):
        for suite in suites:
            if not isinstance(suite, dict):
                continue
            specs = suite.get("specs", [])
            if isinstance(specs, list):
                for spec in specs:
                    if not isinstance(spec, dict):
                        continue
                    tests = spec.get("tests", [])
                    if not isinstance(tests, list):
                        continue
                    for test in tests:
                        if (
                            not isinstance(test, dict)
                            or test.get("expectedStatus") == "failed"
                        ):
                            continue

                        expected_status = test.get("expectedStatus", "passed")
                        expected_status = (
                            expected_status
                            if isinstance(expected_status, str)
                            else "passed"
                        )
                        results = test.get("results", [])
                        if not isinstance(results, list):
                            continue
                        for result in results:
                            if (
                                isinstance(result, dict)
                                and result.get("status") in FAILED_STATUSES
                                and result.get("status") != expected_status
                            ):
                                yield result
            children = suite.get("suites", [])
            if isinstance(children, list):
                yield from visit(children)

    return visit(report["suites"])


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _safe_member_name(name: str) -> str:
    # POSIX basenames may legally contain backslashes, which the bundle reader
    # treats as separators, so both separator styles are flattened.
    return name.replace("/", "_").replace("\\", "_") or "artifact"


def create_bundle(report_path: str | Path, output_path: str | Path) -> BundleResult:
    source_path = Path(report_path)
    destination = Path(output_path)
    if source_path.resolve() == destination.resolve():
        raise ValueError("Bundle output path must differ from the report path")
    report = _read_native_report(source_path)
    rewritten_report = copy.deepcopy(report)
    warnings: list[str] = []
    source_members: dict[Path, str] = {}
    artifact_sources: list[tuple[Path, str]] = []
    unexpected_results = list(_unexpected_results(rewritten_report))

    for result in unexpected_results:
        attachments = result.get("attachments", [])
        if not isinstance(attachments, list):
            continue
        for value in attachments:
            if not isinstance(value, dict):
                continue
            attachment = _attachment(value)
            # Inline bodies already travel inside report.json, so the raw attachment
            # dict is left untouched and no source file is copied.
            if attachment.body_b64 is not None:
                warnings.append(
                    f"Attachment {attachment.name!r} has inline body and was not copied"
                )
                continue
            resolved = resolve_attachment_path(
                source_path.parent.resolve(),
                attachment,
                warnings,
                allow_external_absolute_paths=True,
            )
            if resolved is None:
                continue
            if not resolved.is_file():
                warnings.append(
                    f"Attachment {attachment.name!r} path {attachment.path!r} is not a file"
                )
                continue
            member = source_members.get(resolved)
            if member is None:
                safe_name = _safe_member_name(resolved.name)
                member = f"artifacts/{len(artifact_sources):03d}-{safe_name}"
                source_members[resolved] = member
                artifact_sources.append((resolved, member))
            value["path"] = member

    if not unexpected_results:
        warnings.append(
            "Report contains no unexpected failed or timedOut attempts to package"
        )

    if destination.resolve() in source_members:
        raise ValueError("Bundle output path must not overwrite an attachment artifact")

    report_bytes = json.dumps(
        rewritten_report,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr(_zip_info("report.json"), report_bytes)
        for artifact_path, member in artifact_sources:
            archive.writestr(_zip_info(member), artifact_path.read_bytes())

    return BundleResult(
        output_path=destination,
        artifact_count=len(artifact_sources),
        warnings=warnings,
    )
