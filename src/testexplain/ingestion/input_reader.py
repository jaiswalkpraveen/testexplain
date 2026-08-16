import json
import re
import shutil
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from pydantic import BaseModel, Field, PrivateAttr

from testexplain.ingestion.playwright import parse_attempts
from testexplain.models import Attachment, FailedAttempt

MAX_MEMBERS = 2000
MAX_MEMBER_UNCOMPRESSED_SIZE = 100 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_SIZE = 500 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
COMPRESSION_RATIO_MIN_SIZE = 10 * 1024 * 1024


class InvalidBundleError(ValueError):
    """A ZIP bundle cannot be safely or meaningfully analyzed."""


class LoadedInput(BaseModel):
    attempts: list[FailedAttempt]
    artifact_dir: Path | None
    warnings: list[str] = Field(default_factory=list)
    allow_external_absolute_paths: bool = Field(default=False, exclude=True)
    _temporary_directory: tempfile.TemporaryDirectory[str] | None = PrivateAttr(
        default=None
    )

    def __enter__(self) -> "LoadedInput":
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def resolve_attachment_path(self, attachment: Attachment) -> Path | None:
        return resolve_attachment_path(
            self.artifact_dir,
            attachment,
            self.warnings,
            allow_external_absolute_paths=self.allow_external_absolute_paths,
        )


def _warning(attachment: Attachment, reason: str, warnings: list[str] | None) -> None:
    if warnings is not None:
        warnings.append(f"Attachment {attachment.name!r} {reason}")


def resolve_attachment_path(
    artifact_dir: Path | None,
    attachment: Attachment,
    warnings: list[str] | None = None,
    *,
    allow_external_absolute_paths: bool = False,
) -> Path | None:
    if not attachment.path:
        if attachment.body_b64 is None:
            _warning(attachment, "has no path", warnings)
        return None
    if artifact_dir is None:
        _warning(attachment, "cannot be resolved without an artifact directory", warnings)
        return None

    base = artifact_dir.resolve()
    supplied = Path(attachment.path)
    candidate = supplied.resolve() if supplied.is_absolute() else (base / supplied).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        if not (
            supplied.is_absolute()
            and allow_external_absolute_paths
            and candidate.exists()
        ):
            _warning(attachment, "resolves outside the artifact directory", warnings)
            return None

    if not candidate.exists():
        _warning(attachment, f"path {attachment.path!r} does not exist", warnings)
        return None
    return candidate


def _safe_member_path(name: str) -> Path:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith("/")
        or path.is_absolute()
        or ".." in path.parts
        or re.match(r"^[A-Za-z]:", normalized)
    ):
        raise ValueError(f"ZIP contains unsafe path: {name!r}")
    return Path(*path.parts)


def _validate_archive(infos: list[zipfile.ZipInfo]) -> None:
    if len(infos) > MAX_MEMBERS:
        raise ValueError(f"ZIP exceeds member limit of {MAX_MEMBERS}")

    total_size = sum(info.file_size for info in infos)
    if total_size > MAX_TOTAL_UNCOMPRESSED_SIZE:
        raise ValueError(
            "ZIP exceeds uncompressed size limit of "
            f"{MAX_TOTAL_UNCOMPRESSED_SIZE} bytes"
        )

    seen_paths: set[str] = set()
    for info in infos:
        member_path = _safe_member_path(info.filename)
        path_key = member_path.as_posix().casefold()
        if path_key in seen_paths:
            raise ValueError(f"ZIP contains duplicate member path: {info.filename!r}")
        seen_paths.add(path_key)
        if info.file_size > MAX_MEMBER_UNCOMPRESSED_SIZE:
            raise ValueError(
                f"ZIP member {info.filename!r} exceeds member uncompressed size limit "
                f"of {MAX_MEMBER_UNCOMPRESSED_SIZE} bytes"
            )
        if info.is_dir() or info.file_size <= COMPRESSION_RATIO_MIN_SIZE:
            continue
        ratio = info.file_size / max(info.compress_size, 1)
        if ratio > MAX_COMPRESSION_RATIO:
            raise ValueError(
                f"ZIP member {info.filename!r} exceeds compression ratio limit of "
                f"{MAX_COMPRESSION_RATIO}"
            )


def _load_zip(path: Path) -> LoadedInput:
    temporary_directory = tempfile.TemporaryDirectory(prefix="testexplain-")
    extraction_dir = Path(temporary_directory.name).resolve()
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            _validate_archive(infos)
            report_paths: list[Path] = []
            for info in infos:
                if info.is_dir():
                    continue
                relative_path = _safe_member_path(info.filename)
                destination = extraction_dir / relative_path
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("wb") as target:
                    shutil.copyfileobj(source, target)
                if _is_playwright_report(destination):
                    report_paths.append(destination)

        if len(report_paths) != 1:
            raise ValueError(
                "Bundle ZIP must contain exactly one Playwright JSON report; "
                f"found {len(report_paths)}"
            )
        loaded = LoadedInput(
            attempts=parse_attempts(report_paths[0]),
            artifact_dir=extraction_dir,
        )
        loaded._temporary_directory = temporary_directory
        return loaded
    except Exception:
        temporary_directory.cleanup()
        raise


def _read_json_object(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _is_playwright_report(path: Path) -> bool:
    value = _read_json_object(path)
    return bool(
        value is not None
        and isinstance(value.get("config"), dict)
        and isinstance(value.get("suites"), list)
        and "errors" in value
        and "stats" in value
    )


def _is_json_object(path: Path) -> bool:
    return _read_json_object(path) is not None


def load_input(path: str | Path) -> LoadedInput:
    input_path = Path(path)
    if not input_path.exists():
        raise FileNotFoundError(f"Input file does not exist: {input_path}")
    if input_path.suffix.lower() == ".json" or _is_json_object(input_path):
        return LoadedInput(
            attempts=parse_attempts(input_path),
            artifact_dir=input_path.parent.resolve(),
            allow_external_absolute_paths=True,
        )
    if zipfile.is_zipfile(input_path):
        try:
            return _load_zip(input_path)
        except ValueError as exc:
            raise InvalidBundleError(str(exc)) from exc
    raise InvalidBundleError(f"Input must be a JSON report or ZIP bundle: {input_path}")
