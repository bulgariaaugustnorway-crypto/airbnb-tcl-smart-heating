"""Security, authentication, password verification, and session management for Owner Dashboard."""

import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta
from typing import Tuple, Optional, Dict, Any

from src.config import AutomationConfig, CACHE_DIR

_LOGGER = logging.getLogger(__name__)

_FAILED_ATTEMPTS: Dict[str, Dict[str, Any]] = {}
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_DURATION_SECONDS = 900  # 15 minutes lockout
SESSION_DURATION_SECONDS = 86400  # 24 hours session expiry
RATE_LIMIT_STORE_FILE = CACHE_DIR / "rate_limit_store.json"


import abc

class RateLimiterUnavailableError(RuntimeError):
    """Raised when the rate limiter backend (e.g. DynamoDB in production) is offline or unreachable."""
    pass


class BaseRateLimiter(abc.ABC):
    """Abstract interface for rate limiting and brute-force protection."""

    @abc.abstractmethod
    def is_rate_limited(self, client_ip: str) -> Tuple[bool, int]:
        """Check if client IP is currently rate-limited/locked out."""
        pass

    @abc.abstractmethod
    def record_failed_attempt(self, client_ip: str) -> int:
        """Record a failed login attempt; returns remaining lockout seconds if triggered."""
        pass

    @abc.abstractmethod
    def clear_failed_attempts(self, client_ip: str) -> None:
        """Clear lockout records on successful login."""
        pass


class LocalRateLimiter(BaseRateLimiter):
    """
    Local file-backed rate limiter for development, testing, and off-cloud execution.
    Maintains in-memory dictionary synchronized with persistent JSON file.
    """

    def __init__(self, store_file: Optional[Any] = None):
        self.store_file = store_file or RATE_LIMIT_STORE_FILE
        self._failed_attempts: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> Dict[str, Dict[str, Any]]:
        global _FAILED_ATTEMPTS
        now = time.time()
        try:
            if self.store_file and self.store_file.exists():
                data = json.loads(self.store_file.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    cleaned = {}
                    for ip, info in data.items():
                        if isinstance(info, dict):
                            lockout_until = info.get("lockout_until", 0)
                            first_attempt = info.get("first_attempt", 0)
                            if now < lockout_until or (now - first_attempt <= LOCKOUT_DURATION_SECONDS):
                                cleaned[ip] = info
                    self._failed_attempts.update(cleaned)
                    _FAILED_ATTEMPTS.update(cleaned)
        except Exception as e:
            _LOGGER.debug(f"Could not load local rate limit store: {e}")
        return self._failed_attempts

    def _save(self) -> None:
        try:
            if self.store_file:
                self.store_file.parent.mkdir(parents=True, exist_ok=True)
                now = time.time()
                cleaned = {
                    ip: info for ip, info in self._failed_attempts.items()
                    if now < info.get("lockout_until", 0) or (now - info.get("first_attempt", 0) <= LOCKOUT_DURATION_SECONDS)
                }
                # Atomic write via temporary file replacement
                tmp = self.store_file.with_suffix(".tmp")
                tmp.write_text(json.dumps(cleaned), encoding="utf-8")
                tmp.replace(self.store_file)
        except Exception as e:
            _LOGGER.debug(f"Could not save local rate limit store: {e}")

    def is_rate_limited(self, client_ip: str) -> Tuple[bool, int]:
        self._load()
        now = time.time()
        info = self._failed_attempts.get(client_ip)
        if not info:
            return False, 0

        lockout_until = info.get("lockout_until", 0)
        first_attempt = info.get("first_attempt", now)

        if now < lockout_until:
            remaining = int(lockout_until - now)
            return True, remaining

        if now - first_attempt > LOCKOUT_DURATION_SECONDS:
            self._failed_attempts.pop(client_ip, None)
            _FAILED_ATTEMPTS.pop(client_ip, None)
            self._save()
            return False, 0

        return False, 0

    def record_failed_attempt(self, client_ip: str) -> int:
        self._load()
        now = time.time()
        info = self._failed_attempts.setdefault(client_ip, {"count": 0, "first_attempt": now, "lockout_until": 0})
        _FAILED_ATTEMPTS[client_ip] = info

        # If previous window expired, reset counter
        if now - info.get("first_attempt", now) > LOCKOUT_DURATION_SECONDS and now >= info.get("lockout_until", 0):
            info["count"] = 0
            info["first_attempt"] = now
            info["lockout_until"] = 0

        info["count"] += 1

        lockout_result = 0
        if info["count"] >= MAX_FAILED_ATTEMPTS:
            info["lockout_until"] = now + LOCKOUT_DURATION_SECONDS
            lockout_result = LOCKOUT_DURATION_SECONDS

        self._save()
        return lockout_result

    def clear_failed_attempts(self, client_ip: str) -> None:
        self._load()
        self._failed_attempts.pop(client_ip, None)
        _FAILED_ATTEMPTS.pop(client_ip, None)
        self._save()


class DynamoDBRateLimiter(BaseRateLimiter):
    """
    Durable, distributed, atomic rate limiter for AWS Lambda production using Amazon DynamoDB.
    - Partition Key: client_ip (String)
    - Attributes: attempt_count (Number), first_attempt (Number), lockout_until (Number), ttl (Number)
    - Atomic updates prevent race conditions across concurrent Lambda containers.
    - Native TTL automatically purges stale records at zero cost.
    """

    def __init__(self, table_name: Optional[str] = None, region_name: Optional[str] = None, dynamodb_resource: Optional[Any] = None):
        self.table_name = table_name or os.getenv("DYNAMODB_RATE_LIMIT_TABLE", "tcl_auth_rate_limits")
        self.region_name = region_name or os.getenv("AWS_REGION", "eu-central-1")
        self._table = None
        self._dynamodb_resource = dynamodb_resource

    def _get_table(self):
        if self._table is None:
            if self._dynamodb_resource:
                self._table = self._dynamodb_resource.Table(self.table_name)
            else:
                import boto3
                dynamodb = boto3.resource("dynamodb", region_name=self.region_name)
                self._table = dynamodb.Table(self.table_name)
        return self._table

    def is_rate_limited(self, client_ip: str) -> Tuple[bool, int]:
        now = int(time.time())
        try:
            table = self._get_table()
            resp = table.get_item(Key={"client_ip": client_ip})
            item = resp.get("Item")
            if not item:
                return False, 0

            lockout_until = int(item.get("lockout_until", 0))
            if now < lockout_until:
                return True, int(lockout_until - now)

            first_attempt = int(item.get("first_attempt", now))
            if now - first_attempt > LOCKOUT_DURATION_SECONDS:
                return False, 0

            count = int(item.get("attempt_count", 0))
            if count >= MAX_FAILED_ATTEMPTS:
                return True, LOCKOUT_DURATION_SECONDS

            return False, 0
        except Exception as e:
            _LOGGER.critical(f"DynamoDBRateLimiter error in is_rate_limited: {e}")
            raise RateLimiterUnavailableError(f"DynamoDB rate limit store unavailable: {e}")

    def record_failed_attempt(self, client_ip: str) -> int:
        now = int(time.time())
        ttl = now + LOCKOUT_DURATION_SECONDS + 3600
        cutoff = now - LOCKOUT_DURATION_SECONDS

        try:
            table = self._get_table()

            # Concurrency-safe initialization or window reset:
            # If the item does not exist, or the previous window has expired and lockout has passed:
            # We attempt to initialize/reset the record using a conditional PutItem.
            reset_succeeded = False
            try:
                table.put_item(
                    Item={
                        "client_ip": client_ip,
                        "attempt_count": 1,
                        "first_attempt": now,
                        "last_attempt": now,
                        "lockout_until": 0,
                        "ttl": ttl
                    },
                    ConditionExpression="attribute_not_exists(client_ip) OR (first_attempt < :cutoff AND (attribute_not_exists(lockout_until) OR lockout_until <= :now))",
                    ExpressionAttributeValues={
                        ":cutoff": cutoff,
                        ":now": now
                    }
                )
                reset_succeeded = True
            except Exception as cond_err:
                err_str = str(cond_err).lower()
                is_cond_fail = (
                    "conditionalcheckfailed" in err_str
                    or "conditional check failed" in err_str
                    or "condition check failed" in err_str
                    or type(cond_err).__name__ == "ConditionalCheckFailedException"
                    or getattr(cond_err, "response", {}).get("Error", {}).get("Code", "") == "ConditionalCheckFailedException"
                )
                if not is_cond_fail:
                    _LOGGER.critical(f"DynamoDB error during conditional reset in record_failed_attempt: {cond_err}")
                    raise RateLimiterUnavailableError(f"DynamoDB rate limit store unavailable: {cond_err}")

            if reset_succeeded:
                return 0

            # Active window exists: perform atomic increment using ADD
            # In DynamoDB, ADD attempt_count :one is atomic and serial across all concurrent threads/processes.
            resp = table.update_item(
                Key={"client_ip": client_ip},
                UpdateExpression="ADD attempt_count :one SET last_attempt = :now, #ttl_col = :ttl",
                ExpressionAttributeNames={"#ttl_col": "ttl"},
                ExpressionAttributeValues={
                    ":one": 1,
                    ":now": now,
                    ":ttl": ttl
                },
                ReturnValues="ALL_NEW"
            )

            updated = resp.get("Attributes", {})
            new_count = int(updated.get("attempt_count", 0))

            if new_count >= MAX_FAILED_ATTEMPTS:
                lockout_until = now + LOCKOUT_DURATION_SECONDS
                table.update_item(
                    Key={"client_ip": client_ip},
                    UpdateExpression="SET lockout_until = :lock",
                    ExpressionAttributeValues={":lock": lockout_until}
                )
                return LOCKOUT_DURATION_SECONDS

            return 0
        except Exception as e:
            if isinstance(e, RateLimiterUnavailableError):
                raise
            _LOGGER.critical(f"DynamoDBRateLimiter error in record_failed_attempt: {e}")
            raise RateLimiterUnavailableError(f"DynamoDB rate limit store unavailable: {e}")

    def clear_failed_attempts(self, client_ip: str) -> None:
        try:
            table = self._get_table()
            table.delete_item(Key={"client_ip": client_ip})
        except Exception as e:
            _LOGGER.warning(f"DynamoDBRateLimiter error in clear_failed_attempts: {e}")
            raise RateLimiterUnavailableError(f"DynamoDB rate limit store unavailable: {e}")


_ACTIVE_RATE_LIMITER: Optional[BaseRateLimiter] = None


def get_rate_limiter() -> BaseRateLimiter:
    """Retrieve active rate limiter instance (DynamoDB in AWS Lambda, Local in dev/test)."""
    global _ACTIVE_RATE_LIMITER
    if _ACTIVE_RATE_LIMITER is not None:
        return _ACTIVE_RATE_LIMITER

    use_dynamo = os.getenv("RATE_LIMIT_BACKEND", "").lower() == "dynamodb" or (
        bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME")) and bool(os.getenv("DYNAMODB_RATE_LIMIT_TABLE"))
    )
    if use_dynamo:
        try:
            _ACTIVE_RATE_LIMITER = DynamoDBRateLimiter()
            return _ACTIVE_RATE_LIMITER
        except Exception as e:
            _LOGGER.critical(f"Could not initialize DynamoDBRateLimiter in production: {e}")
            raise RateLimiterUnavailableError(f"Production DynamoDBRateLimiter unavailable: {e}")

    _ACTIVE_RATE_LIMITER = LocalRateLimiter()
    return _ACTIVE_RATE_LIMITER


def set_rate_limiter(limiter: Optional[BaseRateLimiter]) -> None:
    """Inject a custom rate limiter instance (useful for testing)."""
    global _ACTIVE_RATE_LIMITER
    _ACTIVE_RATE_LIMITER = limiter


def is_rate_limited(client_ip: str) -> Tuple[bool, int]:
    """Check if client IP is temporarily locked out due to brute-force attempts."""
    return get_rate_limiter().is_rate_limited(client_ip)


def record_failed_attempt(client_ip: str) -> int:
    """Record a failed login attempt and apply lockout if threshold reached."""
    return get_rate_limiter().record_failed_attempt(client_ip)


def clear_failed_attempts(client_ip: str) -> None:
    """Clear failed attempts upon successful login."""
    get_rate_limiter().clear_failed_attempts(client_ip)


def hash_password(password: str, iterations: int = 100_000) -> str:
    """Hash password using PBKDF2-HMAC-SHA256 with 100,000 iterations and random salt."""
    if not password:
        return ""
    salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), iterations)
    return f"pbkdf2:sha256:{iterations}${salt}${key.hex()}"


def verify_password(password: str, stored_hash_or_password: str) -> bool:
    """
    Verify password against explicit PBKDF2-SHA256 hash.
    Supports constant-time comparison to prevent timing attacks.
    """
    if not password or not stored_hash_or_password:
        return False

    try:
        algorithm, salt, expected = stored_hash_or_password.split("$")
        prefix, digest, rounds = algorithm.split(":")
        iterations = int(rounds)
        if prefix != "pbkdf2" or digest != "sha256" or not 100_000 <= iterations <= 2_000_000:
            return False
        if len(salt) != 32 or len(expected) != 64:
            return False
        bytes.fromhex(salt)
        bytes.fromhex(expected)
        derived = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), iterations).hex()
        return hmac.compare_digest(derived, expected)
    except (ValueError, TypeError):
        return False


def create_session_token(secret_key: str, duration_seconds: int = SESSION_DURATION_SECONDS) -> str:
    """Create cryptographically signed, timestamped session token."""
    session_id = secrets.token_hex(16)
    expires_at = int(time.time() + duration_seconds)
    payload = f"{session_id}:{expires_at}"
    signature = hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def verify_session_token(token: Optional[str], secret_key: str) -> bool:
    """Verify session token integrity, HMAC signature, and expiration."""
    if not token or ":" not in token:
        return False

    parts = token.split(":")
    if len(parts) != 3:
        return False

    session_id, expires_str, signature = parts
    try:
        expires_at = int(expires_str)
    except ValueError:
        return False

    if time.time() > expires_at:
        return False  # Expired

    payload = f"{session_id}:{expires_at}"
    expected_signature = hmac.new(secret_key.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()

    return hmac.compare_digest(signature, expected_signature)


def generate_csrf_token(session_token: str, secret_key: str) -> str:
    """Derive CSRF token bound to the current session."""
    return hmac.new(secret_key.encode("utf-8"), f"csrf:{session_token}".encode("utf-8"), hashlib.sha256).hexdigest()


def verify_csrf_token(csrf_token: Optional[str], session_token: str, secret_key: str) -> bool:
    """Validate CSRF token matches the session."""
    if not csrf_token:
        return False
    expected = generate_csrf_token(session_token, secret_key)
    return hmac.compare_digest(csrf_token, expected)
