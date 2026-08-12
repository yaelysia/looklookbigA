import argparse
import json
import os
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import alpha_primary_artifact
import history_continuity


DEFAULT_ARTIFACT_NAME = "market-history-state"


class HistoryArtifactError(RuntimeError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download_archive(url, token, timeout=30):
    parsed = urllib.parse.urlparse(str(url or ""))
    if parsed.scheme != "https" or parsed.hostname != "api.github.com":
        raise HistoryArtifactError("history artifact download must start at api.github.com HTTPS")
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "looklookbigA-history-artifact",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307, 308):
            raise
        location = exc.headers.get("Location")
    redirected = urllib.parse.urlparse(str(location or ""))
    if redirected.scheme != "https" or not redirected.hostname:
        raise HistoryArtifactError("GitHub artifact redirect was not an absolute HTTPS URL")
    # The signed redirect URL is deliberately fetched without the GitHub token.
    redirected_request = urllib.request.Request(
        location, headers={"User-Agent": "looklookbigA-history-artifact"}
    )
    with urllib.request.urlopen(redirected_request, timeout=timeout) as response:
        return response.read()


def inspect_archive(archive_bytes, expected_run_id, expected_run_attempt, expected_head_sha):
    try:
        members = alpha_primary_artifact._read_zip_members(archive_bytes)
    except Exception as exc:
        raise HistoryArtifactError(f"invalid history artifact ZIP: {exc}") from exc
    raw_manifest = members.get("manifest.json")
    if raw_manifest is None:
        raise HistoryArtifactError("history artifact has no root manifest.json")
    try:
        manifest = json.loads(raw_manifest.decode("utf-8"))
    except Exception as exc:
        raise HistoryArtifactError("history artifact manifest is not valid UTF-8 JSON") from exc
    legacy_identity = False
    try:
        revision = history_continuity._validate_expected_identity(
            manifest,
            expected_run_id,
            expected_run_attempt,
            expected_head_sha,
        )
    except ValueError:
        revision = history_continuity.manifest_revision(manifest)
        if int(manifest.get("schema_version") or 0) >= 3 or revision["head_sha"] is not None:
            raise
        revision = history_continuity._validate_expected_identity(
            manifest,
            expected_run_id,
            expected_run_attempt,
            None,
        )
        legacy_identity = True
    rel = str(manifest.get("latest_snapshot") or "")
    if rel not in members:
        raise HistoryArtifactError(f"history artifact is missing latest snapshot {rel!r}")
    try:
        snapshot = json.loads(members[rel].decode("utf-8"))
    except Exception as exc:
        raise HistoryArtifactError("history artifact latest snapshot is invalid JSON") from exc
    observation = snapshot.get("observation") or {}
    if observation:
        if (
            str(observation.get("run_id")) != str(revision["run_id"])
            or int(observation.get("run_attempt") or 0) != revision["attempt"]
            or str(observation.get("head_sha") or "").lower()
            != str(revision["head_sha"] or "").lower()
        ):
            raise HistoryArtifactError("history snapshot observation contradicts manifest identity")
    elif not legacy_identity:
        raise HistoryArtifactError("history snapshot has no observation identity")
    selected_manifest = dict(manifest)
    selected_manifest["legacy_identity"] = legacy_identity
    return members, selected_manifest


def select_exact_archive(
    artifacts,
    download,
    expected_run_id,
    expected_run_attempt,
    expected_head_sha,
    artifact_name=DEFAULT_ARTIFACT_NAME,
):
    matches = []
    rejected = []
    for artifact in artifacts or []:
        if artifact.get("name") != artifact_name or artifact.get("expired") is True:
            continue
        workflow = artifact.get("workflow_run") or {}
        listed_run = workflow.get("id")
        listed_head = str(workflow.get("head_sha") or "").lower()
        if listed_run not in (None, "") and int(listed_run) != int(expected_run_id):
            rejected.append(f"artifact={artifact.get('id')} reason=listing run mismatch")
            continue
        if listed_head and listed_head != str(expected_head_sha or "").lower():
            rejected.append(f"artifact={artifact.get('id')} reason=listing head SHA mismatch")
            continue
        try:
            archive_bytes = download(artifact)
            members, manifest = inspect_archive(
                archive_bytes,
                expected_run_id,
                expected_run_attempt,
                expected_head_sha,
            )
        except (HistoryArtifactError, ValueError, OSError, urllib.error.URLError) as exc:
            rejected.append(f"artifact={artifact.get('id')} reason={exc}")
            continue
        matches.append((artifact, members, manifest))
    if len(matches) != 1:
        detail = "; ".join(rejected[-5:])
        raise HistoryArtifactError(
            f"expected exactly one history artifact for run={expected_run_id} "
            f"attempt={expected_run_attempt}, found={len(matches)}; {detail}"
        )
    return matches[0]


def _write_members(root, members):
    root = Path(root)
    for name, content in members.items():
        destination = root / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(content)


def restore_exact_history(
    current_root,
    repository,
    token,
    run_id,
    run_attempt,
    head_sha,
    artifact_name=DEFAULT_ARTIFACT_NAME,
):
    artifacts = alpha_primary_artifact.list_exact_run_artifacts(repository, token, run_id)
    selected, members, manifest = select_exact_archive(
        artifacts,
        lambda artifact: _download_archive(artifact.get("archive_download_url"), token),
        run_id,
        run_attempt,
        head_sha,
        artifact_name,
    )
    current_root = Path(current_root)
    current_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="history-artifact.", dir=current_root.parent) as temporary:
        extracted = Path(temporary)
        _write_members(extracted, members)
        history_continuity.validate_history_tree(extracted)
        history_continuity.hydrate_from_exact_artifact(
            current_root,
            extracted,
            run_id,
            run_attempt,
            None if manifest.get("legacy_identity") else head_sha,
        )
    print(
        "HISTORY_ARTIFACT_RESTORED "
        f"artifact_id={selected.get('id')} run_id={run_id} attempt={run_attempt} "
        f"snapshot={manifest.get('latest_snapshot')} legacy_identity={manifest.get('legacy_identity')}",
        flush=True,
    )


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--current", required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY"))
    parser.add_argument("--token-env", default="GITHUB_TOKEN")
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--run-attempt", required=True, type=int)
    parser.add_argument("--head-sha", required=True)
    parser.add_argument("--artifact-name", default=DEFAULT_ARTIFACT_NAME)
    args = parser.parse_args(argv)
    token = os.environ.get(args.token_env)
    if not args.repository or not token:
        raise HistoryArtifactError("repository and GitHub token are required")
    restore_exact_history(
        args.current,
        args.repository,
        token,
        args.run_id,
        args.run_attempt,
        args.head_sha,
        args.artifact_name,
    )


if __name__ == "__main__":
    main()
