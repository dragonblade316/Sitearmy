import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import tomllib
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from builders import BUILDERS

CONFIG_PATH = Path("/etc/sitearmy/sites.toml")
OUTPUT_ROOT = Path("/srv/sitearmy")
CADDYFILE_PATH = Path("/run/sitearmy/Caddyfile")
API_HOST = "127.0.0.1"
API_PORT = 8080
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


def load_config() -> tuple[dict[str, dict[str, str]], str]:
    with CONFIG_PATH.open("rb") as config_file:
        config = tomllib.load(config_file)

    api_token = config.pop("api_token", None)
    if not isinstance(api_token, str) or not api_token:
        raise ValueError("configuration requires a non-empty api_token")

    sites = config
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

    return sites, api_token


def git_command() -> list[str]:
    command = ["git"]
    if os.environ.get("GITHUB_TOKEN"):
        command.extend(
            ["-c", "credential.helper=/opt/sitearmy/github-credential-helper"]
        )
    return command


def current_time() -> str:
    return datetime.now(timezone.utc).isoformat()


def remote_commit(git_url: str) -> str:
    output = subprocess.check_output(
        git_command() + ["ls-remote", "--", git_url, "HEAD"],
        text=True,
    ).strip()
    if not output:
        raise RuntimeError(f"repository returned no HEAD commit: {git_url}")
    return output.split()[0]


def build_site(
    hostname: str,
    site: dict[str, str],
    destination: Path,
) -> str:
    with tempfile.TemporaryDirectory(prefix=".build-", dir=OUTPUT_ROOT) as workspace:
        workspace_path = Path(workspace)
        source = workspace_path / "source"
        output = workspace_path / "output"

        print(f"Building {hostname} with {site['builder']}", flush=True)
        subprocess.run(
            git_command()
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
        commit = subprocess.check_output(
            ["git", "-C", str(source), "rev-parse", "HEAD"],
            text=True,
        ).strip()

        configured_root = site.get("root", ".")
        site_root = (source / configured_root).resolve()
        if not site_root.is_relative_to(source.resolve()):
            raise ValueError(f"root for {hostname} must be within the repository")
        if not site_root.is_dir():
            raise ValueError(
                f"root for {hostname} is not a directory: {configured_root}"
            )

        BUILDERS[site["builder"]](site_root, output)

        previous = workspace_path / "previous"
        if destination.exists():
            destination.rename(previous)
        try:
            output.rename(destination)
        except Exception:
            if previous.exists():
                previous.rename(destination)
            raise

    return commit


def build_sites(
    sites: dict[str, dict[str, str]],
) -> dict[str, dict[str, str | None]]:
    shutil.rmtree(OUTPUT_ROOT, ignore_errors=True)
    OUTPUT_ROOT.mkdir(parents=True)
    statuses = {}

    for index, (hostname, site) in enumerate(sites.items()):
        commit = build_site(hostname, site, OUTPUT_ROOT / str(index))
        timestamp = current_time()
        statuses[hostname] = {
            "builder": site["builder"],
            "state": "ready",
            "deployed_commit": commit,
            "remote_commit": commit,
            "last_checked_at": timestamp,
            "last_built_at": timestamp,
            "error": None,
        }

    return statuses


class SiteManager:
    def __init__(
        self,
        sites: dict[str, dict[str, str]],
        statuses: dict[str, dict[str, str | None]],
    ) -> None:
        self.sites = sites
        self.statuses = statuses
        self.lock = threading.Lock()
        self.update_in_progress = False

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "update_in_progress": self.update_in_progress,
                "sites": {
                    hostname: dict(status)
                    for hostname, status in self.statuses.items()
                },
            }

    def start_update(self) -> bool:
        with self.lock:
            if self.update_in_progress:
                return False
            self.update_in_progress = True

        threading.Thread(target=self.update_sites, daemon=True).start()
        return True

    def set_status(self, hostname: str, **changes: str | None) -> None:
        with self.lock:
            self.statuses[hostname].update(changes)

    def update_sites(self) -> None:
        try:
            for index, (hostname, site) in enumerate(self.sites.items()):
                self.set_status(hostname, state="checking", error=None)
                print(f"Checking {hostname} for updates", flush=True)

                try:
                    latest_commit = remote_commit(site["git"])
                    checked_at = current_time()
                    self.set_status(
                        hostname,
                        remote_commit=latest_commit,
                        last_checked_at=checked_at,
                    )

                    with self.lock:
                        deployed_commit = self.statuses[hostname]["deployed_commit"]

                    if latest_commit == deployed_commit:
                        self.set_status(hostname, state="ready")
                        continue

                    self.set_status(hostname, state="building")
                    deployed_commit = build_site(
                        hostname,
                        site,
                        OUTPUT_ROOT / str(index),
                    )
                    self.set_status(
                        hostname,
                        state="ready",
                        deployed_commit=deployed_commit,
                        remote_commit=deployed_commit,
                        last_built_at=current_time(),
                    )
                except Exception as error:
                    print(f"Failed to update {hostname}: {error}", flush=True)
                    self.set_status(
                        hostname,
                        state="failed",
                        last_checked_at=current_time(),
                        error=str(error),
                    )
        finally:
            with self.lock:
                self.update_in_progress = False


def api_handler(manager: SiteManager, api_token: str) -> type[BaseHTTPRequestHandler]:
    class ApiHandler(BaseHTTPRequestHandler):
        def authenticated(self) -> bool:
            supplied_token = self.headers.get("X-API-Key", "")
            return hmac.compare_digest(
                supplied_token.encode(),
                api_token.encode(),
            )

        def send_json(self, status: int, body: dict) -> None:
            content = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def authorize(self) -> bool:
            if self.authenticated():
                return True
            self.send_json(401, {"error": "unauthorized"})
            return False

        def do_GET(self) -> None:
            if self.path.partition("?")[0] != "/_sitearmy/status":
                self.send_json(404, {"error": "not found"})
                return
            if not self.authorize():
                return
            self.send_json(200, manager.snapshot())

        def do_POST(self) -> None:
            if self.path.partition("?")[0] != "/_sitearmy/update":
                self.send_json(404, {"error": "not found"})
                return
            if not self.authorize():
                return
            if not manager.start_update():
                self.send_json(409, {"error": "an update is already in progress"})
                return
            self.send_json(202, {"status": "accepted"})

    return ApiHandler


def write_caddyfile(sites: dict[str, dict[str, str]], use_https: bool) -> None:
    blocks = []
    for index, (hostname, site) in enumerate(sites.items()):
        address = hostname if use_https else f"http://{hostname}"
        quartz_routes = (
            "\t\ttry_files {path} {path}.html {path}/ =404\n"
            if site["builder"] == "quartz"
            else ""
        )
        blocks.append(
            f"{address} {{\n"
            "\thandle /_sitearmy/* {\n"
            f"\t\treverse_proxy {API_HOST}:{API_PORT}\n"
            "\t}\n"
            "\thandle {\n"
            f"\t\troot * {OUTPUT_ROOT / str(index)}\n"
            f"{quartz_routes}"
            "\t\tfile_server\n"
            "\t}\n"
            "}"
        )

    CADDYFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE_PATH.write_text("\n\n".join(blocks) + "\n")


def main() -> None:
    sites, api_token = load_config()
    statuses = build_sites(sites)
    write_caddyfile(sites, https_enabled())

    subprocess.run(
        ["caddy", "validate", "--config", str(CADDYFILE_PATH), "--adapter", "caddyfile"],
        check=True,
    )

    manager = SiteManager(sites, statuses)
    server = ThreadingHTTPServer((API_HOST, API_PORT), api_handler(manager, api_token))
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    caddy = subprocess.Popen(
        ["caddy", "run", "--config", str(CADDYFILE_PATH), "--adapter", "caddyfile"]
    )

    def forward_signal(signum: int, _frame: object) -> None:
        if caddy.poll() is None:
            caddy.send_signal(signum)

    signal.signal(signal.SIGTERM, forward_signal)
    signal.signal(signal.SIGINT, forward_signal)

    try:
        return_code = caddy.wait()
    finally:
        server.shutdown()
        server.server_close()

    raise SystemExit(return_code)


if __name__ == "__main__":
    main()
