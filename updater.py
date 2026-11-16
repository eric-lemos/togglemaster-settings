"""Create or update repository-level GitHub Actions variables and secrets."""

import argparse
import base64
import binascii
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from dotenv import dotenv_values
from nacl.public import PublicKey, SealedBox


ROOT = Path(__file__).resolve().parent
NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


class UpdateError(Exception):
    """An error that can be reported without revealing configuration values."""


@dataclass(repr=False)
class RepositorySettings:
    name: str
    variables: dict[str, str]
    secrets: dict[str, str]


def unique_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise UpdateError("settings.json contains a duplicate JSON key.")
        result[key] = value
    return result


def resolve_value(
    value: object, environment: Mapping[str, str], label: str, secret: bool
) -> str | None:
    if isinstance(value, dict) and set(value) == {"env"}:
        reference = value["env"]
        if not isinstance(reference, str) or not NAME.fullmatch(reference):
            raise UpdateError(f"{label}: invalid environment reference.")
        resolved = environment.get(reference)
        if resolved is None or not resolved.strip():
            return None
        return resolved
    if not secret and isinstance(value, str):
        return value if value.strip() else None
    raise UpdateError(f'{label}: use {{"env": "NAME"}}; only vars accept literal strings.')


def load_settings(
    path: Path, environment: Mapping[str, str], selected: Sequence[str]
) -> list[RepositorySettings]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_keys)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise UpdateError("Cannot read settings: provide a valid UTF-8 JSON file.") from None
    if not isinstance(data, dict) or not data:
        raise UpdateError("Settings must be a non-empty object keyed by owner/repository.")
    if set(selected) - data.keys():
        raise UpdateError("A selected repository is not present in settings.json.")

    repositories: list[RepositorySettings] = []
    for repository, config in data.items():
        if not REPOSITORY.fullmatch(repository):
            raise UpdateError("Repository keys must use the owner/repository format.")
        if selected and repository not in selected:
            continue
        if not isinstance(config, dict) or set(config) != {"vars", "secrets"}:
            raise UpdateError(f"{repository}: exactly vars and secrets are required.")
        groups: dict[str, dict[str, str]] = {}
        for group in ("vars", "secrets"):
            entries = config[group]
            if not isinstance(entries, dict):
                raise UpdateError(f"{repository}/{group}: expected an object.")
            values: dict[str, str] = {}
            for name, value in entries.items():
                if (
                    not NAME.fullmatch(name)
                    or name.upper().startswith("GITHUB_")
                    or name.upper() in values
                ):
                    raise UpdateError(f"{repository}/{group}: invalid or repeated Actions name.")
                label = f"{repository}/{group}/{name}"
                resolved = resolve_value(
                    value, environment, label, secret=group == "secrets"
                )
                if resolved is not None:
                    values[name.upper()] = resolved
            groups[group] = values
        repositories.append(
            RepositorySettings(repository, groups["vars"], groups["secrets"])
        )
    return repositories


def read_environment(path: Path) -> dict[str, str]:
    try:
        local = dotenv_values(path, encoding="utf-8-sig", interpolate=False) if path.is_file() else {}
    except (OSError, UnicodeError):
        raise UpdateError("Cannot read the .env file.") from None
    environment = {name: value for name, value in local.items() if value is not None}
    environment.update(os.environ)
    return environment


class GitHub:
    def __init__(self, token: str) -> None:
        self.token = token

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, str] | None = None,
        *,
        allow_not_found: bool = False,
    ) -> dict[str, object] | None:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        request = Request(
            f"https://api.github.com{path}",
            data=body,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/vnd.github+json",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "togglemaster-settings",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read()
        except HTTPError as error:
            status = error.code
            error.close()
            if allow_not_found and status == 404:
                return None
            raise UpdateError(
                f"GitHub HTTP {status} during {method} {path}. "
                "Check token permissions, repository access and API limits."
            ) from None
        except (URLError, TimeoutError, OSError):
            raise UpdateError(f"Network failure during {method} {path}.") from None
        if not content:
            return {}
        try:
            result = json.loads(content)
        except (UnicodeError, json.JSONDecodeError):
            raise UpdateError("GitHub returned an invalid JSON response.") from None
        if not isinstance(result, dict):
            raise UpdateError("GitHub returned an unexpected response shape.")
        return result

    def set_variable(self, repository: str, name: str, value: str) -> None:
        collection = f"/repos/{repository}/actions/variables"
        existing = self.request("GET", f"{collection}/{name}", allow_not_found=True)
        if existing is None:
            self.request("POST", collection, {"name": name, "value": value})
        else:
            self.request("PATCH", f"{collection}/{name}", {"name": name, "value": value})

    def public_key(self, repository: str) -> tuple[str, PublicKey]:
        data = self.request("GET", f"/repos/{repository}/actions/secrets/public-key")
        if not data:
            raise UpdateError("GitHub returned an invalid public key response.")
        key_id = data.get("key_id")
        encoded_key = data.get("key")
        if not isinstance(key_id, str) or not isinstance(encoded_key, str):
            raise UpdateError("GitHub returned an invalid public key response.")
        try:
            key = PublicKey(base64.b64decode(encoded_key, validate=True))
        except (ValueError, binascii.Error):
            raise UpdateError("GitHub returned an invalid encryption key.") from None
        return key_id, key

    def set_secret(
        self, repository: str, name: str, value: str, key_id: str, key: PublicKey
    ) -> None:
        encrypted = SealedBox(key).encrypt(value.encode("utf-8"))
        self.request(
            "PUT",
            f"/repos/{repository}/actions/secrets/{name}",
            {"encrypted_value": base64.b64encode(encrypted).decode("ascii"), "key_id": key_id},
        )


def synchronize(repositories: Sequence[RepositorySettings], github: GitHub | None) -> None:
    mode = "APPLY" if github else "DRY-RUN"
    completed = 0
    for repository in repositories:
        key = github.public_key(repository.name) if github and repository.secrets else None
        for name, value in repository.variables.items():
            if github:
                github.set_variable(repository.name, name, value)
            print(f"[{mode}] {repository.name} variable {name}", flush=True)
            completed += 1
        for name, value in repository.secrets.items():
            if github and key:
                github.set_secret(repository.name, name, value, *key)
            print(f"[{mode}] {repository.name} secret {name}", flush=True)
            completed += 1
    print(f"{mode}: {completed} settings {'applied' if github else 'validated; no requests sent'}.")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--settings", type=Path, default=ROOT / "settings.json")
    parser.add_argument("--env-file", type=Path, default=ROOT / ".env")
    parser.add_argument("--repo", action="append", default=[], help="Select owner/repository; repeatable.")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--apply", action="store_true", help="Write settings to GitHub.")
    modes.add_argument("--dry-run", action="store_true", help="Validate only (default).")
    args = parser.parse_args(argv)
    try:
        environment = read_environment(args.env_file)
        repositories = load_settings(args.settings, environment, args.repo)
        if args.apply and not any(
            repository.variables or repository.secrets for repository in repositories
        ):
            print("APPLY: no non-empty settings; skipped without GitHub requests.")
            return 0
        github = None
        if args.apply:
            token = environment.get("GITHUB_TOKEN", "").strip()
            if not token:
                raise UpdateError("GITHUB_TOKEN is required for --apply.")
            github = GitHub(token)
        synchronize(repositories, github)
    except UpdateError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        print(
            "Stopped. Earlier successful writes, if any, remain on GitHub; no rollback was performed.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
