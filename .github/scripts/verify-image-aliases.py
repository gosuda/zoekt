from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from urllib.parse import urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen
from urllib.error import HTTPError

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


def put_blob(value: bytes) -> str:
    expected = digest(value)
    headers, _ = request(f"{API}/blobs/uploads/", "POST", b"")
    location = urlsplit(headers["location"])
    query = location.query + ("&" if location.query else "") + urlencode({"digest": expected})
    target = urlunsplit((location.scheme, location.netloc, location.path, query, location.fragment))
    uploaded, _ = request(target, "PUT", value, "application/octet-stream")
    assert uploaded["docker-content-digest"] == expected
    return expected


def publish_fixture(revision: str) -> str:
    manifests: list[dict[str, object]] = []
    for architecture in ("amd64", "arm64"):
        config = encoded({"architecture": architecture, "os": "linux", "rootfs": {"type": "layers", "diff_ids": []}, "config": {"Labels": {"org.opencontainers.image.revision": revision}}})
        config_digest = put_blob(config)
        manifest = encoded({"schemaVersion": 2, "mediaType": MANIFEST, "config": {"mediaType": CONFIG, "digest": config_digest, "size": len(config)}, "layers": []})
        manifest_digest = digest(manifest)
        headers, _ = request(f"{API}/manifests/{manifest_digest}", "PUT", manifest, MANIFEST)
        assert headers["docker-content-digest"] == manifest_digest
        manifests.append({"mediaType": MANIFEST, "digest": manifest_digest, "size": len(manifest), "platform": {"architecture": architecture, "os": "linux"}})
    index = encoded({"schemaVersion": 2, "mediaType": INDEX, "manifests": manifests})
    headers, _ = request(f"{API}/manifests/commit-{revision}", "PUT", index, INDEX)
    assert headers["docker-content-digest"] == digest(index)
    return digest(index)


def alias_digest(alias: str) -> str:
    headers, payload = request(f"{API}/manifests/{alias}", "GET")
    index = json.loads(payload)
    assert {entry["platform"]["architecture"] for entry in index["manifests"]} == {"amd64", "arm64"}
    return headers["docker-content-digest"]


def main() -> None:
    source = Path.cwd()
    workflow = yaml.safe_load((source / ".github/workflows/docker.yml").read_text())
    baseline = yaml.safe_load(git(source, "show", "0043247a0ba9dbbfc27605976e3173a9acf630af:.github/workflows/docker.yml"))
    assert baseline["jobs"]["merge"]["concurrency"]["queue"] == "max"
    assert "concurrency" not in workflow["jobs"]["merge"]
    assert "github.run_id" in workflow["concurrency"]["group"]
    assert workflow["jobs"]["promote"]["needs"] == "merge"
    plan = next(step["run"] for step in workflow["jobs"]["merge"]["steps"] if step.get("id") == "publication")
    old_guard = next(step["run"] for step in baseline["jobs"]["merge"]["steps"] if step.get("id") == "promotion")
    script = source / ".github/workflows/docker-promote.sh"
    records: list[dict[str, object]] = []
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
        git(writer, "tag", "v1.2.0")
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
        git(consumer, "clone", "--no-tags", str(origin), ".")
        output = root / "outputs"
        environment = {**os.environ, "IMAGE": IMAGE, "RUNNER_TEMP": str(root), "GITHUB_OUTPUT": str(output)}
        publish_fixture(first)
        current_digest = publish_fixture(current)

        for ref, aliases in (("refs/heads/main", ["latest"]), ("refs/tags/v1.2.1", ["latest", "1.2"]), ("refs/tags/v1.2.2-rc.1", ["latest"])):
            output.write_text("")
            command(["bash", "-euo", "pipefail", "-c", plan], consumer, {**environment, "GITHUB_REF": ref})
            values = dict(line.split("=", 1) for line in output.read_text().splitlines())
            assert json.loads(values["aliases"]) == aliases, (ref, values)
            assert values["commit"] == current, values
        records.append({"case": "main/stable/prerelease alias planning", "result": "pass"})

        output.write_text("")
        command(["bash", "-euo", "pipefail", "-c", old_guard], consumer, {**environment, "GITHUB_REF": "refs/tags/v1.2.1", "GITHUB_SHA": current})
        assert output.read_text().strip() == "current_main=false"
        records.append({"case": "old stable-tag guard rejects current main", "result": "red reproduced"})

        command(["bash", str(script), "latest"], consumer, environment)
        assert alias_digest("latest") == current_digest
        records.append({"case": "stable release at main advances latest", "digest": current_digest})
        git(consumer, "checkout", "--detach", first)
        command(["bash", str(script), "latest"], consumer, environment)
        assert alias_digest("latest") == current_digest
        records.append({"case": "late older job preserves current latest", "digest": current_digest})
        command(["bash", str(script), "1.2"], consumer, environment)
        assert alias_digest("1.2") == current_digest
        records.append({"case": "minor alias selects latest stable and excludes prerelease", "digest": current_digest})

        git(writer, "merge", "--ff-only", "future")
        git(writer, "tag", "v1.2.2")
        git(writer, "push", "origin", "main", "--tags")
        for alias in ("latest", "1.2"):
            result = command(["bash", str(script), alias], consumer, environment)
            assert "no published index yet" in result, result
            assert alias_digest(alias) == current_digest
        records.append({"case": "unbuilt newer targets retain both published aliases", "result": "pass"})

        git(writer, "tag", "v1.3.0", current)
        git(writer, "tag", "v1.3.1", future)
        git(writer, "push", "origin", "--tags")
        previous_script = root / "previous-promotion.sh"
        previous_script.write_text(git(source, "show", "310a9a216bfff5f756dd05b3857fe0d60cd9ce0d:.github/workflows/docker-promote.sh") + "\n")
        previous_output = command(["bash", str(previous_script), "1.3"], consumer, environment)
        assert "no published index yet" in previous_output
        try:
            alias_digest("1.3")
        except HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("the previous implementation unexpectedly published the first minor alias")
        command(["bash", str(script), "1.3"], consumer, environment)
        assert alias_digest("1.3") == current_digest
        records.append({"case": "unbuilt newer tag cannot suppress the first published minor alias", "result": "red/green verified"})

        future_digest = publish_fixture(future)
        for alias in ("latest", "1.2", "1.3"):
            command(["bash", str(script), alias], consumer, environment)
            assert alias_digest(alias) == future_digest
        records.append({"case": "older coalescing worker promotes newly published current targets", "digest": future_digest})

        invalid = subprocess.run(["bash", str(script), "../../invalid"], cwd=consumer, env=environment, capture_output=True, text=True, check=False)
        assert invalid.returncode != 0 and "invalid mutable image alias" in invalid.stderr
        records.append({"case": "invalid alias rejected", "exit": invalid.returncode})
        command(["docker", "stop", "zoekt-alias-proof"], source)
        offline = subprocess.run(["bash", str(script), "latest"], cwd=consumer, env=environment, capture_output=True, text=True, check=False)
        assert offline.returncode != 0
        assert "no published index yet" not in offline.stdout
        print(offline.stderr)
        records.append({"case": "registry transport failure is not treated as an absent index", "exit": offline.returncode})
    print(json.dumps({"cases": records}, indent=2))


if __name__ == "__main__":
    main()
