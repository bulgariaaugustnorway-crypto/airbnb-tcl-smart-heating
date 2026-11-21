"""Persistent state storage supporting local filesystem and AWS S3."""

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, Optional

from src.config import AutomationConfig

_LOGGER = logging.getLogger(__name__)


class ConcurrencyConflictError(Exception):
    """Raised when an optimistic concurrency check fails during state persistence."""
    pass


class StateStore:
    """
    Manages persistent state across automation cycles and AWS Lambda invocations.
    Supports local file persistence, AWS S3 object storage with optimistic concurrency locking,
    and in-memory test persistence.
    """

    def __init__(
        self,
        local_path: Optional[Path] = None,
        s3_bucket: Optional[str] = None,
        s3_key: Optional[str] = None,
        in_memory: bool = False,
    ):
        self.in_memory = in_memory
        self._memory_data: Dict[str, Any] = {}
        self._memory_version: int = 0
        self._last_etag: Optional[str] = None
        self._last_version_id: Optional[str] = None
        if in_memory:
            self.local_path = None
            self.s3_bucket = None
            self.s3_key = None
        else:
            self.local_path = local_path if local_path is not None else AutomationConfig.STATE_CACHE_FILE
            self.s3_bucket = s3_bucket if s3_bucket is not None else AutomationConfig.S3_BUCKET
            self.s3_key = s3_key if s3_key is not None else AutomationConfig.S3_STATE_KEY

    def load_state(self) -> Dict[str, Any]:
        """Load state dictionary from S3 if configured, otherwise local file."""
        if self.in_memory:
            self._last_etag = f"mem-v{self._memory_version}"
            return dict(self._memory_data)

        # 1. S3 Persistence
        if self.s3_bucket:
            try:
                import boto3
                s3 = boto3.client("s3")
                resp = s3.get_object(Bucket=self.s3_bucket, Key=self.s3_key)
                self._last_etag = resp.get("ETag")
                self._last_version_id = resp.get("VersionId")
                content = resp["Body"].read().decode("utf-8")
                _LOGGER.debug(f"Loaded persistent state from s3://{self.s3_bucket}/{self.s3_key} (ETag {self._last_etag})")
                return json.loads(content)
            except Exception as e:
                _LOGGER.warning(f"Could not load state from S3 ({e}); falling back to local file")

        # 2. Local File Persistence
        if self.local_path and self.local_path.exists():
            try:
                content = self.local_path.read_text(encoding="utf-8")
                return json.loads(content)
            except Exception as e:
                _LOGGER.warning(f"Failed to load local state file ({e}); starting fresh")

        return {}

    def save_state(self, state: Dict[str, Any], check_concurrency: bool = True) -> bool:
        """
        Save state dictionary to S3 if configured and to local file.
        Uses optimistic concurrency locking via IfMatch (ETag) when writing to S3 or memory.
        """
        if self.in_memory:
            if check_concurrency and self._last_etag is not None:
                current_memory_etag = f"mem-v{self._memory_version}"
                if self._last_etag != current_memory_etag:
                    raise ConcurrencyConflictError(
                        f"In-memory concurrency conflict: expected {self._last_etag}, but current is {current_memory_etag}"
                    )
            self._memory_version += 1
            self._last_etag = f"mem-v{self._memory_version}"
            self._memory_data = dict(state)
            return True

        payload_bytes = json.dumps(state, indent=2, default=str).encode("utf-8")
        success = True

        # 1. Local File Write
        if self.local_path:
            try:
                self.local_path.parent.mkdir(parents=True, exist_ok=True)
                self.local_path.write_bytes(payload_bytes)
            except Exception as e:
                _LOGGER.warning(f"Failed to write local state file: {e}")
                success = False

        # 2. S3 Write with Optimistic Concurrency Control
        if self.s3_bucket:
            try:
                import boto3
                from botocore.exceptions import ClientError
                s3 = boto3.client("s3")
                put_kwargs: Dict[str, Any] = {
                    "Bucket": self.s3_bucket,
                    "Key": self.s3_key,
                    "Body": payload_bytes,
                    "ContentType": "application/json",
                }
                if check_concurrency and self._last_etag:
                    put_kwargs["IfMatch"] = self._last_etag

                resp = s3.put_object(**put_kwargs)
                self._last_etag = resp.get("ETag")
                self._last_version_id = resp.get("VersionId")
                _LOGGER.debug(f"Saved persistent state to s3://{self.s3_bucket}/{self.s3_key} (ETag {self._last_etag})")
            except Exception as e:
                # Check for 412 PreconditionFailed (optimistic locking conflict)
                error_code = ""
                if hasattr(e, "response") and isinstance(e.response, dict):
                    error_code = e.response.get("Error", {}).get("Code", "")
                if error_code in ("PreconditionFailed", "412") or "PreconditionFailed" in str(e):
                    _LOGGER.critical(
                        f"S3 Concurrency Conflict: State file s3://{self.s3_bucket}/{self.s3_key} was modified concurrently! "
                        f"Expected ETag {self._last_etag}. Aborting overwrite."
                    )
                    raise ConcurrencyConflictError(
                        f"S3 concurrency conflict on s3://{self.s3_bucket}/{self.s3_key}: {e}"
                    ) from e
                _LOGGER.error(f"Failed to write state to S3 (s3://{self.s3_bucket}/{self.s3_key}): {e}")
                success = False

        return success
