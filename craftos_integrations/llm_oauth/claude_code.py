"""Claude subscription access through the unmodified Claude Code CLI.

CraftBot stores a versioned opt-in flag. Claude Code owns login, tokens and
refresh inside CraftBot's private CLI config directory. The standalone CLI
login is never read, copied or signed out.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Tuple

from ..config import ConfigStore
from ..credentials_store import load_credential, remove_credential, save_credential

CRED_FILE = "anthropic_claude_code.json"
PROFILE_VERSION = 1
INSTALL_MESSAGE = (
    "Claude Code CLI is required. Install it from "
    "https://code.claude.com/docs/en/setup, then connect again."
)


@dataclass
class ClaudeCodeConnection:
    enabled: bool = True
    # Old markers borrowed the standalone CLI login. Never adopt them into
    # the new profile without a separate login through CraftBot.
    profile_version: int = 0


def profile_directory() -> Path:
    """Private, stable auth directory for this CraftBot installation."""
    path = (ConfigStore.project_root / ".credentials" / "claude_code").resolve()
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path


def login_command() -> str:
    """Manual recovery command targeting the same profile as the app."""
    path = str(profile_directory())
    if os.name == "nt":
        return (
            "$env:CLAUDE_CONFIG_DIR = '"
            + path.replace("'", "''")
            + "'; claude auth login"
        )
    return f"CLAUDE_CONFIG_DIR={shlex.quote(path)} claude auth login"


def cli_command() -> list[str]:
    """Resolve a native CLI or npm entrypoint without passing through a shell."""
    command = shutil.which("claude")
    if not command:
        raise RuntimeError(INSTALL_MESSAGE)
    path = Path(command)
    if path.suffix.lower() in (".cmd", ".bat", ".ps1"):
        # Windows npm shims invoke a shell, which can reinterpret arguments.
        # Execute the same published JS entrypoint directly with Node instead.
        entrypoint = path.parent / "node_modules/@anthropic-ai/claude-code/cli.js"
        node = shutil.which("node")
        if node and entrypoint.is_file():
            return [node, str(entrypoint)]
        raise RuntimeError(INSTALL_MESSAGE)
    return [command]


def cli_environment() -> Dict[str, str]:
    """Always use CraftBot's CLI profile, never the standalone CLI account."""
    env = dict(os.environ)
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
    ):
        env.pop(name, None)
    env["CLAUDE_CONFIG_DIR"] = str(profile_directory())
    return env


def run_cli(args: list[str], *, timeout: float, env_overrides=None, **kwargs):
    env = cli_environment()
    env.update(env_overrides or {})
    # Request-specific generation settings must not redirect authentication.
    env["CLAUDE_CONFIG_DIR"] = str(profile_directory())
    return subprocess.run(
        [*cli_command(), *args],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        **kwargs,
    )


def _read_auth_status() -> Dict[str, Any]:
    result = run_cli(["auth", "status"], timeout=10)
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("loggedIn"), bool):
        raise ValueError("Invalid authentication status")
    if result.returncode not in (0, 1) or (result.returncode and data["loggedIn"]):
        raise RuntimeError("Claude Code authentication check failed")
    return data


def auth_status() -> Dict[str, Any]:
    try:
        data = _read_auth_status()
        if not data["loggedIn"]:
            return {
                "connected": False,
                "error": "Use Connect to sign in to CraftBot's separate Claude profile.",
            }
        if data.get("authMethod") != "claude.ai":
            return {
                "connected": False,
                "error": "A Claude subscription login is required. Use Connect in CraftBot.",
            }
        return {
            "connected": True,
            "email": data.get("email") or "",
            "plan": data.get("subscriptionType") or "Claude Code",
        }
    except subprocess.TimeoutExpired:
        return {
            "connected": False,
            "error": "Claude Code authentication check timed out.",
        }
    except (OSError, RuntimeError, ValueError):
        return {
            "connected": False,
            "error": "Could not check Claude Code login. " + INSTALL_MESSAGE,
        }


def has_credential() -> bool:
    connection = load_credential(CRED_FILE, ClaudeCodeConnection)
    return (
        connection is not None
        and connection.enabled
        and connection.profile_version == PROFILE_VERSION
    )


def status() -> Dict[str, Any]:
    if not has_credential():
        return {"supported": True, "connected": False}
    return {"supported": True, **auth_status()}


def _login() -> Tuple[bool, str]:
    current = auth_status()
    if not current["connected"]:
        try:
            result = run_cli(["auth", "login"], timeout=300)
        except subprocess.TimeoutExpired:
            return (
                False,
                f"Claude sign-in timed out. Retry Connect, or run: {login_command()}",
            )
        except (OSError, RuntimeError):
            return False, INSTALL_MESSAGE
        if result.returncode:
            return (
                False,
                f"Claude sign-in failed. Retry Connect, or run: {login_command()}",
            )
        current = auth_status()
    if not current["connected"]:
        return False, str(current.get("error", "Claude Code sign-in failed."))
    save_credential(CRED_FILE, ClaudeCodeConnection(profile_version=PROFILE_VERSION))
    if not has_credential():
        return False, "Could not save the Claude Code connection."
    return (
        True,
        "Claude subscription connected using CraftBot's separate Claude profile.",
    )


async def run_login() -> Tuple[bool, str]:
    # subprocess.run also works with Windows selector event loops.
    return await asyncio.to_thread(_login)


def remove() -> Tuple[bool, str]:
    """Sign out only CraftBot's official CLI profile, then clear its marker."""
    try:
        result = run_cli(["auth", "logout"], timeout=30)
        if result.returncode:
            return (
                False,
                "Claude sign-out failed. Retry Disconnect; the connection was retained.",
            )
        if _read_auth_status()["loggedIn"]:
            return (
                False,
                "Claude Code is still signed in to CraftBot's profile. Retry Disconnect.",
            )
    except subprocess.TimeoutExpired:
        return False, "Claude sign-out or verification timed out. Retry Disconnect."
    except (OSError, RuntimeError, ValueError):
        return (
            False,
            "Could not verify Claude sign-out. Check the CLI installation and retry Disconnect.",
        )
    try:
        remove_credential(CRED_FILE)
    except OSError:
        return (
            False,
            "Claude signed out, but CraftBot could not clear the connection. Retry Disconnect.",
        )
    return (
        True,
        "Signed out of CraftBot's Claude profile. Your standalone Claude Code login is unchanged.",
    )
