"""Composition only: entrypoints do not construct concrete adapters."""
from __future__ import annotations

from .adapters.data_paths import task_root
from .adapters.task_store import TaskStore
from .adapters.host import TaskClient, TaskHost


def task_client(settings: dict) -> TaskClient:
    store = TaskStore(task_root(settings))
    store.recover_legacy()
    return TaskClient(store, settings)


def task_host(settings: dict) -> TaskHost:
    from .adapters.legacy.price_platform import PricePlatform
    return TaskHost(TaskStore(task_root(settings)), lambda: PricePlatform(settings), settings)


def price_runner(settings: dict, cfg: dict, ui=None):
    from .adapters.legacy.price_runner import PriceTaskRunner
    return PriceTaskRunner(settings, cfg, ui, lambda: task_client(settings))


def archives(settings: dict):
    from .adapters.archives import Archives
    return Archives(TaskStore(task_root(settings)))


def sync_archives(settings: dict) -> dict:
    from .adapters.legacy.sheet_archive import sync_archives as sync
    return sync(TaskStore(task_root(settings)), settings)


def collection(settings: dict):
    from .adapters.collection import Collection
    return Collection(TaskStore(task_root(settings)))
