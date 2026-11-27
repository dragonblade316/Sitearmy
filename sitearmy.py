import hashlib
import hmac
import json
import os
import re
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
DATA_ROOT = Path("/data/sitearmy")
OUTPUT_ROOT = DATA_ROOT / "sites"
BUILD_STATE_PATH = DATA_ROOT / "state.json"
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


def site_output_path(hostname: str) -> Path:
    return OUTPUT_ROOT / hostname.replace("*", "_wildcard_")


def site_fingerprint(site: dict[str, str]) -> str:
    configuration = {
        "git": site["git"],
        "builder": site["builder"],
        "root": site.get("root", "."),
    }
    content = json.dumps(configuration, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(content.encode()).hexdigest()


def load_build_state() -> dict[str, dict[str, str | None]]:
    try:
        state = json.loads(BUILD_STATE_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as error:
        print(f"Ignoring invalid build state: {error}", flush=True)
        return {}

    sites = state.get("sites") if isinstance(state, dict) else None
    if not isinstance(sites, dict):
        return {}
    return {
        hostname: entry
        for hostname, entry in sites.items()
        if isinstance(hostname, str) and isinstance(entry, dict)
    }


def save_build_state(state: dict[str, dict[str, str | None]]) -> None:
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    temporary_path = BUILD_STATE_PATH.with_suffix(".tmp")
    temporary_path.write_text(json.dumps({"sites": state}, indent=2) + "\n")
    temporary_path.replace(BUILD_STATE_PATH)


def valid_cached_build(
    hostname: str,
    site: dict[str, str],
    entry: dict[str, str | None] | None,
) -> bool:
    return bool(
        entry
        and isinstance(entry.get("deployed_commit"), str)
        and entry.get("config_fingerprint") == site_fingerprint(site)
        and site_output_path(hostname).is_dir()
    )


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


def initialize_sites(
    sites: dict[str, dict[str, str]],
) -> tuple[
    dict[str, dict[str, str | None]],
    dict[str, dict[str, str | None]],
]:
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    stored_state = load_build_state()
    build_state = {
        hostname: stored_state[hostname]
        for hostname, site in sites.items()
        if valid_cached_build(hostname, site, stored_state.get(hostname))
    }
    statuses = {}

    for hostname, site in sites.items():
        cached = build_state.get(hostname)
        deployed_commit = cached.get("deployed_commit") if cached else None
        statuses[hostname] = {
            "builder": site["builder"],
            "state": "checking",
            "deployed_commit": deployed_commit,
            "remote_commit": deployed_commit,
            "last_checked_at": None,
            "last_built_at": cached.get("last_built_at") if cached else None,
            "error": None,
        }

        if cached:
            try:
                latest_commit = remote_commit(site["git"])
                statuses[hostname].update(
                    remote_commit=latest_commit,
                    last_checked_at=current_time(),
                )
                if latest_commit == deployed_commit:
                    statuses[hostname]["state"] = "ready"
                    continue
            except Exception as error:
                print(f"Failed to check {hostname}: {error}", flush=True)
                statuses[hostname].update(
                    state="failed",
                    last_checked_at=current_time(),
                    error=str(error),
                )
                continue

        try:
            statuses[hostname]["state"] = "building"
            commit = build_site(hostname, site, site_output_path(hostname))
        except Exception as error:
            if not cached:
                raise
            print(f"Failed to update {hostname}: {error}", flush=True)
            statuses[hostname].update(
                state="failed",
                last_checked_at=current_time(),
                error=str(error),
            )
            continue

        timestamp = current_time()
        statuses[hostname].update(
            state="ready",
            deployed_commit=commit,
            remote_commit=commit,
            last_checked_at=timestamp,
            last_built_at=timestamp,
        )
        build_state[hostname] = {
            "config_fingerprint": site_fingerprint(site),
            "deployed_commit": commit,
            "last_built_at": timestamp,
        }
        save_build_state(build_state)

    save_build_state(build_state)
    return statuses, build_state


class SiteManager:
    def __init__(
        self,
        sites: dict[str, dict[str, str]],
        statuses: dict[str, dict[str, str | None]],
        build_state: dict[str, dict[str, str | None]],
    ) -> None:
        self.sites = sites
        self.statuses = statuses
        self.build_state = build_state
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

    def record_build(self, hostname: str, commit: str) -> None:
        timestamp = current_time()
        with self.lock:
            self.statuses[hostname].update(
                state="ready",
                deployed_commit=commit,
                remote_commit=commit,
                last_built_at=timestamp,
            )
            self.build_state[hostname] = {
                "config_fingerprint": site_fingerprint(self.sites[hostname]),
                "deployed_commit": commit,
                "last_built_at": timestamp,
            }
            state = {
                site_hostname: dict(entry)
                for site_hostname, entry in self.build_state.items()
            }
        save_build_state(state)

    def update_sites(self) -> None:
        try:
            for hostname, site in self.sites.items():
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
                        site_output_path(hostname),
                    )
                    self.record_build(hostname, deployed_commit)
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
    for hostname, site in sites.items():
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
            f"\t\troot * {site_output_path(hostname)}\n"
            f"{quartz_routes}"
            "\t\tfile_server\n"
            "\t}\n"
            "}"
        )

    CADDYFILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CADDYFILE_PATH.write_text("\n\n".join(blocks) + "\n")


def main() -> None:
    sites, api_token = load_config()
    statuses, build_state = initialize_sites(sites)
    write_caddyfile(sites, https_enabled())

    subprocess.run(
        ["caddy", "validate", "--config", str(CADDYFILE_PATH), "--adapter", "caddyfile"],
        check=True,
    )

    manager = SiteManager(sites, statuses, build_state)
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
