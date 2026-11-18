from decimal import Decimal

from django.db import transaction
from django.db.models import F, Max, Sum, Value
from django.db.models.functions import Coalesce

from ..exceptions import (
    InsufficientStock,
    InvalidAmount,
    InvalidInvoiceState,
    Overpayment,
)
from ..models import Invoice, InvoiceItem, Payment, Product
from ..tasks import send_invoice_email

VAT_RATE = Decimal("0.13")
CENT = Decimal("0.01")


def next_invoice_number(business):
    last = Invoice.objects.filter(business=business).aggregate(n=Max("number"))["n"]
    return (last or 0) + 1


def with_returned_quantities(queryset):
    """Annotate each InvoiceItem with the quantity already sent back."""
    return queryset.annotate(
        returned_qty=Coalesce(Sum("return_items__quantity"), Value(0))
    )


def refresh_invoice_status(invoice):
    """Derive status from amount_paid vs total. Shared by payments and returns."""
    if invoice.amount_paid > 0 and invoice.amount_paid >= invoice.total:
        invoice.status = Invoice.Status.PAID
    elif invoice.amount_paid > 0:
        invoice.status = Invoice.Status.PARTIALLY_PAID
    else:
        invoice.status = Invoice.Status.ISSUED
    return invoice


def calculate_totals(invoice):
    subtotal = Decimal("0")
    for item in invoice.items.all():
        subtotal += item.unit_price * item.quantity
    tax = (subtotal * VAT_RATE).quantize(CENT)
    invoice.subtotal = subtotal
    invoice.tax = tax
    invoice.total = subtotal + tax


def recalculate_invoice_totals(invoice):
    """Recompute totals from quantities not yet returned (post-return)."""
    items = with_returned_quantities(InvoiceItem.objects.filter(invoice=invoice))
    subtotal = Decimal("0")
    for item in items:
        remaining = item.quantity - item.returned_qty
        subtotal += item.unit_price * remaining
    invoice.subtotal = subtotal
    invoice.tax = (subtotal * VAT_RATE).quantize(CENT)
    invoice.total = invoice.subtotal + invoice.tax
    invoice.save(update_fields=["subtotal", "tax", "total"])


@transaction.atomic
def create_invoice(*, business, customer, due_date, items, user):
    invoice = Invoice.objects.create(
        business=business,
        customer=customer,
        number=next_invoice_number(business),
        status=Invoice.Status.ISSUED,
        due_date=due_date,
        created_by=user,
    )

    for row in items:
        product = Product.objects.get(pk=row["product_id"], business=business)
        quantity = row["quantity"]
        if product.stock < quantity:
            raise InsufficientStock(product.sku, product.stock, quantity)

        product.stock -= quantity
        product.save(update_fields=["stock"])

        InvoiceItem.objects.create(
            invoice=invoice,
            product=product,
            quantity=quantity,
            unit_price=product.unit_price,
            line_total=product.unit_price * quantity,
        )

    calculate_totals(invoice)
    invoice.save(update_fields=["subtotal", "tax", "total"])

    send_invoice_email.delay(invoice.id)
    return invoice


@transaction.atomic
def record_payment(*, invoice, amount, method, user=None, gateway_ref=""):
    locked = Invoice.objects.select_for_update().get(pk=invoice.pk)

    if locked.status == Invoice.Status.CANCELLED:
        raise InvalidInvoiceState("Cannot record a payment on a cancelled invoice.")
    if amount <= 0:
        raise InvalidAmount("Payment amount must be positive.")
    if amount > locked.balance:
        raise Overpayment(
            f"Payment of {amount} exceeds the invoice balance of {locked.balance}."
        )

    payment = Payment.objects.create(
        invoice=locked,
        amount=amount,
        method=method,
        gateway_ref=gateway_ref,
        recorded_by=user,
    )

    locked.amount_paid += amount
    refresh_invoice_status(locked)
    locked.save(update_fields=["amount_paid", "status"])

    invoice.refresh_from_db(fields=["amount_paid", "status"])
    return payment


@transaction.atomic
def cancel_invoice(invoice):
    locked = Invoice.objects.select_for_update().get(pk=invoice.pk)
    items = with_returned_quantities(
        InvoiceItem.objects.filter(invoice=locked).select_related("product")
    )
    for item in items:
        restock_qty = item.quantity - item.returned_qty
        if restock_qty > 0:
            Product.objects.filter(pk=item.product_id).update(
                stock=F("stock") + restock_qty
            )
    locked.status = Invoice.Status.CANCELLED
    locked.save(update_fields=["status"])
    invoice.refresh_from_db(fields=["status"])
    return invoice
