import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path


DEFAULT_SNAPSHOT = Path("snapshot.json")
DEFAULT_MANIFEST = Path("alpha-artifact-manifest.json")


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(snapshot_path=DEFAULT_SNAPSHOT, artifact_name="realtime-snapshot"):
    snapshot_path = Path(snapshot_path)
    stat = snapshot_path.stat()
    return {
        "schema_version": 1,
        "kind": "looklookbigA-primary-artifact-manifest",
        "primary_artifact_count": 1,
        "primary": {
            "artifact_name": artifact_name,
            "snapshot_path": snapshot_path.name,
            "size_bytes": stat.st_size,
            "digest": sha256_file(snapshot_path),
            "digest_algorithm": "SHA-256",
            "digest_source": "PROVIDER_COMPUTED",
            "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
            "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "producing_ref": os.environ.get("GITHUB_REF"),
            "producing_ref_name": os.environ.get("GITHUB_REF_NAME"),
            "producing_commit_sha": os.environ.get("GITHUB_SHA"),
            "workflow_ref": os.environ.get("GITHUB_WORKFLOW_REF"),
            "workflow_sha": os.environ.get("GITHUB_WORKFLOW_SHA"),
            "produced_at": datetime.now(timezone.utc).isoformat(),
        },
    }


def write_manifest(
    snapshot_path=DEFAULT_SNAPSHOT,
    output_path=DEFAULT_MANIFEST,
    artifact_name="realtime-snapshot",
):
    payload = build_manifest(snapshot_path, artifact_name)
    Path(output_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="Create the deterministic primary snapshot artifact manifest used by looklookAlpha."
    )
    parser.add_argument("--snapshot", default=str(DEFAULT_SNAPSHOT))
    parser.add_argument("--output", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--artifact-name", default="realtime-snapshot")
    args = parser.parse_args(argv)

    payload = write_manifest(args.snapshot, args.output, args.artifact_name)
    primary = payload["primary"]
    print(
        "ALPHA_PRIMARY_ARTIFACT "
        f"count=1 name={primary['artifact_name']} size={primary['size_bytes']} "
        f"sha256={primary['digest']} run_id={primary['workflow_run_id']}"
    )


if __name__ == "__main__":
    main()
