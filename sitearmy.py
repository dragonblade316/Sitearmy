import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path

from builders import BUILDERS

CONFIG_PATH = Path("/etc/sitearmy/sites.toml")
OUTPUT_ROOT = Path("/srv/sitearmy")
CADDYFILE_PATH = Path("/run/sitearmy/Caddyfile")
HOSTNAME_PATTERN = re.compile(
    r"^(?:\*\.)?(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$"
)


def https_enabled() -> bool:
    value = os.environ.get("SITEARMY_HTTPS", "true").lower()
    if value == "true":
        return True
    if value == "false":
        return False
    raise ValueError("SITEARMY_HTTPS must be true or false")


def load_sites() -> dict[str, dict[str, str]]:
    with CONFIG_PATH.open("rb") as config_file:
        sites = tomllib.load(config_file)

    if not sites:
        raise ValueError("configuration contains no sites")

    for hostname, site in sites.items():
        if not HOSTNAME_PATTERN.fullmatch(hostname):
            raise ValueError(f"invalid hostname: {hostname}")
        if not isinstance(site, dict):
            raise ValueError(f"configuration for {hostname} must be a table")
        if not isinstance(site.get("git"), str) or not site["git"]:
            raise ValueError(f"configuration for {hostname} requires a git URL")
        if site.get("builder") not in BUILDERS:
            choices = ", ".join(BUILDERS)
            raise ValueError(f"builder for {hostname} must be one of: {choices}")
        root = site.get("root", ".")
        if not isinstance(root, str) or not root:
            raise ValueError(f"root for {hostname} must be a non-empty path")
        if Path(root).is_absolute():
            raise ValueError(f"root for {hostname} must be relative to the repository")

    return sites


def build_sites(sites: dict[str, dict[str, str]]) -> None:
    shutil.rmtree(OUTPUT_ROOT, ignore_errors=True)
    OUTPUT_ROOT.mkdir(parents=True)

    with tempfile.TemporaryDirectory(prefix="sitearmy-") as workspace:
        workspace_path = Path(workspace)
        for index, (hostname, site) in enumerate(sites.items()):
            source = workspace_path / f"source-{index}"
            destination = OUTPUT_ROOT / str(index)

            print(f"Building {hostname} with {site['builder']}", flush=True)
            git_command = ["git"]
            if os.environ.get("GITHUB_TOKEN"):
                git_command.extend(
                    ["-c", "credential.helper=/opt/sitearmy/github-credential-helper"]
                )

            subprocess.run(
                git_command
                + [
                    "clone",
                    "--depth",
                    "1",
                    "--recurse-submodules",
                    "--shallow-submodules",
                    "--",
                    site["git"],
                    str(source),
                ],
                check=True,
            )
            configured_root = site.get("root", ".")
            site_root = (source / configured_root).resolve()
            if not site_root.is_relative_to(source.resolve()):
                raise ValueError(f"root for {hostname} must be within the repository")
            if not site_root.is_dir():
                raise ValueError(f"root for {hostname} is not a directory: {configured_root}")

            BUILDERS[site["builder"]](site_root, destination)


def write_caddyfile(sites: dict[str, dict[str, str]], use_https: bool) -> None:
    blocks = []
    for index, (hostname, site) in enumerate(sites.items()):
        address = hostname if use_https else f"http://{hostname}"
        quartz_routes = (
            "\ttry_files {path} {path}.html {path}/ =404\n"
            if site["builder"] == "quartz"
            else ""
        )
        blocks.append(
            f"{address} {{\n"
            f"\troot * {OUTPUT_ROOT / str(index)}\n"
            f"{quartz_routes}"
            "\tfile_server\n"
            "}"
        )

    CADDYFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE_PATH.write_text("\n\n".join(blocks) + "\n")


def main() -> None:
    sites = load_sites()
    build_sites(sites)
    write_caddyfile(sites, https_enabled())

    subprocess.run(
        ["caddy", "validate", "--config", str(CADDYFILE_PATH), "--adapter", "caddyfile"],
        check=True,
    )
    os.execvp(
        "caddy",
        ["caddy", "run", "--config", str(CADDYFILE_PATH), "--adapter", "caddyfile"],
    )


if __name__ == "__main__":
    main()
