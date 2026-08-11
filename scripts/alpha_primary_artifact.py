import hashlib
import io
import json
import os
import urllib.parse
import urllib.request
import zipfile
from pathlib import PurePosixPath


DEFAULT_ARTIFACT_NAME = "realtime-snapshot"
DEFAULT_SNAPSHOT_PATH = "snapshot.json"
DEFAULT_MANIFEST_PATH = "alpha-artifact-manifest.json"
MAX_ARTIFACT_PAGES = 100


class PrimaryArtifactError(RuntimeError):
    def __init__(self, code, message):
        self.code = str(code)
        super().__init__(f"{self.code}: {message}")


def _require_positive_int(value, field):
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_INVALID_IDENTITY", f"{field} must be an integer"
        ) from exc
    if parsed <= 0:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_INVALID_IDENTITY", f"{field} must be positive"
        )
    return parsed


def _request_json(url, token):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "looklookbigA-alpha-primary-artifact",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def list_exact_run_artifacts(repository, token, workflow_run_id):
    run_id = _require_positive_int(workflow_run_id, "workflow_run_id")
    repository = str(repository or "")
    if "/" not in repository:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_INVALID_IDENTITY", "repository must be owner/name"
        )
    api = (
        f"https://api.github.com/repos/{repository}/actions/runs/"
        f"{run_id}/artifacts"
    )
    artifacts = []
    for page in range(1, MAX_ARTIFACT_PAGES + 1):
        query = urllib.parse.urlencode({"per_page": 100, "page": page})
        payload = _request_json(f"{api}?{query}", token)
        batch = payload.get("artifacts") or []
        if not isinstance(batch, list):
            raise PrimaryArtifactError(
                "PRIMARY_ARTIFACT_API_INVALID",
                "GitHub exact-run artifact response did not contain an artifact list",
            )
        artifacts.extend(batch)
        if len(batch) < 100:
            return artifacts
    raise PrimaryArtifactError(
        "PRIMARY_ARTIFACT_API_AMBIGUOUS",
        f"artifact listing exceeded {MAX_ARTIFACT_PAGES} pages for exact run {run_id}",
    )


def resolve_exact_primary_from_listing(
    artifacts,
    workflow_run_id,
    expected_name=DEFAULT_ARTIFACT_NAME,
):
    run_id = _require_positive_int(workflow_run_id, "workflow_run_id")
    expected_name = str(expected_name or "")
    if not expected_name:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_INVALID_IDENTITY", "expected artifact name is required"
        )
    matches = [
        item
        for item in (artifacts or [])
        if str(item.get("name") or "") == expected_name
    ]
    if not matches:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_MISSING",
            f"exact run {run_id} has no artifact named {expected_name}",
        )
    if len(matches) != 1:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_AMBIGUOUS",
            f"exact run {run_id} has {len(matches)} artifacts named {expected_name}",
        )

    artifact = matches[0]
    if artifact.get("expired") is True:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_EXPIRED",
            f"artifact {artifact.get('id')} for exact run {run_id} is expired",
        )
    artifact_id = _require_positive_int(artifact.get("id"), "artifact_id")
    workflow = artifact.get("workflow_run") or {}
    observed_run_id = workflow.get("id")
    if observed_run_id not in (None, "") and int(observed_run_id) != run_id:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_WRONG_RUN",
            f"artifact {artifact_id} belongs to workflow run {observed_run_id}, "
            f"expected {run_id}",
        )
    api_url = str(artifact.get("url") or "")
    download_url = str(artifact.get("archive_download_url") or "")
    if not api_url or not download_url:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_INVALID_IDENTITY",
            f"artifact {artifact_id} is missing exact API/download locators",
        )

    return {
        "workflow_run_id": run_id,
        "artifact_id": artifact_id,
        "artifact_name": expected_name,
        "artifact_api_url": api_url,
        "archive_download_url": download_url,
        "artifact_size_bytes": artifact.get("size_in_bytes"),
        "artifact_digest": artifact.get("digest"),
        "artifact_digest_source": "GITHUB_ACTIONS_ARTIFACT_API",
        "artifact_created_at": artifact.get("created_at"),
        "artifact_updated_at": artifact.get("updated_at"),
        "artifact_expires_at": artifact.get("expires_at"),
        "artifact_expired": False,
    }


def resolve_exact_primary(
    repository,
    token,
    workflow_run_id,
    expected_name=DEFAULT_ARTIFACT_NAME,
):
    artifacts = list_exact_run_artifacts(repository, token, workflow_run_id)
    return resolve_exact_primary_from_listing(
        artifacts, workflow_run_id, expected_name
    )


def _safe_archive_member(name):
    value = str(name or "")
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_ARCHIVE_UNSAFE",
            f"unsafe archive member path: {value!r}",
        )
    return value


def _read_zip_members(archive_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(archive_bytes), "r") as archive:
            result = {}
            for info in archive.infolist():
                if info.is_dir():
                    continue
                name = _safe_archive_member(info.filename)
                if name in result:
                    raise PrimaryArtifactError(
                        "PRIMARY_ARTIFACT_ARCHIVE_AMBIGUOUS",
                        f"duplicate archive member: {name}",
                    )
                result[name] = archive.read(info)
            return result
    except PrimaryArtifactError:
        raise
    except zipfile.BadZipFile as exc:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_ARCHIVE_INVALID",
            "downloaded primary artifact is not a valid ZIP",
        ) from exc


def validate_downloaded_primary_archive(
    artifact_identity,
    archive_bytes,
    manifest_path=DEFAULT_MANIFEST_PATH,
):
    run_id = _require_positive_int(
        artifact_identity.get("workflow_run_id"), "workflow_run_id"
    )
    expected_name = str(artifact_identity.get("artifact_name") or "")
    if not expected_name:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_INVALID_IDENTITY",
            "artifact identity has no artifact_name",
        )
    members = _read_zip_members(archive_bytes)
    manifest_path = _safe_archive_member(manifest_path)
    if manifest_path not in members:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_MANIFEST_MISSING",
            f"downloaded artifact {artifact_identity.get('artifact_id')} has no "
            f"{manifest_path}",
        )
    try:
        manifest = json.loads(members[manifest_path].decode("utf-8"))
    except Exception as exc:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_MANIFEST_INVALID",
            "primary artifact manifest is not valid UTF-8 JSON",
        ) from exc

    if manifest.get("kind") != "looklookbigA-primary-artifact-manifest":
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_MANIFEST_INVALID",
            "unexpected primary artifact manifest kind",
        )
    if int(manifest.get("primary_artifact_count") or 0) != 1:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_MANIFEST_INVALID",
            "manifest must declare exactly one primary artifact",
        )
    primary = manifest.get("primary") or {}
    if str(primary.get("artifact_name") or "") != expected_name:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_MANIFEST_IDENTITY_MISMATCH",
            "manifest artifact_name does not match resolved GitHub artifact",
        )
    try:
        manifest_run_id = int(primary.get("workflow_run_id"))
    except (TypeError, ValueError) as exc:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_MANIFEST_IDENTITY_MISMATCH",
            "manifest workflow_run_id is missing or invalid",
        ) from exc
    if manifest_run_id != run_id:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_MANIFEST_IDENTITY_MISMATCH",
            f"manifest workflow_run_id {manifest_run_id} does not match exact run "
            f"{run_id}",
        )
    snapshot_path = _safe_archive_member(
        primary.get("snapshot_path") or DEFAULT_SNAPSHOT_PATH
    )
    if snapshot_path not in members:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_SNAPSHOT_MISSING",
            f"downloaded artifact has no manifest-declared snapshot {snapshot_path}",
        )
    snapshot = members[snapshot_path]
    digest_algorithm = str(primary.get("digest_algorithm") or "").upper()
    if digest_algorithm != "SHA-256":
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_DIGEST_UNSUPPORTED",
            f"unsupported snapshot digest algorithm {digest_algorithm!r}",
        )
    expected_digest = str(primary.get("digest") or "").lower()
    actual_digest = hashlib.sha256(snapshot).hexdigest()
    if expected_digest != actual_digest:
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_DIGEST_MISMATCH",
            f"snapshot digest mismatch expected={expected_digest} "
            f"actual={actual_digest}",
        )
    expected_size = primary.get("size_bytes")
    if expected_size not in (None, "") and int(expected_size) != len(snapshot):
        raise PrimaryArtifactError(
            "PRIMARY_ARTIFACT_SIZE_MISMATCH",
            f"snapshot size mismatch expected={expected_size} actual={len(snapshot)}",
        )
    for required in ("producing_commit_sha", "produced_at"):
        if not str(primary.get(required) or ""):
            raise PrimaryArtifactError(
                "PRIMARY_ARTIFACT_MANIFEST_INVALID",
                f"manifest primary.{required} is required",
            )

    verified = dict(artifact_identity)
    verified.update(
        {
            "snapshot_path": snapshot_path,
            "snapshot_size_bytes": len(snapshot),
            "snapshot_digest": actual_digest,
            "snapshot_digest_algorithm": "SHA-256",
            "snapshot_digest_source": "DOWNLOADED_CONTENT_VERIFIED",
            "producing_ref": primary.get("producing_ref"),
            "producing_ref_name": primary.get("producing_ref_name"),
            "producing_commit_sha": primary.get("producing_commit_sha"),
            "workflow_ref": primary.get("workflow_ref"),
            "workflow_sha": primary.get("workflow_sha"),
            "produced_at": primary.get("produced_at"),
            "manifest_verified": True,
        }
    )
    return verified


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Resolve and validate the exact primary looklookAlpha artifact for "
            "one workflow run."
        )
    )
    sub = parser.add_subparsers(dest="command", required=True)

    resolve = sub.add_parser("resolve")
    resolve.add_argument(
        "--repository", default=os.environ.get("GITHUB_REPOSITORY")
    )
    resolve.add_argument("--token-env", default="GITHUB_TOKEN")
    resolve.add_argument("--run-id", required=True, type=int)
    resolve.add_argument("--artifact-name", default=DEFAULT_ARTIFACT_NAME)

    verify = sub.add_parser("verify-archive")
    verify.add_argument("--identity-json", required=True)
    verify.add_argument("--archive", required=True)

    args = parser.parse_args(argv)
    if args.command == "resolve":
        token = os.environ.get(args.token_env)
        if not args.repository:
            raise PrimaryArtifactError(
                "PRIMARY_ARTIFACT_INVALID_IDENTITY", "repository is required"
            )
        if not token:
            raise PrimaryArtifactError(
                "PRIMARY_ARTIFACT_AUTH_MISSING",
                f"missing token in environment variable {args.token_env}",
            )
        print(
            json.dumps(
                resolve_exact_primary(
                    args.repository, token, args.run_id, args.artifact_name
                )
            )
        )
    else:
        identity = json.loads(args.identity_json)
        with open(args.archive, "rb") as handle:
            verified = validate_downloaded_primary_archive(
                identity, handle.read()
            )
        print(json.dumps(verified))


if __name__ == "__main__":
    main()
