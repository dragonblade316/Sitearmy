import shutil
from pathlib import Path


def build(source: Path, destination: Path) -> None:
    shutil.copytree(source, destination, ignore=shutil.ignore_patterns(".git"))
