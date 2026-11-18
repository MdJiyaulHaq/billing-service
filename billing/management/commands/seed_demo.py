from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand
from django.utils import timezone

from billing.models import Business, Customer, Product, StaffProfile
from billing.services import (
    create_invoice,
    process_return,
    record_payment,
)

User = get_user_model()


class Command(BaseCommand):
    help = "Populate the database with demo data for manual testing."

    def handle(self, *args, **options):
        business = Business.objects.filter(name="Demo Shop").first()
        if business is None:
            business = Business.objects.create(name="Demo Shop", pan_number="123456789")

        user = User.objects.filter(username="demo").first()
        if user is None:
            user = User.objects.create_user(username="demo", password="demo123")
        profile, _ = StaffProfile.objects.get_or_create(user=user)
        profile.business = business
        profile.role = StaffProfile.Role.OWNER
        profile.save()

        customers = [
            Customer.objects.get_or_create(
                business=business, name="Sita Rai", defaults={"phone": "9800000001", "email": "sita@example.com"}
            )[0],
            Customer.objects.get_or_create(
                business=business, name="Gita Sharma", defaults={"phone": "9800000002"}
            )[0],
            Customer.objects.get_or_create(
                business=business, name="Hari Thapa", defaults={"phone": ""}
            )[0],
        ]

        products = [
            Product.objects.get_or_create(
                business=business, sku="RICE-25", defaults={"name": "Rice 25kg", "unit_price": Decimal("3200.00"), "stock": 40}
            )[0],
            Product.objects.get_or_create(
                business=business, sku="OIL-5", defaults={"name": "Cooking Oil 5L", "unit_price": Decimal("1450.00"), "stock": 60}
            )[0],
            Product.objects.get_or_create(
                business=business, sku="SOAP", defaults={"name": "Soap Bar", "unit_price": Decimal("80.00"), "stock": 200}
            )[0],
        ]

        today = timezone.localdate()
        inv1 = create_invoice(
            business=business, customer=customers[0], due_date=today + timedelta(days=15),
            items=[{"product_id": products[0].pk, "quantity": 2}, {"product_id": products[2].pk, "quantity": 10}],
            user=user,
        )
        record_payment(invoice=inv1, amount=Decimal("5000.00"), method="cash", user=user)

        inv2 = create_invoice(
            business=business, customer=customers[1], due_date=today - timedelta(days=5),
            items=[{"product_id": products[1].pk, "quantity": 3}],
            user=user,
        )

        inv3 = create_invoice(
            business=business, customer=customers[2], due_date=today - timedelta(days=10),
            items=[{"product_id": products[2].pk, "quantity": 20}],
            user=user,
        )
        item = inv3.items.get()
        process_return(
            invoice=inv3,
            items=[{"invoice_item_id": item.pk, "quantity": 5}],
            reason="Customer changed mind",
            user=user,
        )

        self.stdout.write(self.style.SUCCESS(
            f"Seeded: 3 customers, 3 products, 3 invoices (1 paid-partial, 2 overdue), 1 return. Login: demo / demo123"
        ))
