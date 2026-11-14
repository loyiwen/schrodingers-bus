"""A short fingerprint of the deployed code, so every result shows which version produced it."""
import hashlib
from pathlib import Path

# The files that are deployed and change behaviour. Tests and settings aren't deployed
DEPLOYED = ("function_app.py", "batch.py", "reliability.py", "version.py", "host.json", "requirements.txt")


def code_version(folder=Path(__file__).parent):
    """Return the first 12 hex digits of a hash over the deployed files' names and contents."""
    digest = hashlib.sha256()
    for name in DEPLOYED:
        # Line endings are normalised, so a Windows checkout gives the same fingerprint
        content = (Path(folder) / name).read_bytes().replace(b"\r\n", b"\n")
        digest.update(name.encode() + b"\0" + content + b"\0")
    return digest.hexdigest()[:12]


if __name__ == "__main__":
    print(code_version())
