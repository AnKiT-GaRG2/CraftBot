"""Claude subscription routing must never export OAuth tokens or bill an API key."""

import asyncio
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from craftos_integrations.config import ConfigStore
from craftos_integrations.llm_oauth import claude_code, tokens
from agent_core.core.models.claude_code_client import ClaudeCodeClient
from agent_core.core.models.factory import ModelFactory
from agent_core.core.models.types import InterfaceType


@pytest.fixture(autouse=True)
def isolated_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(ConfigStore, "project_root", tmp_path)


def connected(monkeypatch):
    def signed_out_cli(args, **kwargs):
        assert args in (["auth", "logout"], ["auth", "status"])
        return SimpleNamespace(returncode=0, stdout='{"loggedIn": false}')

    monkeypatch.setattr(claude_code, "run_cli", signed_out_cli)
    monkeypatch.setattr(
        claude_code,
        "auth_status",
        lambda: {
            "connected": True,
            "email": "user@example.com",
            "plan": "max",
        },
    )
    assert asyncio.run(tokens.connect("anthropic"))[0]


def test_connection_is_explicit_and_does_not_store_cli_credentials(
    monkeypatch, tmp_path
):
    assert not tokens.has_credential("anthropic")
    assert tokens.status("anthropic") == {"supported": True, "connected": False}
    connected(monkeypatch)
    assert tokens.has_credential("anthropic")
    assert tokens.status("anthropic")["email"] == "user@example.com"
    assert tokens.get_bearer("anthropic") is None
    assert json.loads(
        (tmp_path / ".credentials" / claude_code.CRED_FILE).read_text()
    ) == {
        "enabled": True,
        "profile_version": claude_code.PROFILE_VERSION,
    }


def test_disconnect_logs_out_then_verifies_before_clearing_marker(monkeypatch):
    connected(monkeypatch)
    calls = []

    def logout(args, **kwargs):
        assert tokens.has_credential("anthropic")
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"loggedIn": false}')

    monkeypatch.setattr(claude_code, "run_cli", logout)
    assert tokens.disconnect("anthropic")[0]
    assert calls == [["auth", "logout"], ["auth", "status"]]
    assert not tokens.has_credential("anthropic")


@pytest.mark.parametrize(
    "method", ["api_key", "api_key_helper", "third_party", "oauth_token"]
)
def test_auth_status_rejects_non_subscription_auth(monkeypatch, method):
    monkeypatch.setattr(
        claude_code,
        "run_cli",
        lambda *a, **k: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"loggedIn": True, "authMethod": method}),
        ),
    )
    assert not claude_code.auth_status()["connected"]


def test_failed_login_does_not_enable_subscription(monkeypatch):
    monkeypatch.setattr(claude_code, "auth_status", lambda: {"connected": False})
    monkeypatch.setattr(
        claude_code, "run_cli", lambda *a, **k: SimpleNamespace(returncode=1)
    )
    assert not asyncio.run(tokens.connect("anthropic"))[0]
    assert not tokens.has_credential("anthropic")


def test_browser_login_uses_cli_then_checks_account(monkeypatch):
    states = iter([{"connected": False}, {"connected": True}])
    monkeypatch.setattr(claude_code, "auth_status", lambda: next(states))
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(claude_code, "run_cli", run)
    assert asyncio.run(tokens.connect("anthropic"))[0]
    assert calls == [["auth", "login"]]


def test_login_timeout_is_actionable_and_does_not_enable(monkeypatch):
    monkeypatch.setattr(claude_code, "auth_status", lambda: {"connected": False})

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("claude", 300)

    monkeypatch.setattr(claude_code, "run_cli", timeout)
    success, message = asyncio.run(tokens.connect("anthropic"))
    assert not success and "timed out" in message
    assert not tokens.has_credential("anthropic")


def test_inherited_api_auth_and_nested_session_do_not_override_subscription(
    monkeypatch,
):
    for name in (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDECODE",
    ):
        monkeypatch.setenv(name, "must-not-reach-cli")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/my/claude/config")
    env = claude_code.cli_environment()
    assert "must-not-reach-cli" not in env.values()
    assert env["CLAUDE_CONFIG_DIR"] == str(claude_code.profile_directory())
    assert env["CLAUDE_CONFIG_DIR"] != "/my/claude/config"
    assert claude_code.os.environ["CLAUDE_CONFIG_DIR"] == "/my/claude/config"


def test_factory_uses_cli_before_api_key_and_reverts_after_disconnect(monkeypatch):
    connected(monkeypatch)
    ctx = ModelFactory.create(
        provider="anthropic", interface=InterfaceType.LLM, api_key="paid-key"
    )
    assert isinstance(ctx["anthropic_client"], ClaudeCodeClient)
    assert ctx["auth_mode"] == "subscription"
    tokens.disconnect("anthropic")
    client = object()
    monkeypatch.setattr(
        "agent_core.core.models.factory._create_anthropic_client", lambda **k: client
    )
    ctx = ModelFactory.create(
        provider="anthropic", interface=InterfaceType.LLM, api_key="paid-key"
    )
    assert ctx["anthropic_client"] is client
    assert "auth_mode" not in ctx


def test_request_preserves_text_images_and_usage_without_running_tools(monkeypatch):
    connected(monkeypatch)
    prompt = "system instructions " * 10000
    image = {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
    }
    temporary_paths = []

    def run(args, **kwargs):
        assert "--tools" in args and args[args.index("--tools") + 1] == ""
        assert "--strict-mcp-config" in args
        assert "--no-session-persistence" in args
        assert Path(args[args.index("--system-prompt-file") + 1]).read_text() == prompt
        settings = Path(args[args.index("--settings") + 1])
        assert json.loads(settings.read_text())["disableAllHooks"]
        assert json.loads(Path(args[args.index("--mcp-config") + 1]).read_text()) == {
            "mcpServers": {}
        }
        temporary_paths.append(settings.parent)
        blocks = json.loads(kwargs["input"])["message"]["content"]
        assert image in blocks
        texts = " ".join(b.get("text", "") for b in blocks)
        assert (
            "earlier question" in texts
            and "earlier answer" in texts
            and "latest question" in texts
        )
        assert "[assistant]" in texts
        assert kwargs["env_overrides"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "123"
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "type": "result",
                    "is_error": False,
                    "result": "answer",
                    "usage": {
                        "input_tokens": 20,
                        "output_tokens": 5,
                        "cache_read_input_tokens": 10,
                    },
                }
            ),
        )

    monkeypatch.setattr(claude_code, "run_cli", run)
    result = ClaudeCodeClient().messages.create(
        model="claude-sonnet-4-6",
        max_tokens=123,
        system=[
            {"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}
        ],
        messages=[
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
            {
                "role": "user",
                "content": [image, {"type": "text", "text": "latest question"}],
            },
        ],
    )
    assert result.content[0].text == "answer"
    assert result.usage.input_tokens == 20
    assert result.usage.cache_read_input_tokens == 10
    assert all(not path.exists() for path in temporary_paths)


@pytest.mark.parametrize(
    "stdout,code",
    [
        ("not json", 1),
        (json.dumps({"type": "result", "is_error": True, "result": "rate limited"}), 0),
    ],
)
def test_cli_failure_is_not_silently_billed_to_api_key(monkeypatch, stdout, code):
    connected(monkeypatch)
    monkeypatch.setattr(
        claude_code,
        "run_cli",
        lambda *a, **k: SimpleNamespace(returncode=code, stdout=stdout),
    )
    with pytest.raises(RuntimeError, match="Claude Code"):
        ClaudeCodeClient().create(
            model="sonnet", messages=[{"role": "user", "content": "hi"}]
        )


def test_existing_client_stops_after_disconnect(monkeypatch):
    connected(monkeypatch)
    client = ClaudeCodeClient()
    tokens.disconnect("anthropic")
    monkeypatch.setattr(
        claude_code, "run_cli", lambda *a, **k: pytest.fail("Disconnected")
    )
    with pytest.raises(RuntimeError, match="disconnected"):
        client.create(model="sonnet", messages=[{"role": "user", "content": "hi"}])


def test_settings_allow_connected_claude_without_api_key(monkeypatch):
    from app.ui_layer.settings import model_settings

    monkeypatch.setattr(model_settings, "_load_settings", lambda: {})
    assert not model_settings.validate_can_save("anthropic")["success"]
    connected(monkeypatch)
    assert model_settings.validate_can_save("anthropic")["success"]
    info = next(
        p
        for p in model_settings.get_available_providers()["providers"]
        if p["id"] == "anthropic"
    )
    assert info["supports_subscription_oauth"]
    assert "Claude Code" in info["subscription_label"]


def test_connection_test_uses_selected_cli_model(monkeypatch):
    from agent_core.core.models.connection_tester import test_provider_connection

    connected(monkeypatch)
    calls = []
    monkeypatch.setattr(
        ClaudeCodeClient, "create", lambda self, **kwargs: calls.append(kwargs)
    )
    result = test_provider_connection(
        "anthropic", api_key="unused-paid-key", model="claude-sonnet-4-6"
    )
    assert result["success"]
    assert calls[0]["model"] == "claude-sonnet-4-6"


def test_windows_npm_cli_bypasses_shell(monkeypatch, tmp_path):
    shim = tmp_path / "claude.cmd"
    entry = tmp_path / "node_modules/@anthropic-ai/claude-code/cli.js"
    entry.parent.mkdir(parents=True)
    entry.write_text("// official CLI entrypoint")
    monkeypatch.setattr(
        claude_code.shutil,
        "which",
        lambda name: str(shim) if name == "claude" else "node.exe",
    )
    assert claude_code.cli_command() == ["node.exe", str(entry)]


def test_missing_cli_has_install_instructions(monkeypatch):
    monkeypatch.setattr(claude_code.shutil, "which", lambda name: None)
    success, message = asyncio.run(tokens.connect("anthropic"))
    assert not success and "Install" in message


def test_status_detects_cli_logout_without_exporting_tokens(monkeypatch):
    connected(monkeypatch)
    monkeypatch.setattr(
        claude_code,
        "auth_status",
        lambda: {"connected": False, "error": "Sign in again"},
    )
    assert not tokens.status("anthropic")["connected"]
    assert tokens.get_bearer("anthropic") is None


def test_old_shared_login_marker_requires_new_profile_sign_in(tmp_path):
    from craftos_integrations.credentials_store import save_credential

    save_credential(claude_code.CRED_FILE, claude_code.ClaudeCodeConnection())
    assert not tokens.has_credential("anthropic")
    assert tokens.status("anthropic") == {"supported": True, "connected": False}
    assert not (tmp_path / ".credentials" / "claude_code").exists()


def test_all_cli_commands_force_same_private_profile(monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/standalone/profile")
    monkeypatch.setattr(claude_code, "cli_command", lambda: ["claude"])
    invocations = []

    def subprocess_run(args, **kwargs):
        invocations.append((args, kwargs["env"]["CLAUDE_CONFIG_DIR"]))
        return SimpleNamespace(returncode=0, stdout='{"loggedIn": false}')

    monkeypatch.setattr(claude_code.subprocess, "run", subprocess_run)
    for args in (
        ["auth", "login"],
        ["auth", "status"],
        ["auth", "logout"],
        ["--print"],
    ):
        claude_code.run_cli(
            args, timeout=10, env_overrides={"CLAUDE_CONFIG_DIR": "/wrong/profile"}
        )
    assert len(invocations) == 4
    assert {path for _, path in invocations} == {str(claude_code.profile_directory())}
    assert claude_code.os.environ["CLAUDE_CONFIG_DIR"] == "/standalone/profile"


def test_profiles_are_scoped_to_craftbot_installation(monkeypatch, tmp_path):
    first = claude_code.profile_directory()
    monkeypatch.setattr(ConfigStore, "project_root", tmp_path / "another-installation")
    assert claude_code.profile_directory() != first


@pytest.mark.parametrize(
    "failure", ["exit", "timeout", "still_logged_in", "invalid_status"]
)
def test_failed_logout_keeps_connection_and_auth_mode(monkeypatch, failure):
    from app.ui_layer.settings import provider_settings

    connected(monkeypatch)
    modes = []
    monkeypatch.setattr(
        provider_settings, "_persist_auth_mode", lambda *args: modes.append(args)
    )

    def fail(args, **kwargs):
        if args == ["auth", "logout"]:
            if failure == "timeout":
                raise subprocess.TimeoutExpired("claude", 30)
            return SimpleNamespace(returncode=1 if failure == "exit" else 0)
        return SimpleNamespace(
            returncode=0,
            stdout='{"loggedIn": true}' if failure == "still_logged_in" else "invalid",
        )

    monkeypatch.setattr(claude_code, "run_cli", fail)
    success, message = provider_settings.disconnect_subscription("anthropic")
    assert not success and message
    assert tokens.has_credential("anthropic")
    assert modes == []


def test_reconnect_after_logout_starts_new_login(monkeypatch):
    calls = []
    logged_in = False

    def cli(args, **kwargs):
        nonlocal logged_in
        calls.append(args)
        if args == ["auth", "login"]:
            logged_in = True
        elif args == ["auth", "logout"]:
            logged_in = False
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "loggedIn": logged_in,
                    "authMethod": "claude.ai",
                }
            ),
        )

    monkeypatch.setattr(claude_code, "run_cli", cli)
    assert asyncio.run(tokens.connect("anthropic"))[0]
    assert tokens.disconnect("anthropic")[0]
    assert not tokens.has_credential("anthropic")
    assert asyncio.run(tokens.connect("anthropic"))[0]
    assert calls.count(["auth", "login"]) == 2


def test_manual_login_command_targets_craftbot_profile():
    command = claude_code.login_command()
    assert "CLAUDE_CONFIG_DIR" in command
    assert "claude auth login" in command
    assert "claude_code" in command
