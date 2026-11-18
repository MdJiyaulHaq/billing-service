from datetime import timedelta
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from billing.exceptions import InvalidInvoiceState, InvalidReturn, Overpayment
from billing.models import Invoice, PaymentReminder, Product, Return, ReturnItem
from billing.services import (
    cancel_invoice,
    process_return,
    record_payment,
)

from .base import (
    api_client,
    make_business,
    make_customer,
    make_invoice,
    make_product,
    make_user,
)


class ReturnServiceTests(TestCase):
    def setUp(self):
        self.business = make_business()
        self.user = make_user(self.business)
        self.customer = make_customer(self.business)
        self.product = make_product(self.business, price="100.00", stock=50)
        self.invoice = make_invoice(
            self.business, self.customer, [(self.product, 5)], user=self.user
        )
        self.item = self.invoice.items.get()
        self.product.refresh_from_db()

    def test_partial_return_reduces_stock_and_totals(self):
        # invoice: 5 x 100 = 500 subtotal, 65 tax, 565 total
        ret = process_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.item.pk, "quantity": 2}],
            reason="damaged",
            user=self.user,
        )

        self.assertEqual(ret.number, 1)
        self.assertEqual(ret.subtotal, Decimal("200.00"))
        self.assertEqual(ret.tax, Decimal("26.00"))
        self.assertEqual(ret.total, Decimal("226.00"))
        self.assertEqual(ret.items.count(), 1)

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 47)  # 50 - 5 sold + 2 returned

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.subtotal, Decimal("300.00"))
        self.assertEqual(self.invoice.tax, Decimal("39.00"))
        self.assertEqual(self.invoice.total, Decimal("339.00"))
        self.assertEqual(self.invoice.balance, Decimal("339.00"))

    def test_second_partial_return_accumulates(self):
        process_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.item.pk, "quantity": 2}],
            user=self.user,
        )
        ret2 = process_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.item.pk, "quantity": 1}],
            user=self.user,
        )
        self.assertEqual(ret2.number, 2)

        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total, Decimal("226.00"))  # 2 x 113 left

    def test_full_return_zeroes_invoice(self):
        process_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.item.pk, "quantity": 5}],
            user=self.user,
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.total, Decimal("0.00"))
        self.assertEqual(self.invoice.balance, Decimal("0.00"))

        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 50)

    def test_over_return_rejected(self):
        process_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.item.pk, "quantity": 4}],
            user=self.user,
        )
        with self.assertRaises(InvalidReturn):
            process_return(
                invoice=self.invoice,
                items=[{"invoice_item_id": self.item.pk, "quantity": 2}],
                user=self.user,
            )

    def test_item_from_other_invoice_rejected(self):
        other = make_product(self.business, sku="SKU-2")
        other_invoice = make_invoice(
            self.business, self.customer, [(other, 1)], user=self.user
        )
        other_item = other_invoice.items.get()

        with self.assertRaises(InvalidReturn):
            process_return(
                invoice=self.invoice,
                items=[{"invoice_item_id": other_item.pk, "quantity": 1}],
                user=self.user,
            )

    def test_return_on_cancelled_invoice_rejected(self):
        cancel_invoice(self.invoice)
        with self.assertRaises(InvalidInvoiceState):
            process_return(
                invoice=self.invoice,
                items=[{"invoice_item_id": self.item.pk, "quantity": 1}],
                user=self.user,
            )

    def test_paid_invoice_return_creates_credit(self):
        record_payment(
            invoice=self.invoice, amount=self.invoice.total, method="cash", user=self.user
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

        process_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.item.pk, "quantity": 5}],
            user=self.user,
        )
        self.invoice.refresh_from_db()
        # paid 565, total now 0 -> customer is owed 565 (negative balance)
        self.assertEqual(self.invoice.balance, Decimal("-565.00"))
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)

    def test_cancel_after_partial_return_restock_only_unreturned(self):
        # sell 5 (stock 45), return 2 (stock 47), cancel -> restock 3 -> stock 50
        process_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.item.pk, "quantity": 2}],
            user=self.user,
        )
        cancel_invoice(self.invoice)
        self.product.refresh_from_db()
        self.assertEqual(self.product.stock, 50)

    def test_payment_cannot_exceed_balance(self):
        with self.assertRaises(Overpayment):
            record_payment(
                invoice=self.invoice,
                amount=self.invoice.total + Decimal("1"),
                method="cash",
                user=self.user,
            )

    def test_payment_capped_at_post_return_balance(self):
        process_return(
            invoice=self.invoice,
            items=[{"invoice_item_id": self.item.pk, "quantity": 4}],
            user=self.user,
        )
        self.invoice.refresh_from_db()
        with self.assertRaises(Overpayment):
            record_payment(
                invoice=self.invoice,
                amount=self.invoice.total + Decimal("1"),
                method="cash",
                user=self.user,
            )
        # but paying the new balance works
        record_payment(
            invoice=self.invoice,
            amount=self.invoice.balance,
            method="cash",
            user=self.user,
        )
        self.invoice.refresh_from_db()
        self.assertEqual(self.invoice.status, Invoice.Status.PAID)


class ReturnAPITests(TestCase):
    def setUp(self):
        self.business = make_business()
        self.other_business = make_business("Other Shop")
        self.user = make_user(self.business)
        self.customer = make_customer(self.business)
        self.product = make_product(self.business, price="100.00", stock=50)
        self.invoice = make_invoice(
            self.business, self.customer, [(self.product, 3)], user=self.user
        )
        self.item = self.invoice.items.get()
        self.client = api_client(self.user)

    def test_create_return_via_api(self):
        response = self.client.post(
            f"/api/invoices/{self.invoice.pk}/returns/",
            {"reason": "wrong size", "items": [{"invoice_item_id": self.item.pk, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["number"], 1)
        self.assertEqual(response.data["total"], "113.00")
        self.assertEqual(len(response.data["items"]), 1)

    def test_list_returns_via_api(self):
        self.client.post(
            f"/api/invoices/{self.invoice.pk}/returns/",
            {"items": [{"invoice_item_id": self.item.pk, "quantity": 1}]},
            format="json",
        )
        response = self.client.get(f"/api/invoices/{self.invoice.pk}/returns/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)

    def test_return_rejects_bad_quantity(self):
        response = self.client.post(
            f"/api/invoices/{self.invoice.pk}/returns/",
            {"items": [{"invoice_item_id": self.item.pk, "quantity": 99}]},
            format="json",
        )
        self.assertEqual(response.status_code, 400)

    def test_other_business_cannot_return(self):
        other_user = make_user(self.other_business, username="other")
        other_client = api_client(other_user)
        response = other_client.post(
            f"/api/invoices/{self.invoice.pk}/returns/",
            {"items": [{"invoice_item_id": self.item.pk, "quantity": 1}]},
            format="json",
        )
        self.assertEqual(response.status_code, 404)

    def test_payment_on_other_business_invoice_rejected(self):
        other_user = make_user(self.other_business, username="other2")
        other_client = api_client(other_user)
        response = other_client.post(
            f"/api/invoices/{self.invoice.pk}/payments/",
            {"amount": "10.00", "method": "cash"},
            format="json",
        )
        self.assertEqual(response.status_code, 404)
