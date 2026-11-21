"""RFC 5545 iCalendar parser for Airbnb reservation feeds."""

import logging
import re
import urllib.request
from src import http_transport
import urllib.error
import urllib.parse
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set

from src.models import Reservation, ReservationType, mask_uid
from src.config import AutomationConfig

_LOGGER = logging.getLogger(__name__)


class CalendarSyncResult:
    """Encapsulates the result of a calendar synchronization pass."""
    def __init__(
        self,
        reservations: List[Reservation],
        source: str,
        is_from_cache: bool = False,
        cache_age_seconds: Optional[float] = None,
        is_stale: bool = False,
        cancelled_uids: Optional[List[str]] = None,
        error_message: Optional[str] = None
    ):
        self.reservations = reservations
        self.source = source
        self.is_from_cache = is_from_cache
        self.cache_age_seconds = cache_age_seconds
        self.is_stale = is_stale
        self.cancelled_uids = cancelled_uids or []
        self.error_message = error_message

    @property
    def is_usable(self) -> bool:
        """Determines if the calendar data is reliable enough to govern active occupancy."""
        return not self.is_stale

    @property
    def confirmed_reservations(self) -> List[Reservation]:
        return [r for r in self.reservations if r.is_confirmed]

    @property
    def blocked_dates(self) -> List[Reservation]:
        return [r for r in self.reservations if r.reservation_type == ReservationType.BLOCKED]


class AirbnbCalendarParser:
    """Parses Airbnb iCal exports and tracks reservation lifecycle."""

    def __init__(self, cache_file: Optional[Path] = None):
        self.cache_file = cache_file or AutomationConfig.CALENDAR_CACHE_FILE
        self._previously_known_uids: Set[str] = set()

    def parse_ics_content(self, raw_ics: str) -> List[Reservation]:
        """Parse raw RFC 5545 iCalendar text into Reservation objects."""
        # Unfold lines (RFC 5545: line continuations start with space or tab)
        unfolded = re.sub(r"\r?\n[ \t]", "", raw_ics)
        lines = unfolded.splitlines()

        reservations: List[Reservation] = []
        in_vevent = False
        current_event: Dict[str, str] = {}

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line == "BEGIN:VEVENT":
                in_vevent = True
                current_event = {}
                continue
            elif line == "END:VEVENT":
                in_vevent = False
                res = self._build_reservation_from_dict(current_event)
                if res:
                    reservations.append(res)
                continue

            if in_vevent:
                if ":" in line:
                    key_part, val_part = line.split(":", 1)
                    key_norm = key_part.split(";")[0].upper()
                    current_event[key_norm] = val_part
        return self.merge_adjacent_blocked_dates(reservations)

    @staticmethod
    def merge_adjacent_blocked_dates(reservations: List[Reservation]) -> List[Reservation]:
        """
        Merge contiguous adjacent BLOCKED reservations into continuous stays.
        For example:
          Block 1: 2026-10-16 to 2026-10-17
          Block 2: 2026-10-17 to 2026-10-18
        merges into:
          Merged: 2026-10-16 to 2026-10-18
        Confirmed guest bookings are preserved as distinct, discrete stays and are never merged with blocks.
        """
        non_blocked = [r for r in reservations if r.reservation_type != ReservationType.BLOCKED]
        blocked = [r for r in reservations if r.reservation_type == ReservationType.BLOCKED]

        if len(blocked) <= 1:
            return sorted(reservations, key=lambda x: x.start_date)

        # Sort blocks chronologically
        blocked.sort(key=lambda x: (x.start_date, x.end_date))

        merged_blocked: List[Reservation] = []
        for b in blocked:
            if not merged_blocked:
                merged_blocked.append(b)
                continue

            prev = merged_blocked[-1]
            # Check if contiguous (prev.end_date >= b.start_date)
            if prev.end_date >= b.start_date:
                if b.end_date > prev.end_date:
                    merged_res = Reservation(
                        uid=f"{prev.uid}_merged_{b.end_date.isoformat()}",
                        start_date=prev.start_date,
                        end_date=b.end_date,
                        summary=f"{prev.summary} (Merged)",
                        reservation_type=ReservationType.BLOCKED,
                        description=prev.description,
                        raw_status="BLOCKED_MERGED"
                    )
                    merged_blocked[-1] = merged_res
            else:
                merged_blocked.append(b)

        combined = non_blocked + merged_blocked
        combined.sort(key=lambda x: (x.start_date, x.end_date))
        return combined

    def _parse_date(self, date_str: str) -> Optional[date]:
        """Parse iCal date or datetime string to a date object."""
        date_str = date_str.strip()
        # YYYYMMDD
        if len(date_str) == 8 and date_str.isdigit():
            try:
                return datetime.strptime(date_str, "%Y%m%d").date()
            except ValueError:
                return None
        # YYYYMMDDTHHMMSS or YYYYMMDDTHHMMSSZ
        if "T" in date_str:
            clean = date_str.replace("Z", "")
            try:
                return datetime.strptime(clean[:15], "%Y%m%dT%H%M%S").date()
            except ValueError:
                return None
        return None

    def _build_reservation_from_dict(self, event_data: Dict[str, str]) -> Optional[Reservation]:
        """Convert raw VEVENT fields to structured Reservation with classification."""
        dtstart_raw = event_data.get("DTSTART")
        dtend_raw = event_data.get("DTEND")
        uid = event_data.get("UID", "").strip()
        summary = event_data.get("SUMMARY", "").strip()
        description = event_data.get("DESCRIPTION", "").strip()
        status = event_data.get("STATUS", "CONFIRMED").upper()

        if not dtstart_raw or not dtend_raw or not uid:
            return None

        start_date = self._parse_date(dtstart_raw)
        end_date = self._parse_date(dtend_raw)

        if not start_date or not end_date or end_date <= start_date:
            return None

        # Classify reservation type based on Airbnb and multi-channel conventions
        res_type = self._classify_reservation(summary, description, uid, status)

        return Reservation(
            uid=uid,
            start_date=start_date,
            end_date=end_date,
            summary=summary,
            reservation_type=res_type,
            description=description,
            raw_status=status
        )

    def _classify_reservation(
        self,
        summary: str,
        description: str = "",
        uid: str = "",
        status: str = "CONFIRMED"
    ) -> ReservationType:
        """
        Classify between CONFIRMED (guest reservation), BLOCKED (hold/host block), and CANCELLED.
        Differentiates Airbnb native bookings, imported external OTA bookings (Booking.com, VRBO),
        and manual host blocks without false unoccupied classifications.
        """
        if status in ("CANCELLED", "CANCELED"):
            return ReservationType.CANCELLED

        summary_lower = summary.lower()
        desc_lower = description.lower()
        uid_lower = uid.lower()

        # 1. Direct confirmed booking signals
        # Airbnb: "Reserved", "Reserved - John", "Reservation"
        if "reserved" in summary_lower or "reservation" in summary_lower:
            return ReservationType.CONFIRMED

        # Airbnb description with reservation link or booking code
        if (
            "airbnb.com/reservation" in desc_lower
            or "airbnb.com/hosting/reservations" in desc_lower
            or "reservation url:" in desc_lower
        ):
            return ReservationType.CONFIRMED

        # 2. Imported OTA channel booking signals (Booking.com, VRBO, Expedia, etc.)
        if AutomationConfig.TREAT_IMPORTED_CHANNELS_AS_CONFIRMED:
            ota_channel_keywords = [
                "booking.com",
                "booking",
                "vrbo",
                "homeaway",
                "expedia",
                "tripadvisor",
                "agoda",
                "channex",
            ]
            for kw in ota_channel_keywords:
                if kw in summary_lower or kw in desc_lower or kw in uid_lower:
                    _LOGGER.info(f"Classified event '{summary}' ({mask_uid(uid)}) as CONFIRMED via imported OTA channel: {kw}")
                    return ReservationType.CONFIRMED

            # Booking.com native iCal convention: SUMMARY:CLOSED - Not available / CLOSED - Guest
            if (
                summary_lower.startswith("closed")
                or "@booking.com" in uid_lower
                or "admin.booking.com" in uid_lower
            ):
                _LOGGER.info(f"Classified event '{summary}' ({mask_uid(uid)}) as CONFIRMED via Booking.com signature")
                return ReservationType.CONFIRMED

        # 3. Blocked dates (Host manual blocks or availability constraints)
        if "not available" in summary_lower or "blocked" in summary_lower:
            return ReservationType.BLOCKED

        # Default fallback: safe side, do NOT assume occupied unless positive evidence of reservation exists
        return ReservationType.BLOCKED

    @staticmethod
    def mask_url(url: Optional[str]) -> str:
        """Mask sensitive query parameters and authentication tokens from iCal URLs for safe logging."""
        if not url:
            return ""
        return "[REDACTED_CALENDAR_URL]"

    def sync_calendar(
        self,
        url: Optional[str] = None,
        fallback_file: Optional[Path] = None,
        known_previous_uids: Optional[Set[str]] = None
    ) -> CalendarSyncResult:
        """
        Fetch calendar from URL with automatic local caching and fallback.
        Also detects if previously confirmed bookings have been cancelled/removed.
        """
        prev_uids = known_previous_uids if known_previous_uids is not None else self._previously_known_uids
        target_url = url or AutomationConfig.AIRBNB_ICAL_URL
        urls = [u.strip() for u in re.split(r"[,;]", target_url) if u.strip()] if target_url else []
        raw_content: Optional[str] = None
        source_desc = "unknown"
        is_from_cache = False
        cache_age: Optional[float] = None
        error_msg: Optional[str] = None

        # 1a. Multi-URL Network Fetch (Multi-Channel Sync: Airbnb + Booking.com)
        if len(urls) > 1:
            all_reservations: List[Reservation] = []
            successful_sources = []
            combined_ics_parts = []
            for u in urls:
                if u.startswith(("http://", "https://")):
                    try:
                        req = urllib.request.Request(u, headers={"User-Agent": "TCL-Airbnb-HVAC-Automation/1.0"})
                        with http_transport.open_https(req, timeout=10) as resp:
                            part_content = resp.read().decode("utf-8", errors="replace")
                            all_reservations.extend(self.parse_ics_content(part_content))
                            successful_sources.append(u)
                            combined_ics_parts.append(part_content)
                    except Exception as e:
                        _LOGGER.warning(f"Network fetch failed for channel feed {self.mask_url(u)} ({type(e).__name__})")

            if all_reservations:
                # Deduplicate by (start_date, end_date) prioritizing confirmed bookings
                seen_dates = set()
                deduped: List[Reservation] = []
                # Sort so confirmed reservations take precedence over blocks on same dates
                for r in sorted(all_reservations, key=lambda x: (not x.is_confirmed, x.start_date)):
                    date_key = (r.start_date, r.end_date)
                    if date_key not in seen_dates:
                        seen_dates.add(date_key)
                        deduped.append(r)

                current_confirmed_uids = {r.uid for r in deduped if r.is_confirmed}
                cancelled_uids = list(prev_uids - current_confirmed_uids)
                self._previously_known_uids = current_confirmed_uids

                raw_content = "\n".join(combined_ics_parts)
                if self.cache_file:
                    try:
                        self.cache_file.write_text(raw_content, encoding="utf-8")
                    except Exception as e:
                        _LOGGER.warning(f"Could not write cache file: {e}")

                return CalendarSyncResult(
                    reservations=deduped,
                    source=f"multi_url ({len(successful_sources)}/{len(urls)} feeds synced)",
                    cancelled_uids=cancelled_uids,
                    is_stale=False,
                    error_message=None
                )

        # 1b. Single-URL Attempt Network Fetch
        if len(urls) == 1 and urls[0].startswith(("http://", "https://")):
            single_url = urls[0]
            try:
                req = urllib.request.Request(
                    single_url,
                    headers={"User-Agent": "TCL-Airbnb-HVAC-Automation/1.0"}
                )
                with http_transport.open_https(req, timeout=10) as resp:
                    raw_content = resp.read().decode("utf-8", errors="replace")
                    source_desc = f"live_url ({self.mask_url(single_url)})"
                    # Update local cache
                    if self.cache_file:
                        try:
                            self.cache_file.write_text(raw_content, encoding="utf-8")
                        except Exception as e:
                            _LOGGER.warning(f"Could not write cache file: {e}")
                    # Update S3 cache if configured
                    if AutomationConfig.S3_BUCKET:
                        try:
                            import boto3
                            s3 = boto3.client("s3")
                            s3.put_object(
                                Bucket=AutomationConfig.S3_BUCKET,
                                Key=AutomationConfig.S3_CALENDAR_KEY,
                                Body=raw_content.encode("utf-8"),
                                ContentType="text/calendar"
                            )
                        except Exception as e:
                            _LOGGER.warning(f"Could not write S3 calendar cache: {e}")
            except Exception as e:
                error_msg = f"Network fetch failed from {self.mask_url(single_url)} ({type(e).__name__})"
                _LOGGER.warning(error_msg)

        # 2. Attempt Fallback File if network failed or no URL
        if not raw_content and fallback_file and fallback_file.exists():
            try:
                raw_content = fallback_file.read_text(encoding="utf-8")
                source_desc = f"fallback_file ({fallback_file.name})"
            except Exception as e:
                error_msg = f"Failed to read fallback file {fallback_file}: {e}"

        # 3. Attempt Cache File (Local or S3) if still no content
        if not raw_content and self.cache_file and self.cache_file.exists():
            try:
                raw_content = self.cache_file.read_text(encoding="utf-8")
                source_desc = "local_cache"
                is_from_cache = True
                mtime = self.cache_file.stat().st_mtime
                cache_age = datetime.now().timestamp() - mtime
            except Exception as e:
                error_msg = f"Failed to read cache file: {e}"

        if not raw_content and AutomationConfig.S3_BUCKET:
            try:
                import boto3
                s3 = boto3.client("s3")
                resp = s3.get_object(Bucket=AutomationConfig.S3_BUCKET, Key=AutomationConfig.S3_CALENDAR_KEY)
                raw_content = resp["Body"].read().decode("utf-8", errors="replace")
                source_desc = f"s3_cache (s3://{AutomationConfig.S3_BUCKET}/{AutomationConfig.S3_CALENDAR_KEY})"
                is_from_cache = True
                last_modified = resp.get("LastModified")
                if last_modified:
                    cache_age = datetime.now(last_modified.tzinfo).timestamp() - last_modified.timestamp()
            except Exception as e:
                _LOGGER.debug(f"S3 calendar fetch failed or not found: {e}")

        if not raw_content:
            return CalendarSyncResult(
                reservations=[],
                source="none",
                is_stale=True,
                error_message=error_msg or "No calendar source available"
            )

        reservations = self.parse_ics_content(raw_content)

        # 4. Check Staleness
        is_stale = False
        if is_from_cache and cache_age is not None:
            max_age_seconds = AutomationConfig.CALENDAR_MAX_STALE_HOURS * 3600
            if cache_age > max_age_seconds:
                is_stale = True
                stale_hrs = cache_age / 3600
                error_msg = f"Calendar cache is STALE ({stale_hrs:.1f} hours old, exceeds safe limit of {AutomationConfig.CALENDAR_MAX_STALE_HOURS} hours)"
                _LOGGER.error(error_msg)

        # 5. Cancellation Detection
        current_confirmed_uids = {r.uid for r in reservations if r.is_confirmed}
        cancelled_uids = list(prev_uids - current_confirmed_uids)
        self._previously_known_uids = current_confirmed_uids

        return CalendarSyncResult(
            reservations=reservations,
            source=source_desc,
            is_from_cache=is_from_cache,
            cache_age_seconds=cache_age,
            is_stale=is_stale,
            cancelled_uids=cancelled_uids,
            error_message=error_msg
        )
