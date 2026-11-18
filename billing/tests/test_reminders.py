from datetime import datetime, timedelta
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone

from billing.models import Invoice, PaymentReminder
from billing.services import record_payment
from billing.services.reminders import (
    REMINDER_MAX_PER_INVOICE,
    REMINDER_MIN_INTERVAL,
    build_reminder_message,
    is_reminder_due,
    is_within_send_window,
    send_overdue_reminders,
)

from .base import make_business, make_customer, make_invoice, make_product, make_user


def at(hour, minute=0, day=18):
    """Aware datetime on 2026-11-<day> at given local time."""
    return timezone.make_aware(datetime(2026, 11, day, hour, minute))


class SendWindowTests(TestCase):
    def test_window_boundaries(self):
        self.assertFalse(is_within_send_window(at(7, 59)))
        self.assertTrue(is_within_send_window(at(8, 0)))
        self.assertTrue(is_within_send_window(at(19, 59)))
        self.assertFalse(is_within_send_window(at(20, 0)))
        self.assertFalse(is_within_send_window(at(23, 0)))


class ReminderScanTests(TestCase):
    def setUp(self):
        self.business = make_business()
        self.user = make_user(self.business)
        self.customer = make_customer(self.business, phone="9800000001")
        self.product = make_product(self.business, price="100.00", stock=50)
        self.yesterday = timezone.localdate() - timedelta(days=1)

    def make_overdue_invoice(self, qty=1, phone=None):
        customer = self.customer
        if phone is not None:
            customer = make_customer(self.business, name=f"C{phone}", phone=phone)
        return make_invoice(
            self.business,
            customer,
            [(self.product, qty)],
            due_date=self.yesterday,
            user=self.user,
        )

    def test_overdue_invoice_is_reminder_due(self):
        invoice = self.make_overdue_invoice()
        now = at(10)
        from billing.services.reminders import get_overdue_invoices

        found = list(get_overdue_invoices(now.date()))
        self.assertEqual([i.pk for i in found], [invoice.pk])
        self.assertTrue(is_reminder_due(found[0], now))

    def test_paid_invoice_not_scanned(self):
        invoice = self.make_overdue_invoice()
        record_payment(
            invoice=invoice, amount=invoice.total, method="cash", user=self.user
        )
        from billing.services.reminders import get_overdue_invoices

        self.assertEqual(list(get_overdue_invoices(self.yesterday)), [])

    def test_future_due_date_not_scanned(self):
        invoice = make_invoice(
            self.business,
            self.customer,
            [(self.product, 1)],
            due_date=timezone.localdate() + timedelta(days=5),
            user=self.user,
        )
        from billing.services.reminders import get_overdue_invoices

        self.assertNotIn(invoice, get_overdue_invoices(self.yesterday))

    def test_recent_reminder_blocks_resend(self):
        invoice = self.make_overdue_invoice()
        now = at(10)
        PaymentReminder.objects.create(
            invoice=invoice, sent_at=now - timedelta(hours=2), days_overdue=1
        )
        from billing.services.reminders import get_overdue_invoices

        found = get_overdue_invoices(now.date()).get(pk=invoice.pk)
        self.assertFalse(is_reminder_due(found, now))

    def test_old_reminder_allows_resend(self):
        invoice = self.make_overdue_invoice()
        now = at(10)
        PaymentReminder.objects.create(
            invoice=invoice,
            sent_at=now - REMINDER_MIN_INTERVAL - timedelta(hours=1),
            days_overdue=1,
        )
        # need fresh annotation values; refetch via scan
        from billing.services.reminders import get_overdue_invoices

        found = get_overdue_invoices(now.date()).get(pk=invoice.pk)
        self.assertTrue(is_reminder_due(found, now))

    def test_max_reminders_blocks_resend(self):
        invoice = self.make_overdue_invoice()
        now = at(10)
        old = now - REMINDER_MIN_INTERVAL - timedelta(days=1)
        PaymentReminder.objects.bulk_create(
            [
                PaymentReminder(invoice=invoice, sent_at=old, days_overdue=1)
                for _ in range(REMINDER_MAX_PER_INVOICE)
            ]
        )
        from billing.services.reminders import get_overdue_invoices

        found = get_overdue_invoices(now.date()).get(pk=invoice.pk)
        self.assertFalse(is_reminder_due(found, now))


class SendOverdueRemindersTests(TestCase):
    def setUp(self):
        self.business = make_business()
        self.user = make_user(self.business)
        self.product = make_product(self.business, price="100.00", stock=50)
        self.yesterday = timezone.localdate() - timedelta(days=1)

    def make_overdue(self, name="Ram", phone="9800000001", qty=1):
        customer = make_customer(self.business, name=name, phone=phone)
        return make_invoice(
            self.business,
            customer,
            [(self.product, qty)],
            due_date=self.yesterday,
            user=self.user,
        )

    @mock.patch("billing.services.reminders.send_sms")
    def test_sends_sms_and_records_reminder(self, mock_sms):
        invoice = self.make_overdue()
        result = send_overdue_reminders(now=at(10))

        self.assertEqual(result["customers_reminded"], 1)
        self.assertEqual(result["invoices_reminded"], 1)
        mock_sms.assert_called_once()
        phone, text = mock_sms.call_args[0]
        self.assertEqual(phone, "9800000001")
        self.assertIn(f"#{invoice.number}", text)
        self.assertEqual(PaymentReminder.objects.count(), 1)
        reminder = PaymentReminder.objects.get()
        self.assertEqual(reminder.invoice, invoice)
        self.assertEqual(reminder.days_overdue, 1)

    @mock.patch("billing.services.reminders.send_sms")
    def test_quiet_hours_skip(self, mock_sms):
        self.make_overdue()
        result = send_overdue_reminders(now=at(22))
        self.assertEqual(result["status"], "skipped")
        mock_sms.assert_not_called()
        self.assertEqual(PaymentReminder.objects.count(), 0)

    @mock.patch("billing.services.reminders.send_sms")
    def test_second_run_same_day_does_not_resend(self, mock_sms):
        self.make_overdue()
        send_overdue_reminders(now=at(10))
        result = send_overdue_reminders(now=at(11))
        self.assertEqual(result["customers_reminded"], 0)
        self.assertEqual(mock_sms.call_count, 1)

    @mock.patch("billing.services.reminders.send_sms")
    def test_multiple_invoices_one_sms_per_customer(self, mock_sms):
        customer = make_customer(self.business, name="Multi", phone="9800000002")
        inv1 = make_invoice(
            self.business, customer, [(self.product, 1)],
            due_date=self.yesterday, user=self.user,
        )
        inv2 = make_invoice(
            self.business, customer, [(self.product, 2)],
            due_date=self.yesterday, user=self.user,
        )
        result = send_overdue_reminders(now=at(10))

        self.assertEqual(result["customers_reminded"], 1)
        self.assertEqual(result["invoices_reminded"], 2)
        mock_sms.assert_called_once()
        text = mock_sms.call_args[0][1]
        self.assertIn(f"#{inv1.number}", text)
        self.assertIn(f"#{inv2.number}", text)
        self.assertEqual(PaymentReminder.objects.count(), 2)

    @mock.patch("billing.services.reminders.send_sms")
    def test_no_phone_skipped(self, mock_sms):
        self.make_overdue(phone="")
        result = send_overdue_reminders(now=at(10))
        self.assertEqual(result["customers_reminded"], 0)
        mock_sms.assert_not_called()

    @mock.patch("billing.services.reminders.send_sms")
    def test_sms_failure_not_recorded(self, mock_sms):
        self.make_overdue()
        mock_sms.side_effect = Exception("gateway down")
        result = send_overdue_reminders(now=at(10))
        self.assertEqual(result["failures"], 1)
        self.assertEqual(PaymentReminder.objects.count(), 0)

    def test_message_format(self):
        customer = make_customer(self.business, name="Sita", phone="1")
        invoice = make_invoice(
            self.business, customer, [(self.product, 2)],
            due_date=self.yesterday, user=self.user,
        )
        invoice.outstanding = Decimal("226.00")
        message = build_reminder_message(customer, [invoice])
        self.assertIn("Sita", message)
        self.assertIn(f"#{invoice.number}", message)
        self.assertIn("226.00", message)
        self.assertIn(self.business.name, message)
