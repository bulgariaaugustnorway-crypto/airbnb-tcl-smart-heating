"""Unit tests for RFC 5545 Airbnb iCal parsing, classification, and cancellation detection."""

import unittest
from datetime import date
from pathlib import Path

from src.calendar_parser import AirbnbCalendarParser, CalendarSyncResult
from src.models import ReservationType
from src.config import SAMPLE_CALENDARS_DIR


class TestAirbnbCalendarParser(unittest.TestCase):

    def setUp(self):
        self.parser = AirbnbCalendarParser()

    def test_parse_normal_stay(self):
        ics_file = SAMPLE_CALENDARS_DIR / "normal_stay.ics"
        raw_ics = ics_file.read_text(encoding="utf-8")
        reservations = self.parser.parse_ics_content(raw_ics)

        self.assertEqual(len(reservations), 1)
        res = reservations[0]
        self.assertEqual(res.uid, "synthetic-6719abf47b@example.invalid")
        self.assertEqual(res.start_date, date(2026, 10, 20))
        self.assertEqual(res.end_date, date(2026, 10, 23))
        self.assertEqual(res.nights_count, 3)
        self.assertEqual(res.reservation_type, ReservationType.CONFIRMED)
        self.assertTrue(res.is_confirmed)

    def test_parse_blocked_dates_not_confirmed(self):
        ics_file = SAMPLE_CALENDARS_DIR / "blocked_dates.ics"
        raw_ics = ics_file.read_text(encoding="utf-8")
        reservations = self.parser.parse_ics_content(raw_ics)

        self.assertEqual(len(reservations), 2)
        for res in reservations:
            self.assertFalse(res.is_confirmed, f"Blocked entry {res.uid} should not be confirmed")
            self.assertEqual(res.reservation_type, ReservationType.BLOCKED)

    def test_consecutive_stays_parsing(self):
        ics_file = SAMPLE_CALENDARS_DIR / "consecutive_stays.ics"
        raw_ics = ics_file.read_text(encoding="utf-8")
        reservations = self.parser.parse_ics_content(raw_ics)

        self.assertEqual(len(reservations), 2)
        res_a, res_b = reservations[0], reservations[1]
        self.assertTrue(res_a.is_confirmed)
        self.assertTrue(res_b.is_confirmed)
        self.assertEqual(res_a.end_date, res_b.start_date)  # Same-day turnover

    def test_cancellation_detection(self):
        # Pass 1: Parse v1 (reservation exists)
        file_v1 = SAMPLE_CALENDARS_DIR / "cancellation_v1.ics"
        result_v1 = self.parser.sync_calendar(fallback_file=file_v1)
        self.assertEqual(len(result_v1.confirmed_reservations), 1)
        self.assertEqual(len(result_v1.cancelled_uids), 0)

        # Pass 2: Parse v2 (reservation removed)
        file_v2 = SAMPLE_CALENDARS_DIR / "cancellation_v2.ics"
        result_v2 = self.parser.sync_calendar(fallback_file=file_v2)
        self.assertEqual(len(result_v2.confirmed_reservations), 0)
        self.assertEqual(len(result_v2.cancelled_uids), 1)
        self.assertIn("synthetic-a6ed1b4af6@example.invalid", result_v2.cancelled_uids)

    def test_parse_imported_booking_com_stays(self):
        """Test parsing multi-channel iCal feed with imported Booking.com reservations."""
        ics_file = SAMPLE_CALENDARS_DIR / "imported_booking_com.ics"
        raw_ics = ics_file.read_text(encoding="utf-8")
        reservations = self.parser.parse_ics_content(raw_ics)

        self.assertEqual(len(reservations), 3)

        # 1. Booking.com imported into Airbnb
        bcom_ota = next(r for r in reservations if r.uid == "synthetic-495d0085a8@example.invalid")
        self.assertTrue(bcom_ota.is_confirmed)
        self.assertEqual(bcom_ota.reservation_type, ReservationType.CONFIRMED)
        self.assertEqual(bcom_ota.start_date, date(2026, 10, 20))
        self.assertEqual(bcom_ota.end_date, date(2026, 10, 24))

        # 2. Booking.com native CLOSED event
        bcom_native = next(r for r in reservations if r.uid == "synthetic-58090cc70f@example.invalid")
        self.assertTrue(bcom_native.is_confirmed)
        self.assertEqual(bcom_native.reservation_type, ReservationType.CONFIRMED)
        self.assertEqual(bcom_native.start_date, date(2026, 10, 25))
        self.assertEqual(bcom_native.end_date, date(2026, 10, 28))

        # 3. Host manual block
        manual_block = next(r for r in reservations if r.uid == "synthetic-15b6a4847a@example.invalid")
        self.assertFalse(manual_block.is_confirmed)
        self.assertEqual(manual_block.reservation_type, ReservationType.BLOCKED)


if __name__ == "__main__":
    unittest.main()
