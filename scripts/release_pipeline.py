"""发布流水线的薄入口；核心校验位于 pa_agent.release_pipeline。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from pa_agent import __version__
from pa_agent.release_pipeline import (
    ReleaseValidationError,
    archive_source,
    build_release_manifest,
    sanitize_junit_report,
    scan_release_tree,
    validate_capability_index,
    validate_desktop_evidence,
    validate_source_archive,
    write_sha256sums,
)


def _git_sha(root: Path, ref: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", ref],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    sha = completed.stdout.strip().lower()
    if completed.returncode != 0 or len(sha) != 40:
        raise ReleaseValidationError("无法解析发布 ref 的完整 SHA")
    return sha


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-source")
    build.add_argument("--repo-root", type=Path, default=Path.cwd())
    build.add_argument("--output-dir", type=Path, required=True)
    build.add_argument("--ref", default="HEAD")

    verify = subparsers.add_parser("verify-archive")
    verify.add_argument("archive", type=Path)
    verify.add_argument("--sha", required=True)

    index = subparsers.add_parser("validate-index")
    index.add_argument(
        "--path",
        type=Path,
        default=Path("docs/evidence/capability-index.json"),
    )
    index.add_argument("--repo-root", type=Path, default=Path.cwd())
    index.add_argument("--evidence-root", type=Path)
    index.add_argument("--schema-root", type=Path)
    index.add_argument("--source-archive", type=Path)
    index.add_argument("--evidence-archive", type=Path)
    index.add_argument("--release-manifest", type=Path)
    index.add_argument("--checksums", type=Path)
    index.add_argument("--require-fresh-now", action="store_true")
    index.add_argument("--sha")
    index.add_argument("--stable", action="store_true")

    desktop = subparsers.add_parser("validate-desktop")
    desktop.add_argument("directory", type=Path)
    desktop.add_argument("--sha", required=True)

    scan = subparsers.add_parser("scan-tree")
    scan.add_argument("directory", type=Path)
    scan.add_argument(
        "--reject-private-paths",
        action="store_true",
    )

    sanitize = subparsers.add_parser("sanitize-junit")
    sanitize.add_argument("report", type=Path)

    manifest = subparsers.add_parser("manifest")
    manifest.add_argument("--source", type=Path, required=True)
    manifest.add_argument("--evidence", type=Path, required=True)
    manifest.add_argument("--output-dir", type=Path, required=True)
    manifest.add_argument("--sha", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "build-source":
            root = args.repo_root.resolve()
            sha = _git_sha(root, args.ref)
            archive = archive_source(
                repo_root=root,
                output_dir=args.output_dir,
                ref=args.ref,
                version=__version__,
            )
            result = validate_source_archive(
                archive,
                expected_version=__version__,
                expected_sha=sha,
            )
        elif args.command == "verify-archive":
            result = validate_source_archive(
                args.archive,
                expected_version=__version__,
                expected_sha=args.sha,
            )
        elif args.command == "validate-index":
            result = validate_capability_index(
                args.path,
                stable=args.stable,
                expected_sha=args.sha,
                expected_version=__version__,
                repo_root=args.repo_root,
                evidence_root=args.evidence_root,
                schema_root=args.schema_root,
                source_archive=args.source_archive,
                evidence_archive=args.evidence_archive,
                release_manifest=args.release_manifest,
                checksums=args.checksums,
                require_fresh_now=args.require_fresh_now,
            )
        elif args.command == "validate-desktop":
            result = validate_desktop_evidence(
                args.directory,
                expected_sha=args.sha,
            )
        elif args.command == "scan-tree":
            result = scan_release_tree(
                args.directory,
                reject_private_paths=args.reject_private_paths,
            )
        elif args.command == "sanitize-junit":
            result = sanitize_junit_report(args.report)
        else:
            output_dir = args.output_dir.resolve()
            output_dir.mkdir(parents=True, exist_ok=True)
            manifest = build_release_manifest(
                version=__version__,
                git_sha=args.sha,
                source_archive=args.source,
                evidence_archive=args.evidence,
            )
            manifest_path = output_dir / "release-manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            sums_path = output_dir / "SHA256SUMS"
            write_sha256sums(
                [args.source, args.evidence, manifest_path],
                sums_path,
            )
            result = {
                "manifest": str(manifest_path),
                "sha256sums": str(sums_path),
            }
    except ReleaseValidationError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
