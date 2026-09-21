"""Host-side public-registry downloader for a VM without registry connectivity."""

import gzip
import hashlib
import io
import json
import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlencode

ROOT = Path(__file__).resolve().parent
IMAGES = [
    ("registry-1.docker.io", "library/memcached", "alpine", "memcached:alpine"),
    ("registry-1.docker.io", "library/redis", "alpine", "redis:alpine"),
    ("registry-1.docker.io", "library/rabbitmq", "4.2", "rabbitmq:4.2"),
    ("registry-1.docker.io", "zulip/zulip-postgresql", "14", "zulip/zulip-postgresql:14"),
    ("ghcr.io", "zulip/zulip-server", "12.2-0", "ghcr.io/zulip/zulip-server:12.2-0"),
]
ACCEPT = ",".join(
    [
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ]
)


def fetch(url: str, path: Path, token: str = "") -> None:
    """Download with the host's working TLS stack, without exposing response bodies."""
    command = [
        "curl",
        "--fail",
        "--silent",
        "--show-error",
        "--location",
        "--retry",
        "3",
        "--connect-timeout",
        "15",
        "--max-time",
        "900",
        "--output",
        str(path),
        "--header",
        f"Accept: {ACCEPT}",
    ]
    if token:
        command += ["--header", f"Authorization: Bearer {token}"]
    subprocess.run([*command, url], check=True)


def add_bytes(archive: tarfile.TarFile, name: str, data: bytes) -> None:
    """Add one in-memory metadata member to a Docker save archive."""
    info = tarfile.TarInfo(name)
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def main() -> None:
    """Verify layer digests and transfer one public arm64 image at a time."""
    env = {
        **os.environ,
        "DOCKER_CONFIG": str(ROOT / "docker-client"),
        "DOCKER_HOST": f"unix://{Path.home()}/.colima/zulip-bridge/docker.sock",
    }
    digests_path = ROOT / "image-digests.json"
    digests = json.loads(digests_path.read_text()) if digests_path.exists() else {}
    for host, repository, tag, image in IMAGES:
        if (
            image in digests
            and subprocess.run(
                ["docker", "image", "inspect", image],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            ).returncode
            == 0
        ):
            continue
        print(f"Downloading {image}", flush=True)
        with tempfile.TemporaryDirectory(prefix="zulip-image-") as directory:
            tmp = Path(directory)
            auth = "https://auth.docker.io/token" if host != "ghcr.io" else "https://ghcr.io/token"
            service = "registry.docker.io" if host != "ghcr.io" else "ghcr.io"
            fetch(
                auth
                + "?"
                + urlencode({"service": service, "scope": f"repository:{repository}:pull"}),
                tmp / "auth",
            )
            token = json.loads((tmp / "auth").read_text())["token"]
            base = f"https://{host}/v2/{repository}"
            fetch(f"{base}/manifests/{tag}", tmp / "manifest", token)
            manifest = json.loads((tmp / "manifest").read_text())
            if "manifests" in manifest:
                selected = next(
                    m
                    for m in manifest["manifests"]
                    if m.get("platform", {}).get("os") == "linux"
                    and m["platform"].get("architecture") == "arm64"
                )
                fetch(f"{base}/manifests/{selected['digest']}", tmp / "manifest", token)
                assert (
                    "sha256:" + hashlib.sha256((tmp / "manifest").read_bytes()).hexdigest()
                    == selected["digest"]
                )
                manifest = json.loads((tmp / "manifest").read_text())
            manifest_digest = (
                "sha256:" + hashlib.sha256((tmp / "manifest").read_bytes()).hexdigest()
            )
            config = manifest["config"]["digest"]
            fetch(f"{base}/blobs/{config}", tmp / "config", token)
            assert "sha256:" + hashlib.sha256((tmp / "config").read_bytes()).hexdigest() == config
            config_name = config.split(":")[1] + ".json"
            archive_path = tmp / "image.tar"
            layers = []
            with tarfile.open(archive_path, "w") as archive:
                add_bytes(archive, config_name, (tmp / "config").read_bytes())
                for index, layer in enumerate(manifest["layers"]):
                    print(f"  layer {index + 1}/{len(manifest['layers'])}", flush=True)
                    blob = tmp / "blob"
                    fetch(f"{base}/blobs/{layer['digest']}", blob, token)
                    with blob.open("rb") as source:
                        assert (
                            "sha256:" + hashlib.file_digest(source, "sha256").hexdigest()
                            == layer["digest"]
                        )
                    uncompressed = tmp / "layer.tar"
                    with blob.open("rb") as source:
                        magic = source.read(2)
                    opener = gzip.open if magic == b"\x1f\x8b" else open
                    with opener(blob, "rb") as source, uncompressed.open("wb") as dest:
                        shutil.copyfileobj(source, dest)
                    name = f"{layer['digest'].split(':')[1]}/layer.tar"
                    layers.append(name)
                    archive.add(uncompressed, arcname=name)
                    blob.unlink()
                    uncompressed.unlink()
                add_bytes(
                    archive,
                    "manifest.json",
                    json.dumps(
                        [{"Config": config_name, "RepoTags": [image], "Layers": layers}]
                    ).encode(),
                )
            subprocess.run(["docker", "load", "--input", str(archive_path)], env=env, check=True)
            digests[image] = manifest_digest
            digests_path.write_text(json.dumps(digests, indent=2) + "\n")


if __name__ == "__main__":
    main()
