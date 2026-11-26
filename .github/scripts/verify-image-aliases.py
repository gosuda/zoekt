from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

import yaml

IMAGE = "localhost:5000/ci-zoekt"
API = "http://127.0.0.1:5000/v2/ci-zoekt"
INDEX = "application/vnd.oci.image.index.v1+json"
MANIFEST = "application/vnd.oci.image.manifest.v1+json"
CONFIG = "application/vnd.oci.image.config.v1+json"


def command(arguments: list[str], directory: Path, environment: dict[str, str] | None = None) -> str:
    result = subprocess.run(arguments, cwd=directory, env=environment, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"{arguments!r}: exit {result.returncode}\n{result.stdout}\n{result.stderr}")
    return result.stdout.strip()


def git(directory: Path, *arguments: str) -> str:
    return command(["git", "-c", "user.name=CI fixture", "-c", "user.email=ci@example.invalid", "-c", "commit.gpgsign=false", *arguments], directory)


def request(url: str, method: str, body: bytes | None = None, media_type: str | None = None) -> tuple[dict[str, str], bytes]:
    headers = {"Content-Type": media_type} if media_type else {"Accept": INDEX}
    with urlopen(Request(url, data=body, headers=headers, method=method), timeout=10) as response:
        return {key.lower(): value for key, value in response.headers.items()}, response.read()


def encoded(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode()


def digest(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def source_tag(ref: str, revision: str) -> str:
    return f"source-{revision}-{hashlib.sha256(ref.encode()).hexdigest()}"


def put_blob(value: bytes) -> str:
    expected = digest(value)
    headers, _ = request(f"{API}/blobs/uploads/", "POST", b"")
    location = urlsplit(headers["location"])
    query = location.query + ("&" if location.query else "") + urlencode({"digest": expected})
    target = urlunsplit((location.scheme, location.netloc, location.path, query, location.fragment))
    uploaded, _ = request(target, "PUT", value, "application/octet-stream")
    assert uploaded["docker-content-digest"] == expected
    return expected


def publish_fixture(revision: str, ref: str, version: str, tag_override: str | None = None) -> str:
    manifests: list[dict[str, object]] = []
    for architecture in ("amd64", "arm64"):
        config = encoded({"architecture": architecture, "os": "linux", "rootfs": {"type": "layers", "diff_ids": []}, "config": {"Labels": {"org.opencontainers.image.revision": revision, "org.opencontainers.image.version": version}}})
        config_digest = put_blob(config)
        manifest = encoded({"schemaVersion": 2, "mediaType": MANIFEST, "config": {"mediaType": CONFIG, "digest": config_digest, "size": len(config)}, "layers": []})
        manifest_digest = digest(manifest)
        headers, _ = request(f"{API}/manifests/{manifest_digest}", "PUT", manifest, MANIFEST)
        assert headers["docker-content-digest"] == manifest_digest
        manifests.append({"mediaType": MANIFEST, "digest": manifest_digest, "size": len(manifest), "platform": {"architecture": architecture, "os": "linux"}})
    index = encoded({"schemaVersion": 2, "mediaType": INDEX, "manifests": manifests})
    tag = tag_override if tag_override is not None else source_tag(ref, revision)
    headers, _ = request(f"{API}/manifests/{tag}", "PUT", index, INDEX)
    assert headers["docker-content-digest"] == digest(index)
    return digest(index)


def alias_digest(alias: str) -> str:
    headers, payload = request(f"{API}/manifests/{alias}", "GET")
    index = json.loads(payload)
    assert {entry["platform"]["architecture"] for entry in index["manifests"]} == {"amd64", "arm64"}
    return headers["docker-content-digest"]


def main() -> None:
    source = Path.cwd()
    print(f"PyYAML {yaml.__version__}")
    workflow = yaml.safe_load((source / ".github/workflows/docker.yml").read_text())
    promotion = yaml.safe_load((source / ".github/workflows/docker-promote.yml").read_text())
    baseline = yaml.safe_load(git(source, "show", "7f5ffc0c1e29d9312e1494073500aaaf68b3e062:.github/workflows/docker.yml"))
    assert "promote" in baseline["jobs"]
    assert "concurrency" in baseline["jobs"]["promote"]
    assert set(workflow["jobs"]) == {"build", "merge"}
    assert "concurrency" not in workflow["jobs"]["merge"]
    assert "github.run_id" in workflow["concurrency"]["group"]
    trigger = promotion.get("on", promotion.get(True))
    assert trigger["workflow_run"] == {"workflows": [workflow["name"]], "types": ["completed"]}
    assert set(promotion["jobs"]) == {"promote"}
    assert "conclusion == 'success'" in promotion["jobs"]["promote"]["if"]
    assert promotion["concurrency"] == {"group": "zoekt-container-aliases", "cancel-in-progress": False}
    assert all("build-push-action" not in step.get("uses", "") for step in promotion["jobs"]["promote"]["steps"])
    assert promotion["jobs"]["promote"]["steps"][-1]["run"].endswith(" all")
    plan = next(step["run"] for step in workflow["jobs"]["merge"]["steps"] if step.get("id") == "publication")
    old_plan = next(step["run"] for step in baseline["jobs"]["merge"]["steps"] if step.get("id") == "publication")
    script = source / ".github/workflows/docker-promote.sh"
    records: list[dict[str, object]] = [{"case": "immutable publication detached from coalesced alias workflow", "result": "pass"}]
    with TemporaryDirectory(prefix="zoekt-native-registry-") as temporary:
        root = Path(temporary)
        origin = root / "origin.git"
        writer = root / "writer"
        consumer = root / "consumer"
        for directory in (origin, writer, consumer):
            directory.mkdir()
        git(origin, "init", "--bare", "--initial-branch=main")
        git(writer, "init", "--initial-branch=main")
        git(writer, "remote", "add", "origin", str(origin))
        git(writer, "commit", "--allow-empty", "-m", "first source")
        first = git(writer, "rev-parse", "HEAD")
        git(writer, "push", "origin", "main")
        git(consumer, "clone", "--no-tags", str(origin), ".")
        fixture_script = consumer / ".github/workflows/docker-promote.sh"
        fixture_script.parent.mkdir(parents=True)
        fixture_script.write_text(script.read_text())
        output = root / "outputs"
        environment = {**os.environ, "IMAGE": IMAGE, "RUNNER_TEMP": str(root), "GITHUB_OUTPUT": str(output)}

        no_tags_baseline = root / "no-tags-baseline.sh"
        no_tags_baseline.write_text(git(source, "show", "32d61f7d3110081f837097e01ee05ec80aeed6e1:.github/workflows/docker-promote.sh") + "\n")
        red = subprocess.run(["bash", "-x", str(no_tags_baseline), "all"], cwd=consumer, env=environment, capture_output=True, text=True, check=False)
        print(f"NO TAGS BASELINE: exit {red.returncode}")
        print(red.stderr)
        assert red.returncode == 1, red.stderr
        assert red.stderr.rstrip().endswith("+ git fetch --no-tags --depth=1 --refmap= origin 'refs/tags/v*:refs/tags/v*'"), red.stderr
        records.append({"case": "separate unmatched tag fetch fails before first release", "result": "red reproduced"})

        first_digest = publish_fixture(first, "refs/heads/main", "development-first")
        command(["bash", str(script), "all"], consumer, environment)
        assert alias_digest("latest") == first_digest
        records.append({"case": "repository without stable tags promotes main", "digest": first_digest})

        git(writer, "tag", "v1.2.0")
        publish_fixture(first, "refs/tags/v1.2.0", "1.2.0")
        git(writer, "commit", "--allow-empty", "-m", "current source")
        current = git(writer, "rev-parse", "HEAD")
        git(writer, "tag", "v1.2.1")
        git(writer, "checkout", "-b", "future")
        git(writer, "commit", "--allow-empty", "-m", "future source")
        future = git(writer, "rev-parse", "HEAD")
        git(writer, "tag", "v1.2.2-rc.1")
        git(writer, "checkout", "main")
        git(writer, "push", "origin", "--all")
        git(writer, "push", "origin", "--tags")
        git(consumer, "fetch", "--no-tags", "origin", "main")
        git(consumer, "checkout", "--detach", current)

        refs = ("refs/heads/main", "refs/tags/v1.2.1", "refs/tags/v1.2.1+build.1", "refs/tags/v1.2.2-rc.1")
        generated: list[str] = []
        for ref in refs:
            output.write_text("")
            command(["bash", "-euo", "pipefail", "-c", plan], consumer, {**environment, "GITHUB_REF": ref})
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            assert values == {"source": source_tag(ref, current)}, (ref, values)
            assert len(values["source"]) <= 128
            generated.append(values["source"])
        assert len(set(generated)) == len(refs)
        records.append({"case": "main, stable, metadata and prerelease sources have distinct identities", "result": "pass"})

        previous_sources: list[str] = []
        for ref in ("refs/heads/main", "refs/tags/v1.2.1"):
            output.write_text("")
            command(["bash", "-euo", "pipefail", "-c", old_plan], consumer, {**environment, "GITHUB_REF": ref})
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            previous_sources.append(f"commit-{values['commit']}")
        assert previous_sources[0] == previous_sources[1]
        old_main = publish_fixture(current, "refs/heads/main", "development-current", previous_sources[0])
        old_release = publish_fixture(current, "refs/tags/v1.2.1", "1.2.1", previous_sources[1])
        assert old_main != old_release
        assert alias_digest(previous_sources[0]) == old_release
        records.append({"case": "old shared commit source is overwritten across build kinds", "result": "red reproduced"})

        main_digest = publish_fixture(current, "refs/heads/main", "development-current")
        command(["bash", str(script), "latest"], consumer, environment)
        assert alias_digest("latest") == main_digest
        records.append({"case": "unbuilt current release falls back to its main build", "digest": main_digest})
        current_digest = publish_fixture(current, "refs/tags/v1.2.1", "1.2.1")
        command(["bash", str(script), "latest"], consumer, environment)
        assert alias_digest("latest") == current_digest
        records.append({"case": "stable release at main advances latest", "digest": current_digest})
        main_digest = publish_fixture(current, "refs/heads/main", "development-current-rebuilt")
        assert alias_digest(source_tag("refs/tags/v1.2.1", current)) == current_digest
        command(["bash", str(script), "latest"], consumer, environment)
        assert alias_digest("latest") == current_digest
        records.append({"case": "late main build cannot overwrite the preferred release", "result": "pass"})

        git(consumer, "checkout", "--detach", first)
        command(["bash", str(script), "latest"], consumer, environment)
        assert alias_digest("latest") == current_digest
        command(["bash", str(script), "1.2"], consumer, environment)
        assert alias_digest("1.2") == current_digest
        records.append({"case": "late worker uses current refs and excludes prereleases", "result": "pass"})

        git(writer, "merge", "--ff-only", "future")
        git(writer, "tag", "v1.2.2")
        git(writer, "tag", "v1.3.0", current)
        git(writer, "tag", "v1.3.1", future)
        git(writer, "push", "origin", "main", "--tags")
        minor3_previous = publish_fixture(current, "refs/tags/v1.3.0", "1.3.0")
        command(["bash", str(script), "all"], consumer, environment)
        assert alias_digest("latest") == current_digest
        assert alias_digest("1.2") == current_digest
        assert alias_digest("1.3") == minor3_previous
        records.append({"case": "unbuilt newer targets retain published aliases and allow a new minor alias", "result": "pass"})

        future_main = publish_fixture(future, "refs/heads/main", "development-future")
        command(["bash", str(script), "latest"], consumer, environment)
        assert alias_digest("latest") == future_main
        future_minor2 = publish_fixture(future, "refs/tags/v1.2.2", "1.2.2")
        future_minor3 = publish_fixture(future, "refs/tags/v1.3.1", "1.3.1")
        command(["bash", str(script), "all"], consumer, environment)
        assert alias_digest("latest") == future_minor3
        assert alias_digest("1.2") == future_minor2
        assert alias_digest("1.3") == future_minor3
        records.append({"case": "one surviving worker converges every affected minor and latest", "result": "pass"})

        git(writer, "checkout", "-b", "metadata")
        git(writer, "commit", "--allow-empty", "-m", "metadata release")
        metadata = git(writer, "rev-parse", "HEAD")
        git(writer, "tag", "-a", "v1.4.2+build.0", "-m", "annotated metadata release")
        git(writer, "tag", "v1.4.1+build.999999999", current)
        git(writer, "tag", "v1.4.9-rc.1+build.9", metadata)
        git(writer, "tag", "v1.4.9+bad_1", metadata)
        git(writer, "tag", "v01.4.9+build.1", metadata)
        git(writer, "tag", "v1.4.3+001", metadata)
        git(writer, "push", "origin", "--all")
        git(writer, "push", "origin", "--tags")
        publish_fixture(current, "refs/tags/v1.4.1+build.999999999", "1.4.1")
        metadata_digest = publish_fixture(metadata, "refs/tags/v1.4.2+build.0", "1.4.2")
        publish_fixture(metadata, "refs/tags/v1.4.9-rc.1+build.9", "1.4.9-rc.1")
        publish_fixture(metadata, "refs/tags/v1.4.9+bad_1", "invalid-metadata")
        publish_fixture(metadata, "refs/tags/v01.4.9+build.1", "invalid-core")
        publish_fixture(metadata, "refs/tags/v1.4.2+build.0", "1.4.2", f"commit-{metadata}")

        previous_script = root / "previous-promotion.sh"
        previous_script.write_text(git(source, "show", "7f5ffc0c1e29d9312e1494073500aaaf68b3e062:.github/workflows/docker-promote.sh") + "\n")
        previous = subprocess.run(["bash", "-x", str(previous_script), "1.4"], cwd=consumer, env=environment, capture_output=True, text=True, check=False)
        assert previous.returncode == 1 and "+ test -n ''" in previous.stderr, previous.stderr
        print(f"METADATA DISCOVERY BASELINE: exit {previous.returncode}")
        print(previous.stderr)
        output.write_text("")
        command(["bash", "-euo", "pipefail", "-c", old_plan], consumer, {**environment, "GITHUB_REF": "refs/tags/v1.4.2+build.0"})
        previous_values = dict(line.split("=", 1) for line in output.read_text().splitlines())
        assert json.loads(previous_values["aliases"]) == ["latest"]
        records.append({"case": "old plan and discovery exclude valid build metadata", "result": "red reproduced"})

        command(["bash", str(script), "all"], consumer, environment)
        assert alias_digest("1.4") == metadata_digest
        assert alias_digest("latest") == future_minor3
        records.append({"case": "metadata releases obey core-version precedence and annotated-tag identity", "result": "pass"})
        metadata_higher = publish_fixture(metadata, "refs/tags/v1.4.3+001", "1.4.3")
        command(["bash", str(script), "all"], consumer, environment)
        assert alias_digest("1.4") == metadata_higher
        assert alias_digest("latest") == future_minor3
        records.append({"case": "numeric build metadata is accepted without promoting off-main releases to latest", "result": "pass"})

        git(writer, "tag", "v1.4.3+999", metadata)
        git(writer, "push", "origin", "--tags")
        alternate_metadata = publish_fixture(metadata, "refs/tags/v1.4.3+999", "1.4.3+999")
        git(consumer, "config", "--local", "--", "tag.sort", "-refname")
        old_order_script = root / "ambient-tag-order.sh"
        assert script.read_text().count("--sort=refname ") == 1
        old_order_script.write_text(script.read_text().replace("--sort=refname ", ""))
        command(["bash", str(old_order_script), "1.4"], consumer, environment)
        assert alias_digest("1.4") == alternate_metadata
        command(["bash", str(script), "1.4"], consumer, environment)
        assert alias_digest("1.4") == metadata_higher
        records.append({"case": "equal-precedence metadata ties ignore ambient Git tag sorting", "result": "red/green verified"})

        git(writer, "checkout", "main")
        git(writer, "merge", "--ff-only", "metadata")
        git(writer, "push", "origin", "main")
        command(["bash", str(script), "all"], consumer, environment)
        assert alias_digest("latest") == metadata_higher
        records.append({"case": "published metadata release becomes preferred when main reaches it", "result": "pass"})

        invalid = subprocess.run(["bash", str(script), "../../invalid"], cwd=consumer, env=environment, capture_output=True, text=True, check=False)
        assert invalid.returncode != 0 and "invalid mutable image alias" in invalid.stderr
        records.append({"case": "invalid alias rejected", "exit": invalid.returncode})
        command(["docker", "stop", "zoekt-alias-proof"], source)
        offline = subprocess.run(["bash", str(script), "latest"], cwd=consumer, env=environment, capture_output=True, text=True, check=False)
        assert offline.returncode != 0
        assert "no published index yet" not in offline.stdout
        print(offline.stderr)
        records.append({"case": "registry transport failure remains an error", "exit": offline.returncode})
    print(json.dumps({"cases": records}, indent=2))


if __name__ == "__main__":
    main()
