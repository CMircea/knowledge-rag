"""The file watcher skips the directories that indexing skips.

One recursive watch covers ``documents_dir``. Without pruning, watchdog puts
an OS watch on every ``.git``, ``node_modules`` or build directory below it,
which can exhaust inotify limits on large trees. With
``documents.watch_excluded_dirs: false`` (the default) the watch receives a
``directory_filter`` built from the same predicate and base directory as
``DocumentParser.iter_files``, when the installed watchdog accepts one.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from mcp_server import server

PATTERNS = [".git", "node_modules", "docs/drafts"]


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    root = tmp_path / "documents"
    for sub in (".git/objects", "node_modules/pkg", "src/node_modules", "docs/drafts/old", "docs/guides", "src/app"):
        (root / sub).mkdir(parents=True)
    return root


@pytest.fixture
def watch_config(monkeypatch, docs: Path) -> Path:
    monkeypatch.setattr(server.config, "documents_dir", docs)
    monkeypatch.setattr(server.config, "exclude_patterns", list(PATTERNS))
    monkeypatch.setattr(server.config, "watch_excluded_dirs", False)
    return docs


class _FilterObserver:
    """Stand-in for an observer from a watchdog release with ``directory_filter``."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def schedule(self, event_handler, path, *, recursive=False, event_filter=None, directory_filter=None):
        self.calls.append(((event_handler, path), {"recursive": recursive, "directory_filter": directory_filter}))


class _LegacyObserver:
    """Stand-in for watchdog 7.1.0 and older, whose ``schedule`` has no ``directory_filter``."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def schedule(self, event_handler, path, *, recursive=False, event_filter=None, follow_symlink=False):
        self.calls.append(((event_handler, path), {"recursive": recursive}))


def _watcher_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if line.startswith("[WATCHER]")]


# -------------------------------------------------------------- predicate


def test_directory_filter_prunes_excluded_dirs_and_keeps_the_rest(docs: Path) -> None:
    watch = server._watch_directory_filter(docs, PATTERNS)

    pruned = [".git", "node_modules", "src/node_modules", "docs/drafts"]
    kept = ["src", "src/app", "docs", "docs/guides"]
    assert [d for d in pruned if watch(str(docs / d))] == []
    assert [d for d in kept if not watch(str(docs / d))] == []


def test_directory_filter_prunes_a_link_that_leaves_documents_dir(tmp_path: Path, docs: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        os.symlink(outside, docs / "escape", target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"cannot create a directory symlink: {exc}")

    watch = server._watch_directory_filter(docs, [])

    assert watch(str(docs / "escape")) is False
    assert watch(str(docs / "src")) is True


def test_directory_filter_matches_resolved_paths_against_the_resolved_root(tmp_path: Path) -> None:
    """A resolved path must not be matched as an absolute path (``build`` is an ancestor here)."""
    real = tmp_path / "build" / "documents"
    (real / "notes").mkdir(parents=True)
    (real / "docs" / "drafts").mkdir(parents=True)
    link = tmp_path / "link"
    try:
        os.symlink(real, link, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"cannot create a directory symlink: {exc}")

    watch = server._watch_directory_filter(link, ["build", "docs/drafts"])

    assert watch(str(real / "notes")) is True
    assert watch(str(real / "docs" / "drafts")) is False


# -------------------------------------------------------------- scheduling


def test_supporting_watchdog_gets_the_directory_filter(watch_config: Path, capsys) -> None:
    observer = _FilterObserver()
    handler = object()

    assert server._schedule_document_watch(observer, handler) is True

    [(args, kwargs)] = observer.calls
    assert args == (handler, str(watch_config))
    assert kwargs["recursive"] is True
    directory_filter = kwargs["directory_filter"]
    assert directory_filter(str(watch_config / "node_modules")) is False
    assert directory_filter(str(watch_config / "docs" / "guides")) is True
    assert _watcher_lines(capsys.readouterr().err) == []


def test_legacy_watchdog_schedules_without_the_filter_and_says_so_once(watch_config: Path, capsys) -> None:
    observer = _LegacyObserver()

    assert server._schedule_document_watch(observer, object()) is False

    [(_args, kwargs)] = observer.calls
    assert kwargs == {"recursive": True}
    [line] = _watcher_lines(capsys.readouterr().err)
    assert "pruning unavailable" in line and server.WATCHDOG_VERSION in line


def test_watch_excluded_dirs_true_passes_no_filter_and_logs_nothing(watch_config: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(server.config, "watch_excluded_dirs", True)
    observer = _FilterObserver()

    assert server._schedule_document_watch(observer, object()) is False

    [(_args, kwargs)] = observer.calls
    assert kwargs["directory_filter"] is None
    assert _watcher_lines(capsys.readouterr().err) == []


def test_installed_watchdog_schedules_without_error(watch_config: Path, capsys) -> None:
    """Real Observer: the filter is passed exactly when its ``schedule`` accepts it."""
    observer = server.Observer()
    try:
        pruned = server._schedule_document_watch(observer, server.DocumentWatcher(lambda: None))
        supported = server._supports_directory_filter(observer)
        assert pruned is supported
        assert len(_watcher_lines(capsys.readouterr().err)) == (0 if supported else 1)
    finally:
        observer.unschedule_all()


# -------------------------------------------------------------- config


def test_watch_excluded_dirs_defaults_to_false(monkeypatch) -> None:
    import mcp_server.config as config_module

    monkeypatch.setattr(config_module, "_yaml", {})
    assert config_module.Config().watch_excluded_dirs is False


def test_watch_excluded_dirs_from_yaml_is_respected(monkeypatch) -> None:
    import mcp_server.config as config_module

    monkeypatch.setattr(config_module, "_yaml", {"documents": {"watch_excluded_dirs": True}})
    assert config_module.Config().watch_excluded_dirs is True


def test_given_a_non_bool_yaml_value_when_loading_then_it_falls_back_to_false(monkeypatch, capsys) -> None:
    import mcp_server.config as config_module

    monkeypatch.setattr(config_module, "_yaml", {"documents": {"watch_excluded_dirs": "true"}})
    assert config_module.Config().watch_excluded_dirs is False
    assert "documents.watch_excluded_dirs has wrong type" in capsys.readouterr().err


def test_given_a_non_bool_argument_when_constructing_then_it_falls_back_to_false(monkeypatch, capsys) -> None:
    import mcp_server.config as config_module

    monkeypatch.setattr(config_module, "_yaml", {})
    assert config_module.Config(watch_excluded_dirs="yes").watch_excluded_dirs is False  # type: ignore[arg-type]
    assert "[WARN] watch_excluded_dirs='yes' invalid, using False" in capsys.readouterr().err
