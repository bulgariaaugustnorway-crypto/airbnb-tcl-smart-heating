"""AWS Lambda Handler for unattended Airbnb TCL AC HVAC automation."""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Dict, Any, Optional

# Ensure project root is in sys.path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.config import AutomationConfig, TIMEZONE_SOFIA
from src.orchestrator import AutomationOrchestrator, CycleResult
from src.state_store import StateStore
from src.calendar_parser import AirbnbCalendarParser
from src.notifier import NotificationDispatcher, NotificationEvent
from src.tcl.controller import TCLCloudController
from src.device_controller import MockACController, BaseACController
from src.models import mask_uid
from src.dashboard.config_store import DashboardConfigStore
from src.dashboard.handler import handle_dashboard_request

# Configure logging
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
_log_level = getattr(logging, LOG_LEVEL, logging.INFO)
logging.basicConfig(
    level=_log_level,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
root_logger = logging.getLogger()
root_logger.setLevel(_log_level)
for h in root_logger.handlers:
    h.setLevel(_log_level)
_LOGGER = logging.getLogger("lambda_handler")
_LOGGER.setLevel(_log_level)


def load_ssm_parameters(prefix: str) -> Dict[str, str]:
    """
    Retrieve secrets and sensitive configuration from AWS Systems Manager Parameter Store.
    Securely fetches parameters under prefix (e.g. /smart-heating/example) with automatic KMS decryption.
    """
    if not prefix:
        return {}
    try:
        import boto3
        ssm = boto3.client("ssm")
        _LOGGER.info(f"Retrieving credentials and configuration from SSM Parameter Store path: {prefix}...")
        resp = ssm.get_parameters_by_path(
            Path=prefix.rstrip("/"),
            Recursive=True,
            WithDecryption=True
        )
        params = {}
        for p in resp.get("Parameters", []):
            name = p["Name"].split("/")[-1].lower()
            params[name] = p["Value"]
        _LOGGER.info(f"Retrieved {len(params)} parameter(s) from SSM path '{prefix}': {list(params.keys())}")
        return params
    except Exception as e:
        _LOGGER.warning(f"Could not load parameters from SSM ({prefix}): {e}")
        return {}


def init_controller(config: AutomationConfig) -> BaseACController:
    """Initialize hardware controller (TCL Cloud with graceful fallback to Mock for dry-run)."""
    if config.TCL_USERNAME and config.TCL_PASSWORD:
        _LOGGER.info("Configuring live TCL Cloud Controller...")
        controller = TCLCloudController(
            username=config.TCL_USERNAME,
            password=config.TCL_PASSWORD,
            read_only=config.TCL_READ_ONLY,
            target_device_ids=config.DEVICE_IDS
        )
        connected = controller.authenticate_and_connect()
        if connected:
            _LOGGER.info(
                f"TCL Cloud connected successfully. Discovered {len(controller.discovered_devices)} devices."
            )
            return controller
        else:
            _LOGGER.error("Failed to authenticate with TCL Cloud.")
            if config.DRY_RUN:
                _LOGGER.warning("DRY_RUN is active; falling back to MockACController for testing.")
                return MockACController(device_ids=config.DEVICE_IDS)
            else:
                raise RuntimeError("Failed to authenticate with TCL Cloud in non-dry-run mode.")

    _LOGGER.warning("TCL credentials not configured in environment. Using MockACController.")
    return MockACController(device_ids=config.DEVICE_IDS)


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    AWS Lambda entrypoint triggered by EventBridge Scheduler (every 15 minutes) or manual invocation.

    Parameters:
        event: Event payload from EventBridge or API Gateway
        context: AWS Lambda runtime context object

    Returns:
        Structured response with execution metrics and decision summary.
    """
    start_time = datetime.now()
    _LOGGER.info("Starting Airbnb TCL HVAC Automation cycle...")
    _LOGGER.debug("Invocation received; request payload omitted from logs")

    config = AutomationConfig()

    # Hydrate credentials from AWS Systems Manager Parameter Store if configured
    ssm_prefix = os.getenv("SSM_PARAMETER_PREFIX")
    if ssm_prefix:
        ssm_params = load_ssm_parameters(ssm_prefix)
        if ssm_params.get("tcl_username"):
            config.TCL_USERNAME = ssm_params["tcl_username"]
        if ssm_params.get("tcl_password"):
            config.TCL_PASSWORD = ssm_params["tcl_password"]
        if ssm_params.get("airbnb_ical_url"):
            config.AIRBNB_ICAL_URL = ssm_params["airbnb_ical_url"]
        if ssm_params.get("notification_webhook_url"):
            config.NOTIFICATION_WEBHOOK_URL = ssm_params["notification_webhook_url"]
        if ssm_params.get("telegram_bot_token"):
            config.TELEGRAM_BOT_TOKEN = ssm_params["telegram_bot_token"]
        if ssm_params.get("telegram_chat_id"):
            config.TELEGRAM_CHAT_ID = ssm_params["telegram_chat_id"]
        if ssm_params.get("heat_during_blocked_dates"):
            config.HEAT_DURING_BLOCKED_DATES = ssm_params["heat_during_blocked_dates"].lower() in ("true", "1", "yes")
        if ssm_params.get("dashboard_password_hash"):
            config.DASHBOARD_PASSWORD_HASH = ssm_params["dashboard_password_hash"]
        elif ssm_params.get("dashboard_password"):
            config.DASHBOARD_PASSWORD_HASH = ssm_params["dashboard_password"]
        if ssm_params.get("session_secret_key"):
            config.SESSION_SECRET_KEY = ssm_params["session_secret_key"]
        _LOGGER.info(
            f"SSM Configuration loaded: TCL User: {'YES' if config.TCL_USERNAME else 'NO'} | "
            f"TCL Password: {'YES' if config.TCL_PASSWORD else 'NO'} | "
            f"Airbnb iCal: {'YES' if config.AIRBNB_ICAL_URL else 'NO'} | "
            f"Webhook: {'YES' if config.NOTIFICATION_WEBHOOK_URL else 'NO'} | "
            f"Telegram: {'CONFIGURED' if config.is_telegram_configured else 'NOT_CONFIGURED'} | "
            f"Heat Blocked: {config.HEAT_DURING_BLOCKED_DATES} | "
            f"Dashboard Auth: {'CONFIGURED' if config.DASHBOARD_PASSWORD_HASH else 'NOT_CONFIGURED'}"
        )
        if config.AIRBNB_ICAL_URL:
            is_valid_scheme = config.AIRBNB_ICAL_URL.startswith(("http://", "https://"))
            _LOGGER.info(
                f"Airbnb iCal URL check: valid scheme: {is_valid_scheme}, length: {len(config.AIRBNB_ICAL_URL)}"
            )
            if not is_valid_scheme:
                _LOGGER.warning(
                    "SSM parameter 'airbnb_ical_url' does not start with http:// or https://. "
                    "Value appears invalid or corrupted."
                )

    # Check if invocation is an HTTP request from Lambda Function URL or API Gateway
    is_http = (
        isinstance(event, dict) and (
            "rawPath" in event
            or "httpMethod" in event
            or ("requestContext" in event and "http" in event.get("requestContext", {}))
        )
    )
    if is_http:
        _LOGGER.info("Dispatching HTTP request to Owner Dashboard handler...")
        return handle_dashboard_request(event, context, config=config)

    notifier = NotificationDispatcher(
        webhook_url=config.NOTIFICATION_WEBHOOK_URL,
        bot_token=config.TELEGRAM_BOT_TOKEN,
        chat_id=config.TELEGRAM_CHAT_ID,
        config=config
    )

    try:
        # 1. Initialize persistent state store (S3 in cloud, local in test)
        state_store = StateStore(
            s3_bucket=config.S3_BUCKET,
            s3_key=config.S3_STATE_KEY
        )

        # 1b. Apply persistent dashboard settings and active Owner Test session from S3
        config_store = DashboardConfigStore(config=config)
        config = config_store.apply_to_config(config)
        active_owner_test = config_store.load_owner_test_session()

        # 2. Initialize device controller
        controller = init_controller(config)

        # 3. Initialize calendar parser
        calendar_parser = AirbnbCalendarParser()

        # 4. Instantiate orchestrator
        orchestrator = AutomationOrchestrator(
            config=config,
            controller=controller,
            notifier=notifier,
            calendar_parser=calendar_parser,
            state_store=state_store
        )

        # 5. Execute automation cycle
        # Allow event to specify current_dt for scheduled backtesting or simulation
        current_dt = None
        if isinstance(event, dict) and "current_dt" in event:
            try:
                current_dt = datetime.fromisoformat(event["current_dt"])
            except Exception as e:
                _LOGGER.warning(f"Failed to parse override current_dt '{event['current_dt']}': {e}")

        result: CycleResult = orchestrator.run_cycle(
            current_dt=current_dt,
            calendar_url=config.AIRBNB_ICAL_URL,
            owner_test=active_owner_test
        )

        duration_ms = (datetime.now() - start_time).total_seconds() * 1000

        # 6. Format structured output
        summary = {
            "status": "success",
            "timestamp": result.timestamp.isoformat(),
            "system_state": result.system_state.value,
            "decision_reason": result.decision_reason,
            "active_reservation": result.active_reservation.masked_uid if result.active_reservation else None,
            "commands_sent": result.commands_sent,
            "in_grace_period": [mask_uid(u) for u in result.in_grace_period_uids],
            "calendar_stale": result.calendar_stale,
            "dry_run": result.dry_run,
            "execution_duration_ms": round(duration_ms, 2)
        }

        _LOGGER.info(
            f"Cycle completed successfully in {duration_ms:.1f}ms. "
            f"State: {result.system_state.value} | Commands sent: {result.commands_sent}"
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": summary
        }

    except Exception as e:
        duration_ms = (datetime.now() - start_time).total_seconds() * 1000
        error_msg = f"Unhandled exception during automation cycle: {e}"
        _LOGGER.exception(error_msg)

        notifier.dispatch(
            NotificationEvent(
                event_type="LAMBDA_FATAL_ERROR",
                title="CRITICAL: Lambda Automation Failure",
                message=error_msg,
                severity="CRITICAL",
                metadata={"duration_ms": duration_ms}
            )
        )

        return {
            "statusCode": 500,
            "headers": {"Content-Type": "application/json"},
            "body": {
                "status": "error",
                "error": str(e),
                "timestamp": datetime.now().isoformat(),
                "execution_duration_ms": round(duration_ms, 2)
            }
        }


def dashboard_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """
    Dedicated AWS Lambda entrypoint for Owner Mobile Dashboard HTTP requests.
    Decoupled from 15-minute HVAC scheduler to isolate web/API traffic and prevent
    concurrency exhaustion or timeouts affecting scheduled HVAC automation.
    """
    config = AutomationConfig()
    ssm_prefix = os.getenv("SSM_PARAMETER_PREFIX")
    if ssm_prefix:
        ssm_params = load_ssm_parameters(ssm_prefix)
        if ssm_params.get("dashboard_password_hash"):
            config.DASHBOARD_PASSWORD_HASH = ssm_params["dashboard_password_hash"]
        elif ssm_params.get("dashboard_password"):
            config.DASHBOARD_PASSWORD_HASH = ssm_params["dashboard_password"]
        if ssm_params.get("session_secret_key"):
            config.SESSION_SECRET_KEY = ssm_params["session_secret_key"]
        if ssm_params.get("notification_webhook_url"):
            config.NOTIFICATION_WEBHOOK_URL = ssm_params["notification_webhook_url"]
        if ssm_params.get("telegram_bot_token"):
            config.TELEGRAM_BOT_TOKEN = ssm_params["telegram_bot_token"]
        if ssm_params.get("telegram_chat_id"):
            config.TELEGRAM_CHAT_ID = ssm_params["telegram_chat_id"]
    return handle_dashboard_request(event, context, config=config)


if __name__ == "__main__":
    # Local CLI runner for ad-hoc execution
    resp = lambda_handler({}, None)
    print(json.dumps(resp, indent=2))
