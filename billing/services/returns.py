from decimal import Decimal

from django.db import transaction
from django.db.models import F, Max, Prefetch

from ..exceptions import InvalidInvoiceState, InvalidReturn
from ..models import Invoice, InvoiceItem, Product, Return, ReturnItem
from .invoices import (
    VAT_RATE,
    CENT,
    recalculate_invoice_totals,
    refresh_invoice_status,
    with_returned_quantities,
)


def next_return_number(business):
    last = Return.objects.filter(business=business).aggregate(n=Max("number"))["n"]
    return (last or 0) + 1


@transaction.atomic
def process_return(*, invoice, items, reason="", user=None):
    """Process a customer return against an invoice.

    Restocks returned quantities, issues a credit note (Return), and
    recomputes the invoice totals from the quantities still sold.
    """
    locked = (
        Invoice.objects.select_for_update()
        .prefetch_related(
            Prefetch(
                "items",
                queryset=with_returned_quantities(
                    InvoiceItem.objects.select_related("product")
                ),
            )
        )
        .get(pk=invoice.pk)
    )

    if locked.status in (Invoice.Status.DRAFT, Invoice.Status.CANCELLED):
        raise InvalidInvoiceState(
            f"Cannot return items on a {locked.status} invoice."
        )

    requested = {}
    for row in items:
        item_id = row["invoice_item_id"]
        requested[item_id] = requested.get(item_id, 0) + row["quantity"]

    lines = {item.pk: item for item in locked.items.all()}
    for item_id, quantity in requested.items():
        item = lines.get(item_id)
        if item is None:
            raise InvalidReturn("One or more items do not belong to this invoice.")
        remaining = item.quantity - item.returned_qty
        if quantity > remaining:
            raise InvalidReturn(
                f"{item.product.sku}: {remaining} returnable, {quantity} requested."
            )

    return_note = Return.objects.create(
        business=locked.business,
        invoice=locked,
        number=next_return_number(locked.business),
        reason=reason,
        created_by=user,
    )

    subtotal = Decimal("0")
    restock = {}
    return_items = []
    for item_id, quantity in requested.items():
        item = lines[item_id]
        line_total = item.unit_price * quantity
        return_items.append(
            ReturnItem(
                return_note=return_note,
                invoice_item=item,
                quantity=quantity,
                unit_price=item.unit_price,
                line_total=line_total,
            )
        )
        subtotal += line_total
        restock[item.product_id] = restock.get(item.product_id, 0) + quantity
    ReturnItem.objects.bulk_create(return_items)

    return_note.subtotal = subtotal
    return_note.tax = (subtotal * VAT_RATE).quantize(CENT)
    return_note.total = return_note.subtotal + return_note.tax
    return_note.save(update_fields=["subtotal", "tax", "total"])

    for product_id, quantity in restock.items():
        Product.objects.filter(pk=product_id).update(stock=F("stock") + quantity)

    recalculate_invoice_totals(locked)
    refresh_invoice_status(locked)
    locked.save(update_fields=["status"])

    invoice.refresh_from_db()
    return return_note
