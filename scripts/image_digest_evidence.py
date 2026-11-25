"""Validate a downloaded GitHub image-digest artifact without extracting it."""

import io
import re
import stat
import zipfile
import zlib
from dataclasses import dataclass

MAX_ARCHIVE_BYTES = 64 * 1024
MAX_EVIDENCE_BYTES = 4096

_COMMIT = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_REGISTRY = re.compile(r"[a-zA-Z0-9-]+\.azurecr\.io")
_EVIDENCE = re.compile(
    r"Image: (?P<registry>[a-zA-Z0-9-]+\.azurecr\.io)/agentic-gitops-staging@"
    r"(?P<digest>sha256:[a-f0-9]{64})\n"
    r"Commit: (?P<commit>(?:[a-f0-9]{40}|[a-f0-9]{64}))\n"
    r"Run ID: (?P<run_id>[1-9][0-9]*)\n"
    r"Run attempt: (?P<attempt>[1-9][0-9]*)\n"
)


@dataclass(frozen=True)
class ImageEvidence:
    repository: str
    digest: str


def validate_image_digest_artifact(
    archive: bytes, *, registry: str, commit: str, run_id: int, attempt: int
) -> ImageEvidence:
    """Validate artifact contents against independently established run metadata.

    The caller must authenticate and verify the GitHub run and publish job,
    bound the download itself, and validate the source and destination digests.
    """
    if (
        not _REGISTRY.fullmatch(registry)
        or not _COMMIT.fullmatch(commit)
        or type(run_id) is not int
        or run_id < 1
        or type(attempt) is not int
        or attempt < 1
    ):
        raise ValueError("Invalid expected image evidence identity")
    if len(archive) > MAX_ARCHIVE_BYTES:
        raise ValueError("Image evidence archive exceeds size limit")

    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            entries = zipped.infolist()
            if len(entries) != 1 or entries[0].filename != "image-digest.txt":
                raise ValueError("Image evidence archive must contain exactly image-digest.txt")
            entry = entries[0]
            file_type = stat.S_IFMT(entry.external_attr >> 16)
            if (
                entry.is_dir()
                or file_type not in (0, stat.S_IFREG)
                or entry.flag_bits & 1
                or entry.file_size > MAX_EVIDENCE_BYTES
            ):
                raise ValueError("Invalid image evidence archive entry")
            with zipped.open(entry) as evidence_file:
                content = evidence_file.read(MAX_EVIDENCE_BYTES + 1)
            if len(content) > MAX_EVIDENCE_BYTES:
                raise ValueError("Image evidence exceeds size limit")
        text = content.decode("utf-8")
    except (zipfile.BadZipFile, EOFError, OSError, RuntimeError, NotImplementedError, UnicodeDecodeError, zlib.error) as exc:
        raise ValueError("Invalid image evidence archive") from exc

    match = _EVIDENCE.fullmatch(text)
    if not match:
        raise ValueError("Invalid image evidence format")
    if (
        match["registry"] != registry
        or match["commit"] != commit
        or match["run_id"] != str(run_id)
        or match["attempt"] != str(attempt)
    ):
        raise ValueError("Image evidence does not match expected run")
    return ImageEvidence(
        repository=f"{registry}/agentic-gitops-staging",
        digest=match["digest"],
    )
