import subprocess
from pathlib import Path


def build(source: Path, destination: Path) -> None:
    subprocess.run(
        ["hugo", "--source", str(source), "--destination", str(destination)],
        check=True,
    )
