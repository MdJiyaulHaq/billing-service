import logging
from datetime import timedelta

from django.db.models import Count, F, Max
from django.utils import timezone

from ..models import Invoice, PaymentReminder
from ..tasks import send_sms

logger = logging.getLogger(__name__)

SEND_WINDOW_START_HOUR = 8
SEND_WINDOW_END_HOUR = 20
REMINDER_MIN_INTERVAL = timedelta(days=3)
REMINDER_MAX_PER_INVOICE = 4
MAX_INVOICES_PER_MESSAGE = 5


def is_within_send_window(now):
    """SMS may only go out between 08:00 and 20:00 local time."""
    return SEND_WINDOW_START_HOUR <= now.hour < SEND_WINDOW_END_HOUR


def get_overdue_invoices(today):
    """Unpaid invoices past their due date, annotated for throttle checks.

    reminder_count / last_reminder_at come from aggregates, so the whole
    scan is a single query — no per-invoice reminder lookups.
    """
    return (
        Invoice.objects.filter(
            status__in=[Invoice.Status.ISSUED, Invoice.Status.PARTIALLY_PAID],
            due_date__lt=today,
            total__gt=F("amount_paid"),
        )
        .annotate(
            outstanding=F("total") - F("amount_paid"),
            reminder_count=Count("reminders"),
            last_reminder_at=Max("reminders__sent_at"),
        )
        .select_related("customer", "business")
        .order_by("due_date", "number")
    )


def is_reminder_due(invoice, now):
    if invoice.reminder_count >= REMINDER_MAX_PER_INVOICE:
        return False
    last_sent = invoice.last_reminder_at
    if last_sent is not None and now - last_sent < REMINDER_MIN_INTERVAL:
        return False
    return True


def build_reminder_message(customer, invoices):
    business = invoices[0].business
    numbers = [f"#{invoice.number}" for invoice in invoices[:MAX_INVOICES_PER_MESSAGE]]
    if len(invoices) > MAX_INVOICES_PER_MESSAGE:
        numbers.append(f"and {len(invoices) - MAX_INVOICES_PER_MESSAGE} more")
    total = sum(invoice.outstanding for invoice in invoices)
    return (
        f"Dear {customer.name}, invoice(s) {', '.join(numbers)} from "
        f"{business.name} are overdue. Total due: Rs. {total}. "
        f"Please pay at your earliest convenience."
    )


def send_overdue_reminders(now=None):
    """Send at most one reminder SMS per customer per run.

    Runs hourly via Celery beat; skips outside the send window, throttles
    per invoice, and groups all of a customer's overdue invoices into a
    single message so customers never get several SMS in one day.
    """
    now = now or timezone.localtime()
    today = now.date()

    if not is_within_send_window(now):
        logger.info("Reminder run skipped: outside send window at %s", now)
        return {"status": "skipped", "reason": "outside_send_window"}

    by_customer = {}
    for invoice in get_overdue_invoices(today):
        if not invoice.customer.phone or not is_reminder_due(invoice, now):
            continue
        group = by_customer.setdefault(
            invoice.customer_id, {"customer": invoice.customer, "invoices": []}
        )
        group["invoices"].append(invoice)

    customers_reminded = 0
    invoices_reminded = 0
    failures = 0

    for group in by_customer.values():
        customer = group["customer"]
        invoices = group["invoices"]
        try:
            send_sms(customer.phone, build_reminder_message(customer, invoices))
        except Exception:
            failures += 1
            logger.exception("Failed to send reminder SMS to customer %s", customer.pk)
            continue

        PaymentReminder.objects.bulk_create(
            [
                PaymentReminder(
                    invoice=invoice,
                    sent_at=now,
                    days_overdue=(today - invoice.due_date).days,
                    balance=invoice.outstanding,
                )
                for invoice in invoices
            ]
        )
        customers_reminded += 1
        invoices_reminded += len(invoices)

    return {
        "status": "completed",
        "customers_reminded": customers_reminded,
        "invoices_reminded": invoices_reminded,
        "failures": failures,
    }
