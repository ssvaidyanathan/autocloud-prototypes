"""Atomic Open Knowledge File (OKF) storage foundation.

Implements Section 3.2 of the thin-bridge design:
- OKFDocument model for YAML frontmatter and markdown body
- Atomic read-modify-write loops using GCS if_generation_match preconditions
- Exponential backoff with jitter on PreconditionFailed
- Local filesystem fallback with file-level locking and atomic replacement
"""

import fcntl
import logging
import os
from pathlib import Path
import random
import re
import time
from typing import Any, Callable, Optional, Union

from google.api_core import exceptions
from pydantic import BaseModel, ConfigDict, Field
import yaml

from ..config import AgentConfig, get_agent_settings

logger = logging.getLogger(__name__)


class ConcurrencyLockError(Exception):
    """Raised when atomic OKF update fails after max retries due to generation mismatch."""
    pass


from .models import IncidentGroupStatus, OKFDocument, parse_iso_timestamp


def _extract_group_id_from_path(identifier: str) -> str:
    """Extract clean group_id from blob name, file path, or group_id string."""
    name = Path(identifier).name
    if name.endswith(".md"):
        name = name[:-3]
    if name.startswith("incident-"):
        name = name[len("incident-"):]
    return name


def get_canonical_blob_name(identifier: str) -> str:
    """Return canonical GCS blob key for incident document."""
    if identifier.startswith("knowledge/incident-manager/"):
        return identifier
    clean_name = Path(identifier).name
    if not clean_name.endswith(".md"):
        clean_name = f"incident-{clean_name}.md"
    elif not clean_name.startswith("incident-"):
        clean_name = f"incident-{clean_name}"
    return f"knowledge/incident-manager/{clean_name}"


def get_canonical_file_path(base_dir: Path, identifier: str) -> Path:
    """Return canonical local filesystem path for incident document."""
    clean_name = Path(identifier).name
    if not clean_name.endswith(".md"):
        clean_name = f"incident-{clean_name}.md"
    elif not clean_name.startswith("incident-"):
        clean_name = f"incident-{clean_name}"
    return base_dir / clean_name


def _update_gcs_atomically(
    bucket_name: str,
    blob_name: str,
    update_fn: Callable[[OKFDocument], OKFDocument],
    max_retries: int = 5,
    storage_client: Optional[Any] = None,
    backoff_base: float = 0.05,
    create_if_missing: bool = True,
) -> Optional[OKFDocument]:
    """Execute atomic read-modify-write on GCS with if_generation_match precondition.

    With ``create_if_missing=False`` a blob that does not exist is not created: the update
    is skipped with a WARNING and None is returned (see ``OKFStore.update_atomically``).
    """
    if storage_client is None:
        from google.cloud import storage
        storage_client = storage.Client()

    canonical_blob_name = get_canonical_blob_name(blob_name)
    bucket = storage_client.bucket(bucket_name)
    blob = bucket.blob(canonical_blob_name)

    inferred_group_id = _extract_group_id_from_path(blob_name)

    for attempt in range(max_retries):
        try:
            blob.reload()
            gen_val = getattr(blob, "generation", None)
            generation = 0 if gen_val is None else int(gen_val)
            content = blob.download_as_text()
            doc = OKFDocument.from_markdown(content)
        except Exception as exc:
            is_not_found = (
                isinstance(exc, exceptions.NotFound)
                or getattr(exc, "code", None) == 404
                or getattr(exc, "status_code", None) == 404
                or "NotFound" in type(exc).__name__
            )
            if not is_not_found:
                raise
            if not create_if_missing:
                _log_missing_not_recreated(inferred_group_id, f"gs://{bucket_name}/{canonical_blob_name}")
                return None
            generation = 0
            doc = OKFDocument(group_id=inferred_group_id)

        updated_doc = update_fn(doc)
        if not isinstance(updated_doc, OKFDocument):
            raise TypeError(f"update_fn must return an OKFDocument instance, got {type(updated_doc)}")

        try:
            blob.upload_from_string(
                updated_doc.to_markdown(),
                content_type="text/markdown",
                if_generation_match=generation,
            )
            return updated_doc
        except Exception as exc:
            is_precondition_failed = (
                isinstance(exc, exceptions.PreconditionFailed)
                or getattr(exc, "code", None) == 412
                or getattr(exc, "status_code", None) == 412
                or "PreconditionFailed" in type(exc).__name__
            )
            if is_precondition_failed:
                logger.warning(
                    "Precondition generation mismatch on blob %s (expected gen=%s, attempt=%s/%s). Retrying...",
                    canonical_blob_name,
                    generation,
                    attempt + 1,
                    max_retries,
                )
                if attempt == max_retries - 1:
                    raise ConcurrencyLockError(
                        f"Failed to update OKF {canonical_blob_name} after {max_retries} retries due to generation mismatch"
                    ) from exc
                sleep_sec = backoff_base * (2 ** attempt) + random.uniform(0.01, 0.05)
                time.sleep(sleep_sec)
                continue
            raise

    raise ConcurrencyLockError(f"Failed to update OKF {canonical_blob_name} after {max_retries} retries")


def _log_missing_not_recreated(group_id: str, location: str) -> None:
    logger.warning(
        "OKF incident file for group %s no longer exists at %s (archived or deleted since it was read); "
        "skipping the update and NOT recreating it",
        group_id,
        location,
    )


# Directory used when the configured local store directory cannot be created.
LOCAL_FALLBACK_DIR = Path("/tmp/knowledge/incident-manager")


def resolve_local_target_file(storage_path: Path, identifier: str, create: bool = True) -> Path:
    """Return the local file that a local atomic update writes, creating its directory.

    Falls back to ``LOCAL_FALLBACK_DIR`` when the primary directory cannot be created.
    With ``create=False`` nothing is created: the fallback file is returned only when the
    primary directory does not exist and the fallback directory does, else the primary file.
    """
    target_file = get_canonical_file_path(storage_path, identifier)
    if not create:
        fallback_file = LOCAL_FALLBACK_DIR / target_file.name
        # os.path.isdir never raises (Path.is_dir can, on an unreadable parent before 3.13).
        if not os.path.isdir(target_file.parent) and os.path.isdir(fallback_file.parent):
            return fallback_file
        return target_file
    try:
        target_file.parent.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        target_file = LOCAL_FALLBACK_DIR / target_file.name
        target_file.parent.mkdir(parents=True, exist_ok=True)
    return target_file


def _update_local_atomically(
    storage_path: Path,
    identifier: str,
    update_fn: Callable[[OKFDocument], OKFDocument],
    max_retries: int = 5,
    create_if_missing: bool = True,
) -> Optional[OKFDocument]:
    """Execute atomic read-modify-write on local filesystem using file lock and atomic rename."""
    if not create_if_missing:
        target_file = resolve_local_target_file(storage_path, identifier, create=False)
        if not target_file.exists():
            _log_missing_not_recreated(_extract_group_id_from_path(identifier), str(target_file))
            return None
    else:
        target_file = resolve_local_target_file(storage_path, identifier)

    lock_file = target_file.parent / f".{target_file.name}.lock"
    inferred_group_id = _extract_group_id_from_path(identifier)

    with open(lock_file, "a+") as lock_f:
        fcntl.flock(lock_f, fcntl.LOCK_EX)
        try:
            if target_file.exists():
                content = target_file.read_text(encoding="utf-8")
                doc = OKFDocument.from_markdown(content)
            elif not create_if_missing:
                # Removed between the check above and taking the lock.
                _log_missing_not_recreated(inferred_group_id, str(target_file))
                return None
            else:
                doc = OKFDocument(group_id=inferred_group_id)

            updated_doc = update_fn(doc)
            if not isinstance(updated_doc, OKFDocument):
                raise TypeError(f"update_fn must return an OKFDocument instance, got {type(updated_doc)}")

            tmp_file = target_file.parent / f".tmp.{target_file.name}.{os.getpid()}.{time.time_ns()}"
            tmp_file.write_text(updated_doc.to_markdown(), encoding="utf-8")
            os.replace(tmp_file, target_file)
            return updated_doc
        finally:
            fcntl.flock(lock_f, fcntl.LOCK_UN)


class OKFStore:
    """Store for managing atomic OKF document persistence across GCS and local fallback."""

    def __init__(
        self,
        bucket_name: Optional[str] = None,
        storage_path: Optional[Union[str, Path]] = None,
        storage_client: Optional[Any] = None,
        is_local: Optional[bool] = None,
        config: Optional[AgentConfig] = None,
    ) -> None:
        self.config = config or get_agent_settings()
        self.bucket_name = (
            bucket_name
            or getattr(self.config, "incidents_bucket", None)
            or f"{self.config.project_id}-{self.config.name_prefix}-incidents"
        )
        self.storage_path = Path(storage_path) if storage_path else self.config.get_effective_store_path()
        self.storage_client = storage_client
        self.is_local = is_local if is_local is not None else self.config.is_local

    def update_atomically(
        self,
        blob_name_or_group_id: str,
        update_fn: Callable[[OKFDocument], OKFDocument],
        max_retries: int = 5,
        create_if_missing: bool = True,
    ) -> Optional[OKFDocument]:
        """Atomically update an OKF document.

        By default a missing document is created from ``OKFDocument(group_id=...)`` (status
        open). Updates to an existing group (e.g. after a timer wake) pass
        ``create_if_missing=False``: if the file was archived or deleted meanwhile, nothing
        is written, a WARNING names the group, and None is returned. Otherwise the write
        would recreate a stub ``status: open`` file in the active folder.
        """
        if self.storage_client is not None or (self.bucket_name and not self.is_local):
            return _update_gcs_atomically(
                bucket_name=self.bucket_name,
                blob_name=blob_name_or_group_id,
                update_fn=update_fn,
                max_retries=max_retries,
                storage_client=self.storage_client,
                create_if_missing=create_if_missing,
            )
        return _update_local_atomically(
            storage_path=self.storage_path,
            identifier=blob_name_or_group_id,
            update_fn=update_fn,
            max_retries=max_retries,
            create_if_missing=create_if_missing,
        )

    def read(self, blob_name_or_group_id: str) -> Optional[OKFDocument]:
        """Read OKF document by blob name or group_id."""
        if self.storage_client is not None or (self.bucket_name and not self.is_local):
            canonical = get_canonical_blob_name(blob_name_or_group_id)
            client = self.storage_client
            if client is None:
                from google.cloud import storage
                client = storage.Client()
            bucket = client.bucket(self.bucket_name)
            blob = bucket.blob(canonical)
            try:
                blob.reload()
                content = blob.download_as_text()
                return OKFDocument.from_markdown(content)
            except Exception:
                return None
        else:
            target = get_canonical_file_path(self.storage_path, blob_name_or_group_id)
            if not target.exists():
                if not self.storage_path.exists():
                    fallback = Path("/tmp/knowledge/incident-manager") / target.name
                    if fallback.exists():
                        target = fallback
                    else:
                        return None
                else:
                    return None
            try:
                return OKFDocument.from_markdown(target.read_text(encoding="utf-8"))
            except Exception:
                return None

    def write(self, blob_name_or_group_id: str, doc: OKFDocument) -> OKFDocument:
        """Write OKF document."""
        return self.update_atomically(blob_name_or_group_id, lambda _: doc)

    def list_documents(self, status_filter: Optional[set[str]] = None) -> list[OKFDocument]:
        """List and load OKF documents, optionally filtering by status."""
        results: list[OKFDocument] = []
        if self.storage_client is not None or (self.bucket_name and not self.is_local):
            client = self.storage_client
            if client is None:
                from google.cloud import storage
                client = storage.Client()
            bucket = client.bucket(self.bucket_name)
            prefix = "knowledge/incident-manager/incident-"
            try:
                blobs = list(bucket.list_blobs(prefix=prefix))
                for b in blobs:
                    if b.name.endswith(".md"):
                        try:
                            content = b.download_as_text()
                            doc = OKFDocument.from_markdown(content)
                            if status_filter is None or str(doc.status).lower() in status_filter:
                                results.append(doc)
                        except Exception as exc:
                            logger.debug("Failed reading blob %s during list: %s", b.name, exc)
            except Exception as exc:
                logger.warning("Failed listing GCS blobs in %s: %s", self.bucket_name, exc)
        else:
            dirs_to_check = [self.storage_path]
            if not self.storage_path.exists():
                fallback_dir = Path("/tmp/knowledge/incident-manager")
                if fallback_dir.exists():
                    dirs_to_check = [fallback_dir]

            seen_group_ids: set[str] = set()
            for base_dir in dirs_to_check:
                if not base_dir.exists():
                    continue
                for f in base_dir.glob("incident-*.md"):
                    if f.name.startswith("."):
                        continue
                    try:
                        content = f.read_text(encoding="utf-8")
                        doc = OKFDocument.from_markdown(content)
                        if doc.group_id not in seen_group_ids:
                            seen_group_ids.add(doc.group_id)
                            if status_filter is None or str(doc.status).lower() in status_filter:
                                results.append(doc)
                    except Exception as exc:
                        logger.debug("Failed reading file %s during list: %s", f, exc)

        return results


def update_okf_atomically(
    bucket_name: Optional[str] = None,
    blob_name: str = "",
    update_fn: Callable[[OKFDocument], OKFDocument] = lambda d: d,
    max_retries: int = 5,
    storage_client: Optional[Any] = None,
    storage_path: Optional[Union[str, Path]] = None,
    is_local: Optional[bool] = None,
    config: Optional[AgentConfig] = None,
) -> OKFDocument:
    """Convenience function for atomic OKF update."""
    store = OKFStore(
        bucket_name=bucket_name,
        storage_path=storage_path,
        storage_client=storage_client,
        is_local=is_local,
        config=config,
    )
    return store.update_atomically(blob_name, update_fn, max_retries=max_retries)


def read_okf(
    blob_name_or_group_id: str,
    bucket_name: Optional[str] = None,
    storage_client: Optional[Any] = None,
    storage_path: Optional[Union[str, Path]] = None,
    is_local: Optional[bool] = None,
    config: Optional[AgentConfig] = None,
) -> Optional[OKFDocument]:
    """Convenience function for reading OKF."""
    store = OKFStore(
        bucket_name=bucket_name,
        storage_path=storage_path,
        storage_client=storage_client,
        is_local=is_local,
        config=config,
    )
    return store.read(blob_name_or_group_id)


def write_okf(
    blob_name_or_group_id: str,
    doc: OKFDocument,
    bucket_name: Optional[str] = None,
    storage_client: Optional[Any] = None,
    storage_path: Optional[Union[str, Path]] = None,
    is_local: Optional[bool] = None,
    config: Optional[AgentConfig] = None,
) -> OKFDocument:
    """Convenience function for writing OKF."""
    store = OKFStore(
        bucket_name=bucket_name,
        storage_path=storage_path,
        storage_client=storage_client,
        is_local=is_local,
        config=config,
    )
    return store.write(blob_name_or_group_id, doc)


def list_okf_documents(
    status_filter: Optional[set[str]] = None,
    bucket_name: Optional[str] = None,
    storage_client: Optional[Any] = None,
    storage_path: Optional[Union[str, Path]] = None,
    is_local: Optional[bool] = None,
    config: Optional[AgentConfig] = None,
) -> list[OKFDocument]:
    """Convenience function for listing OKF documents."""
    store = OKFStore(
        bucket_name=bucket_name,
        storage_path=storage_path,
        storage_client=storage_client,
        is_local=is_local,
        config=config,
    )
    return store.list_documents(status_filter=status_filter)
