import subprocess
from pathlib import Path


def build(source: Path, destination: Path) -> None:
    subprocess.run(["npm", "ci"], cwd=source, check=True)
    subprocess.run(["npx", "quartz", "plugin", "install"], cwd=source, check=True)
    subprocess.run(
        ["npx", "quartz", "build", "--output", str(destination)],
        cwd=source,
        check=True,
    )
