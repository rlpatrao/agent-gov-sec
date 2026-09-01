#!/usr/bin/env python3
"""Build, tag, and push the Galaxy enforcement service image to Amazon ECR.

The enforcement service is the runtime authority (mechanism 4), so which build is
running has to be answerable at any time. That requires three things, which this
script produces together:

* a **declared version**, read from ``deploy/VERSION`` — owned by the governing team
  and bumped in a reviewed commit, never derived from the branch or the clock;
* an **immutable per-commit tag**, ``<version>-<git-sha>`` — every build is uniquely
  addressable even when the declared version has not changed;
* a **digest**, printed on completion — the only identifier that cannot be moved,
  and therefore the one deployments should pin.

The bare ``<version>`` tag marks a release and is pushed only with ``--release``,
only from a clean tree. There is deliberately no ``latest``: a moving tag on the
component that enforces the controls would make "what is enforcing?" unanswerable.

Usage::

    # dev build → pushes 1.0.0-a1b2c3d only
    python scripts/publish_service_image.py --create-repo

    # release → pushes 1.0.0-a1b2c3d and 1.0.0
    python scripts/publish_service_image.py --release

    # print what would happen, touch nothing
    python scripts/publish_service_image.py --dry-run

The image is built for a single explicit platform (``linux/amd64`` by default; pass
``--platform linux/arm64`` for Graviton) with BuildKit attestations disabled, so one
tag resolves to one manifest and one digest.

Behind a TLS-terminating proxy, pass the private root so the in-container
``pip install`` can verify the index (used for that layer only, never baked in)::

    python scripts/publish_service_image.py --pip-ca /path/to/root.pem

Requires the ``[aws]`` extra (boto3), Docker, and AWS credentials that permit
``ecr:GetAuthorizationToken``, ``ecr:DescribeRepositories``, and push to the
repository (plus ``ecr:CreateRepository`` for ``--create-repo``).
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VERSION_FILE = ROOT / "deploy" / "VERSION"
DOCKERFILE = ROOT / "deploy" / "Dockerfile.service"
DEFAULT_REPO = "galaxy-rp-gov-enforcement"  # ${project_tag}-gov-enforcement

# Docker tags: letters, digits, underscore, period, dash; not leading with period
# or dash. A semver build-metadata "+" is not permitted, hence "-" throughout.
_TAG_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    """Run a command, streaming its output, and raise on a non-zero exit."""
    print(f"$ {' '.join(cmd)}", flush=True)
    return subprocess.run(cmd, check=True, **kw)


def capture(cmd: list[str]) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout.strip()


def read_version() -> str:
    if not VERSION_FILE.exists():
        raise SystemExit(f"missing {VERSION_FILE.relative_to(ROOT)}")
    version = VERSION_FILE.read_text().strip()
    # Semver without build metadata: the "+" separator is not a legal tag character.
    if not re.fullmatch(r"\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?", version):
        raise SystemExit(
            f"{VERSION_FILE.relative_to(ROOT)} is {version!r}; expected MAJOR.MINOR.PATCH "
            "with an optional pre-release suffix (build metadata after '+' is not a "
            "legal Docker tag)"
        )
    return version


def git_state() -> tuple[str, bool]:
    """Short revision, and whether the working tree has uncommitted changes."""
    sha = capture(["git", "-C", str(ROOT), "rev-parse", "--short=7", "HEAD"])
    dirty = bool(capture(["git", "-C", str(ROOT), "status", "--porcelain"]))
    return sha, dirty


def _explain_credentials(exc: Exception, profile: str | None) -> None:
    """Print actionable guidance for the usual credential failures."""
    from botocore.exceptions import (
        NoCredentialsError,
        TokenRetrievalError,
        UnauthorizedSSOTokenError,
    )

    if isinstance(exc, (TokenRetrievalError, UnauthorizedSSOTokenError)):
        # Name the profile the session actually used, whether it came from the
        # flag or the environment, so the suggested command is runnable as-is.
        resolved = profile or os.environ.get("AWS_PROFILE")
        target = f" --profile {resolved}" if resolved else ""
        print(
            f"\nAWS SSO session has expired. Refresh it and re-run:\n"
            f"  aws sso login{target}",
            file=sys.stderr,
        )
    elif isinstance(exc, NoCredentialsError):
        print(
            "\nNo AWS credentials resolved. Set AWS_PROFILE, pass --profile, or "
            "configure credentials, then re-run.",
            file=sys.stderr,
        )


def ecr_client(region: str | None, profile: str | None):
    try:
        import boto3
    except ModuleNotFoundError:
        raise SystemExit("boto3 is required: pip install '.[aws]'")
    session = boto3.Session(profile_name=profile, region_name=region)
    if not session.region_name:
        raise SystemExit("no AWS region resolved; pass --region or set AWS_REGION")
    return session.client("ecr"), session.region_name


def ensure_repository(ecr, name: str, create: bool) -> str:
    """Return the repository URI, creating the repository when asked to."""
    from botocore.exceptions import ClientError

    try:
        repos = ecr.describe_repositories(repositoryNames=[name])["repositories"]
        return repos[0]["repositoryUri"]
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "RepositoryNotFoundException":
            raise
    if not create:
        raise SystemExit(
            f"ECR repository {name!r} does not exist. Provision it with Terraform "
            "(cloud_adapters/aws/infra) — the declarative path — or re-run with "
            "--create-repo."
        )
    print(f"creating ECR repository {name} (immutable tags, scan on push)")
    created = ecr.create_repository(
        repositoryName=name,
        # Immutable: a released tag cannot be repointed at different bytes. This is
        # the property that makes "version 1.0.0 is enforcing" a verifiable claim.
        imageTagMutability="IMMUTABLE",
        imageScanningConfiguration={"scanOnPush": True},
        encryptionConfiguration={"encryptionType": "AES256"},
        tags=[{"Key": "Project", "Value": "galaxy-governance"},
              {"Key": "Component", "Value": "enforcement-service"}],
    )
    return created["repository"]["repositoryUri"]


def existing_tags(ecr, name: str, tags: list[str]) -> list[str]:
    """Which of `tags` already exist in the repository."""
    from botocore.exceptions import ClientError

    present = []
    for tag in tags:
        try:
            ecr.describe_images(repositoryName=name, imageIds=[{"imageTag": tag}])
            present.append(tag)
        except ClientError as exc:
            if exc.response["Error"]["Code"] not in (
                "ImageNotFoundException",
                "RepositoryNotFoundException",
            ):
                raise
    return present


def docker_login(ecr, registry: str) -> None:
    token = ecr.get_authorization_token()["authorizationData"][0]
    user, password = base64.b64decode(token["authorizationToken"]).decode().split(":", 1)
    print(f"$ docker login {registry} (ECR authorization token)")
    subprocess.run(
        ["docker", "login", "--username", user, "--password-stdin", registry],
        input=password, text=True, check=True, stdout=subprocess.DEVNULL,
    )


def build(
    tags: list[str], version: str, revision: str, pip_ca: Path | None, platform: str
) -> None:
    created = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    cmd = [
        "docker", "build",
        "-f", str(DOCKERFILE.relative_to(ROOT)),
        # No attestations: BuildKit's default provenance turns the push into a
        # manifest list whose children show up untagged in ECR, where an
        # expire-untagged lifecycle rule would delete manifests the released tag
        # still points at. One tag, one manifest, one digest to pin.
        "--provenance=false",
        "--sbom=false",
        # Always explicit. Building on an arm64 workstation would otherwise produce an
        # arm64-only image that fails to start on an x86 task, and the failure surfaces
        # at deploy time rather than at build time.
        "--platform", platform,
        "--build-arg", f"SERVICE_VERSION={version}",
        "--build-arg", f"SERVICE_REVISION={revision}",
        "--build-arg", f"SERVICE_CREATED={created}",
    ]
    if pip_ca:
        cmd += ["--secret", f"id=pip_ca,src={pip_ca}"]
    for tag in tags:
        cmd += ["-t", tag]
    cmd.append(".")
    run(cmd, cwd=ROOT)


def push(ref: str, attempts: int) -> None:
    """Push `ref`, retrying on transport failures.

    A push that dies partway leaves its completed blobs in the registry, so a retry
    resumes rather than restarting. Large layers over a proxied connection time out
    often enough that failing the release on the first attempt is the wrong default.
    """
    for attempt in range(1, attempts + 1):
        try:
            run(["docker", "push", ref])
            return
        except subprocess.CalledProcessError:
            if attempt == attempts:
                raise
            print(
                f"push attempt {attempt}/{attempts} failed; retrying "
                "(completed layers are not re-uploaded)",
                file=sys.stderr,
            )


def digest_of(ref: str) -> str | None:
    out = capture(["docker", "image", "inspect", ref, "--format", "{{json .RepoDigests}}"])
    for entry in json.loads(out) or []:
        if "@" in entry:
            return entry.split("@", 1)[1]
    return None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--repository", default=DEFAULT_REPO,
                    help=f"ECR repository name (default: {DEFAULT_REPO})")
    ap.add_argument("--region", default=None, help="AWS region (default: session region)")
    ap.add_argument("--profile", default=None, help="AWS profile (default: session default)")
    ap.add_argument("--release", action="store_true",
                    help="also push the bare <version> tag; requires a clean tree")
    ap.add_argument("--create-repo", action="store_true",
                    help="create the ECR repository if it does not exist")
    ap.add_argument("--pip-ca", type=Path, default=None,
                    help="private TLS root for the in-container pip install")
    ap.add_argument("--allow-existing", action="store_true",
                    help="skip tags already in the repository instead of failing")
    ap.add_argument("--platform", default="linux/amd64",
                    help="target platform (default: linux/amd64; use linux/arm64 for "
                         "Graviton tasks). Cross-building emulates and is slow; prefer a "
                         "native builder for releases.")
    ap.add_argument("--push-retries", type=int, default=3, metavar="N",
                    help="push attempts before giving up (default: 3)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan and exit without building or pushing")
    args = ap.parse_args()

    version = read_version()
    sha, dirty = git_state()
    revision = f"{sha}-dirty" if dirty else sha
    build_tag = f"{version}-{sha}{'-dirty' if dirty else ''}"

    if args.release and dirty:
        raise SystemExit(
            "refusing to release from a dirty working tree: the bare "
            f"{version!r} tag must correspond to a commit. Commit or stash first."
        )

    tag_names = [build_tag] + ([version] if args.release else [])
    for tag in tag_names:
        if not _TAG_RE.match(tag):
            raise SystemExit(f"computed tag {tag!r} is not a legal Docker tag")

    print(f"version   {version}   (deploy/VERSION)")
    print(f"revision  {revision}")
    print(f"tags      {', '.join(tag_names)}")
    print(f"platform  {args.platform}")
    if not args.release:
        print("           (dev build — the bare version tag needs --release)")

    if args.dry_run:
        print("\ndry run: nothing built, nothing pushed")
        return 0

    ecr, region = ecr_client(args.region, args.profile)
    try:
        repo_uri = ensure_repository(ecr, args.repository, args.create_repo)
        clash = existing_tags(ecr, args.repository, tag_names)
    except Exception as exc:  # noqa: BLE001 — re-raised unless it is a credential problem
        _explain_credentials(exc, args.profile)
        raise
    registry = repo_uri.split("/", 1)[0]
    print(f"repository {repo_uri} ({region})")

    if clash:
        if not args.allow_existing:
            raise SystemExit(
                f"tags already present in {args.repository}: {', '.join(clash)}. "
                "The repository is immutable by design — bump deploy/VERSION for a new "
                "release, or pass --allow-existing to push only the remaining tags."
            )
        print(f"skipping tags that already exist: {', '.join(clash)}")
        tag_names = [t for t in tag_names if t not in clash]
        if not tag_names:
            print("nothing to push")
            return 0

    refs = [f"{repo_uri}:{t}" for t in tag_names]
    build(refs, version, revision, args.pip_ca, args.platform)
    docker_login(ecr, registry)
    for ref in refs:
        push(ref, args.push_retries)

    digest = digest_of(refs[0])
    print("\npushed:")
    for ref in refs:
        print(f"  {ref}")
    if digest:
        print(f"\ndigest {digest}")
        print("Pin deployments by digest, not by tag:")
        print(f"  {repo_uri}@{digest}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"\ncommand failed with exit {exc.returncode}", file=sys.stderr)
        raise SystemExit(exc.returncode)
    except Exception as exc:  # noqa: BLE001 — the message is the useful part here
        from botocore.exceptions import (
            NoCredentialsError,
            TokenRetrievalError,
            UnauthorizedSSOTokenError,
        )

        if isinstance(
            exc, (TokenRetrievalError, UnauthorizedSSOTokenError, NoCredentialsError)
        ):
            raise SystemExit(2)
        raise
