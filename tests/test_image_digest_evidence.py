import io
import stat
import zipfile

import pytest

from scripts.image_digest_evidence import MAX_ARCHIVE_BYTES, validate_image_digest_artifact

REGISTRY = "example.azurecr.io"
COMMIT = "b" * 40
DIGEST = f"sha256:{'a' * 64}"


def evidence():
    return (
        f"Image: {REGISTRY}/agentic-gitops-staging@{DIGEST}\n"
        f"Commit: {COMMIT}\nRun ID: 123\nRun attempt: 2\n"
    )


def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, contents in entries:
            zipped.writestr(name, contents)
    return buffer.getvalue()


def validate(data, **overrides):
    expected = {"registry": REGISTRY, "commit": COMMIT, "run_id": 123, "attempt": 2}
    return validate_image_digest_artifact(data, **(expected | overrides))


def test_accepts_exact_evidence():
    result = validate(archive([("image-digest.txt", evidence())]))
    assert result.repository == f"{REGISTRY}/agentic-gitops-staging"
    assert result.digest == DIGEST


@pytest.mark.parametrize(
    "overrides",
    [
        {"registry": "other.azurecr.io"},
        {"commit": "c" * 40},
        {"run_id": 124},
        {"attempt": 1},
        {"run_id": True},
    ],
)
def test_rejects_mismatched_or_invalid_expected_identity(overrides):
    with pytest.raises(ValueError):
        validate(archive([("image-digest.txt", evidence())]), **overrides)


@pytest.mark.parametrize(
    "text",
    [
        evidence().replace(DIGEST, "sha256:bad"),
        evidence().replace("Run attempt: 2", "Run attempt: 02"),
        evidence() + "Extra: value\n",
        evidence().replace("\n", "\r\n"),
        b"\xff",
        "x" * 4097,
    ],
)
def test_rejects_untrusted_evidence(text):
    with pytest.raises(ValueError):
        validate(archive([("image-digest.txt", text)]))


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [("../image-digest.txt", evidence())],
        [("image-digest.txt", evidence()), ("extra", "")],
    ],
)
def test_rejects_wrong_archive_layout(entries):
    with pytest.raises(ValueError):
        validate(archive(entries))


def test_rejects_symlink_entry():
    entry = zipfile.ZipInfo("image-digest.txt")
    entry.create_system = 3
    entry.external_attr = (stat.S_IFLNK | 0o777) << 16
    with pytest.raises(ValueError):
        validate(archive([(entry, evidence())]))


def test_rejects_duplicate_entries():
    with pytest.warns(UserWarning):
        data = archive([("image-digest.txt", evidence()), ("image-digest.txt", evidence())])
    with pytest.raises(ValueError):
        validate(data)


def test_rejects_corrupt_or_oversized_archive():
    for data in (b"not a zip file", b"x" * (MAX_ARCHIVE_BYTES + 1)):
        with pytest.raises(ValueError):
            validate(data)
