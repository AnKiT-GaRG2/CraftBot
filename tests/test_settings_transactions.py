"""Concurrent settings changes compose, and readers only see complete JSON."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError

import pytest

from app import config, settings_store
from app.ui_layer.settings import (
    memory_settings,
    model_settings,
    proactive_settings,
    provider_settings,
)
from craftos_integrations.config import ConfigStore
from craftos_integrations.credentials_store import save_credential
from craftos_integrations.llm_oauth import claude_code


@pytest.fixture
def settings_path(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps({"model": {"llm_provider": "anthropic"}, "sentinel": "keep"})
    )
    for module in (
        config,
        model_settings,
        provider_settings,
        memory_settings,
        proactive_settings,
    ):
        monkeypatch.setattr(module, "SETTINGS_CONFIG_PATH", path)
    monkeypatch.setattr(ConfigStore, "project_root", tmp_path)
    monkeypatch.setattr(config, "_settings_cache", None)
    return path


@pytest.mark.parametrize(
    "other", ["model", "auth", "key", "memory", "proactive", "language"]
)
def test_overlapping_settings_mutations_preserve_both_changes(
    settings_path, monkeypatch, other
):
    first_read = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()
    real_load = model_settings._load_settings

    def paused_load():
        snapshot = real_load()
        if not first_read.is_set():
            first_read.set()
            assert release_first.wait(5)
        return snapshot

    monkeypatch.setattr(model_settings, "_load_settings", paused_load)

    def second_change():
        second_started.set()
        if other == "model":
            return model_settings.update_model_settings(
                vlm_model="new-vlm", include_subscription_status=False
            )
        if other == "auth":
            return provider_settings._persist_auth_mode("anthropic", "subscription")
        if other == "key":
            return provider_settings.save_settings_to_json("anthropic", "fake-key")
        if other == "memory":
            return memory_settings.set_memory_processing_threshold(25)
        if other == "proactive":
            return proactive_settings.set_proactive_mode(False)
        return config.set_ui_language("fr")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            model_settings.update_model_settings,
            llm_model="new-llm",
            include_subscription_status=False,
        )
        assert first_read.wait(5)
        second = pool.submit(second_change)
        try:
            assert second_started.wait(5)
            # The second transaction cannot finish while the first holds its snapshot.
            with pytest.raises(FutureTimeoutError):
                second.result(timeout=0.1)
        finally:
            release_first.set()
        assert first.result(timeout=5)["success"]
        second.result(timeout=5)

    saved = json.loads(settings_path.read_text())
    assert saved["sentinel"] == "keep"
    assert saved["model"]["llm_model"] == "new-llm"
    expected = {
        "model": ("model", "vlm_model", "new-vlm"),
        "auth": ("auth_mode", "anthropic", "subscription"),
        "key": ("api_keys", "anthropic", "fake-key"),
        "memory": ("memory", "processing_threshold", 25),
        "proactive": ("proactive", "enabled", False),
        "language": ("general", "ui_language", "fr"),
    }
    section, key, value = expected[other]
    assert saved[section][key] == value


def test_subscription_status_does_not_hold_the_settings_lock(
    settings_path, monkeypatch
):
    save_credential(
        claude_code.CRED_FILE,
        claude_code.ClaudeCodeConnection(profile_version=claude_code.PROFILE_VERSION),
    )
    checking_status = threading.Event()
    release_status = threading.Event()

    def slow_status():
        checking_status.set()
        assert release_status.wait(5)
        return {"connected": True}

    monkeypatch.setattr(claude_code, "auth_status", slow_status)
    with ThreadPoolExecutor(max_workers=2) as pool:
        saving = pool.submit(model_settings.update_model_settings, llm_model="new-llm")
        assert checking_status.wait(5)
        try:
            # Auth-mode persistence must not wait for the CLI's ten-second timeout.
            pool.submit(
                provider_settings._persist_auth_mode, "anthropic", "subscription"
            ).result(timeout=2)
        finally:
            release_status.set()
        assert saving.result(timeout=5)["success"]
    saved = json.loads(settings_path.read_text())
    assert saved["model"]["llm_model"] == "new-llm"
    assert saved["auth_mode"]["anthropic"] == "subscription"


def test_failed_atomic_replace_preserves_the_previous_settings(
    settings_path, monkeypatch
):
    previous = settings_path.read_bytes()

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(settings_store.os, "replace", fail)
    assert not model_settings.update_model_settings(
        llm_model="new-llm", include_subscription_status=False
    )["success"]
    assert settings_path.read_bytes() == previous
    assert list(settings_path.parent.glob(".settings.json.*")) == []


def test_concurrent_readers_never_observe_partial_json(settings_path):
    stop = threading.Event()
    observed = threading.Event()

    def read():
        while not stop.is_set():
            data = model_settings._load_settings()
            assert data["sentinel"] == "keep"
            observed.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        reader = pool.submit(read)
        try:
            assert observed.wait(5)
            for i in range(25):
                assert model_settings.update_model_settings(
                    llm_model=f"model-{i}", include_subscription_status=False
                )["success"]
        finally:
            stop.set()
        reader.result(timeout=5)
