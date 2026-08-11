import hashlib
import io
import json
import zipfile

import alpha_primary_artifact


RUN_ID = 101
ARTIFACT_NAME = "realtime-snapshot"
RUN_SHA = "c" * 40


def artifact(
    artifact_id=7,
    *,
    name=ARTIFACT_NAME,
    run_id=RUN_ID,
    expired=False,
    head_sha=RUN_SHA,
):
    return {
        "id": artifact_id,
        "name": name,
        "size_in_bytes": 1234,
        "url": f"https://api.github.test/artifacts/{artifact_id}",
        "archive_download_url": (
            f"https://api.github.test/artifacts/{artifact_id}/zip"
        ),
        "expired": expired,
        "created_at": "2026-08-11T00:00:00Z",
        "updated_at": "2026-08-11T00:01:00Z",
        "expires_at": "2026-08-12T00:00:00Z",
        "digest": "sha256:" + "a" * 64,
        "workflow_run": {"id": run_id, "head_sha": head_sha},
    }


def exact_run():
    return {
        "workflow_run_id": RUN_ID,
        "workflow_run_head_sha": RUN_SHA,
        "workflow_run_attempt": 1,
        "workflow_run_head_branch": "master",
        "workflow_path": ".github/workflows/realtime-quotes.yml",
        "workflow_id": 123,
        "workflow_run_api_url": f"https://api.github.test/runs/{RUN_ID}",
        "workflow_run_html_url": f"https://github.test/runs/{RUN_ID}",
        "workflow_run_event": "workflow_dispatch",
    }


def identity():
    return alpha_primary_artifact.resolve_exact_primary_from_listing(
        [artifact()], RUN_ID, ARTIFACT_NAME, exact_run_provenance=exact_run()
    )


def archive_bytes(
    snapshot=b'{"schema_version":16}\n',
    *,
    digest=None,
    include_manifest=True,
    manifest_run_id=RUN_ID,
    producing_commit_sha=RUN_SHA,
    workflow_run_attempt="1",
    producing_ref_name="master",
    workflow_ref=(
        "owner/repo/.github/workflows/realtime-quotes.yml@refs/heads/master"
    ),
):
    digest = digest or hashlib.sha256(snapshot).hexdigest()
    manifest = {
        "schema_version": 1,
        "kind": "looklookbigA-primary-artifact-manifest",
        "primary_artifact_count": 1,
        "primary": {
            "artifact_name": ARTIFACT_NAME,
            "snapshot_path": "snapshot.json",
            "size_bytes": len(snapshot),
            "digest": digest,
            "digest_algorithm": "SHA-256",
            "digest_source": "PROVIDER_COMPUTED",
            "workflow_run_id": str(manifest_run_id),
            "workflow_run_attempt": workflow_run_attempt,
            "producing_ref": "refs/heads/master",
            "producing_ref_name": producing_ref_name,
            "producing_commit_sha": producing_commit_sha,
            "workflow_ref": workflow_ref,
            "workflow_sha": RUN_SHA,
            "produced_at": "2026-08-11T00:02:00+00:00",
        },
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("snapshot.json", snapshot)
        if include_manifest:
            archive.writestr(
                "alpha-artifact-manifest.json", json.dumps(manifest)
            )
    return buffer.getvalue()


def expect_code(code, fn):
    try:
        fn()
    except alpha_primary_artifact.PrimaryArtifactError as exc:
        assert exc.code == code, (exc.code, str(exc))
    else:
        raise AssertionError(f"expected {code}")


def test_exact_primary_identity_is_machine_resolved():
    resolved = identity()
    assert resolved["workflow_run_id"] == RUN_ID
    assert resolved["workflow_run_head_sha"] == RUN_SHA
    assert resolved["workflow_run_attempt"] == 1
    assert resolved["workflow_run_head_branch"] == "master"
    assert resolved["workflow_path"] == ".github/workflows/realtime-quotes.yml"
    assert resolved["artifact_id"] == 7
    assert resolved["artifact_name"] == ARTIFACT_NAME
    assert resolved["artifact_api_url"].endswith("/7")
    assert resolved["archive_download_url"].endswith("/7/zip")
    assert resolved["artifact_size_bytes"] == 1234
    assert (
        resolved["artifact_digest_source"]
        == "GITHUB_ACTIONS_ARTIFACT_API"
    )
    print("PASS alpha_primary_exact_identity")


def test_listing_and_exact_run_provenance_must_agree():
    wrong = exact_run()
    wrong["workflow_run_head_sha"] = "d" * 40
    expect_code(
        "PRIMARY_ARTIFACT_PROVENANCE_MISMATCH",
        lambda: alpha_primary_artifact.resolve_exact_primary_from_listing(
            [artifact()], RUN_ID, ARTIFACT_NAME, exact_run_provenance=wrong
        ),
    )
    print("PASS alpha_primary_listing_run_provenance_binding")


def test_missing_multiple_expired_and_wrong_run_fail_closed():
    expect_code(
        "PRIMARY_ARTIFACT_MISSING",
        lambda: alpha_primary_artifact.resolve_exact_primary_from_listing(
            [], RUN_ID, ARTIFACT_NAME
        ),
    )
    expect_code(
        "PRIMARY_ARTIFACT_AMBIGUOUS",
        lambda: alpha_primary_artifact.resolve_exact_primary_from_listing(
            [artifact(7), artifact(8)], RUN_ID, ARTIFACT_NAME
        ),
    )
    expect_code(
        "PRIMARY_ARTIFACT_EXPIRED",
        lambda: alpha_primary_artifact.resolve_exact_primary_from_listing(
            [artifact(expired=True)], RUN_ID, ARTIFACT_NAME
        ),
    )
    expect_code(
        "PRIMARY_ARTIFACT_WRONG_RUN",
        lambda: alpha_primary_artifact.resolve_exact_primary_from_listing(
            [artifact(run_id=999)], RUN_ID, ARTIFACT_NAME
        ),
    )
    print("PASS alpha_primary_resolution_fail_closed")


def test_downloaded_manifest_and_snapshot_digest_are_verified():
    verified = alpha_primary_artifact.validate_downloaded_primary_archive(
        identity(), archive_bytes()
    )
    assert verified["manifest_verified"] is True
    assert verified["run_provenance_verified"] is True
    assert (
        verified["snapshot_digest_source"]
        == "DOWNLOADED_CONTENT_VERIFIED"
    )
    assert verified["producing_commit_sha"] == RUN_SHA
    assert verified["produced_at"] == "2026-08-11T00:02:00+00:00"
    print("PASS alpha_primary_download_verification")


def test_manifest_run_provenance_mismatch_fails_closed():
    expect_code(
        "PRIMARY_ARTIFACT_PROVENANCE_MISMATCH",
        lambda: alpha_primary_artifact.validate_downloaded_primary_archive(
            identity(), archive_bytes(producing_commit_sha="d" * 40)
        ),
    )
    expect_code(
        "PRIMARY_ARTIFACT_PROVENANCE_MISMATCH",
        lambda: alpha_primary_artifact.validate_downloaded_primary_archive(
            identity(), archive_bytes(workflow_run_attempt="2")
        ),
    )
    expect_code(
        "PRIMARY_ARTIFACT_PROVENANCE_MISMATCH",
        lambda: alpha_primary_artifact.validate_downloaded_primary_archive(
            identity(), archive_bytes(producing_ref_name="other")
        ),
    )
    expect_code(
        "PRIMARY_ARTIFACT_PROVENANCE_MISMATCH",
        lambda: alpha_primary_artifact.validate_downloaded_primary_archive(
            identity(), archive_bytes(
                workflow_ref=(
                    "owner/repo/.github/workflows/other.yml@refs/heads/master"
                )
            )
        ),
    )
    print("PASS alpha_primary_manifest_run_provenance_fail_closed")


def test_missing_manifest_and_digest_corruption_fail_closed():
    expect_code(
        "PRIMARY_ARTIFACT_MANIFEST_MISSING",
        lambda: alpha_primary_artifact.validate_downloaded_primary_archive(
            identity(), archive_bytes(include_manifest=False)
        ),
    )
    expect_code(
        "PRIMARY_ARTIFACT_DIGEST_MISMATCH",
        lambda: alpha_primary_artifact.validate_downloaded_primary_archive(
            identity(), archive_bytes(digest="0" * 64)
        ),
    )
    expect_code(
        "PRIMARY_ARTIFACT_MANIFEST_IDENTITY_MISMATCH",
        lambda: alpha_primary_artifact.validate_downloaded_primary_archive(
            identity(), archive_bytes(manifest_run_id=999)
        ),
    )
    print("PASS alpha_primary_download_fail_closed")


def main():
    tests = [
        test_exact_primary_identity_is_machine_resolved,
        test_listing_and_exact_run_provenance_must_agree,
        test_missing_multiple_expired_and_wrong_run_fail_closed,
        test_downloaded_manifest_and_snapshot_digest_are_verified,
        test_manifest_run_provenance_mismatch_fails_closed,
        test_missing_manifest_and_digest_corruption_fail_closed,
    ]
    for test in tests:
        test()
    print(f"ALPHA_PRIMARY_ARTIFACT_TESTS passed={len(tests)}")


if __name__ == "__main__":
    main()
