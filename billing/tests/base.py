from datetime import timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.utils import timezone
from rest_framework.test import APIClient

from billing.models import Business, Customer, Product, StaffProfile
from billing.services import create_invoice

User = get_user_model()


def make_business(name="Test Shop"):
    return Business.objects.create(name=name)


def make_user(business, username="cashier", role=StaffProfile.Role.CASHIER):
    user = User.objects.create_user(username=username, password="pass123")
    StaffProfile.objects.create(user=user, business=business, role=role)
    return user


def make_customer(business, name="Ram Sharma", phone="9800000001", email=""):
    return Customer.objects.create(
        business=business, name=name, phone=phone, email=email
    )


def make_product(business, sku="SKU-1", price="100.00", stock=50, name=None):
    return Product.objects.create(
        business=business,
        sku=sku,
        name=name or sku,
        unit_price=Decimal(price),
        stock=stock,
    )


def make_invoice(
    business,
    customer,
    items,
    due_date=None,
    user=None,
):
    """items: list of (product, quantity) tuples."""
    return create_invoice(
        business=business,
        customer=customer,
        due_date=due_date or (timezone.localdate() + timedelta(days=15)),
        items=[{"product_id": p.pk, "quantity": q} for p, q in items],
        user=user,
    )


def api_client(user):
    client = APIClient()
    client.force_authenticate(user=user)
    return client
