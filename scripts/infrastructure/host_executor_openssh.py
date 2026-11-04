"""Build the verified upstream client release; distribution clients remain vulnerable."""

import hashlib
import io
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path, PurePosixPath

URL = "https://cdn.openbsd.org/pub/OpenBSD/OpenSSH/portable/openssh-10.6p1.tar.gz"
SHA256 = "a9dc9565dffe8640f64d863cd29a32bc4a3dbdec0566a7fc44c5d6ee767d5f39"


def validate_archive(members):
    if len(members) > 5000 or sum(member.size for member in members) > 64 * 1024 * 1024:
        raise ValueError("OpenSSH source archive exceeds unpacking bounds")
    for member in members:
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or ".." in path.parts
            or not path.parts
            or path.parts[0] != "openssh-10.6p1"
            or not (member.isfile() or member.isdir())
        ):
            raise ValueError("Unexpected upstream OpenSSH source layout")


def build():
    with urllib.request.urlopen(URL, timeout=60) as response:
        data = response.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != SHA256:
        raise ValueError("OpenSSH source differs from the upstream release checksum")
    with tempfile.TemporaryDirectory() as temporary:
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            validate_archive(archive.getmembers())
            archive.extractall(temporary, filter="data")
        folder = Path(temporary) / "openssh-10.6p1"
        subprocess.run(
            [
                "./configure",
                "--prefix=/opt/klip/openssh",
                "--sysconfdir=/etc/ssh",
                "--without-pam",
                "--without-kerberos5",
                "--without-libedit",
            ],
            cwd=folder,
            check=True,
        )
        binaries = ["ssh", "scp", "sftp", "ssh-keygen"]
        subprocess.run(["make", "-j2", *binaries], cwd=folder, check=True)
        destination = Path("/opt/klip/openssh/bin")
        destination.mkdir(parents=True)
        for binary in binaries:
            shutil.copyfile(folder / binary, destination / binary)
            (destination / binary).chmod(0o755)


if __name__ == "__main__":
    build()
