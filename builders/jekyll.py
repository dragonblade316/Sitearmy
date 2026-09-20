import os
import subprocess
from pathlib import Path


def build(source: Path, destination: Path) -> None:
    environment = os.environ.copy()
    command = ["jekyll"]

    if (source / "Gemfile").is_file():
        environment["BUNDLE_PATH"] = str(source.parent / "bundle")
        subprocess.run(["bundle", "install"], cwd=source, env=environment, check=True)
        command = ["bundle", "exec", "jekyll"]

    subprocess.run(
        command + ["build", "--source", str(source), "--destination", str(destination)],
        cwd=source,
        env=environment,
        check=True,
    )
