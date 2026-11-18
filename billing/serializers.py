from decimal import Decimal

from rest_framework import serializers

from .models import Customer, Invoice, InvoiceItem, Payment, Product, Return, ReturnItem


class CustomerSerializer(serializers.ModelSerializer):
    class Meta:
        model = Customer
        fields = ["id", "name", "phone", "email", "created_at"]
        read_only_fields = ["created_at"]


class ProductSerializer(serializers.ModelSerializer):
    class Meta:
        model = Product
        fields = ["id", "sku", "name", "unit_price", "stock"]


class InvoiceItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="product.name", read_only=True)

    class Meta:
        model = InvoiceItem
        fields = ["id", "product", "product_name", "quantity", "unit_price", "line_total"]


class InvoiceSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    items = InvoiceItemSerializer(many=True, read_only=True)
    balance = serializers.DecimalField(max_digits=14, decimal_places=2, read_only=True)

    class Meta:
        model = Invoice
        fields = "__all__"


class InvoiceLineInputSerializer(serializers.Serializer):
    product_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class InvoiceCreateSerializer(serializers.Serializer):
    customer_id = serializers.IntegerField()
    due_date = serializers.DateField()
    items = InvoiceLineInputSerializer(many=True, allow_empty=False)

    def validate_customer_id(self, value):
        business = self.context["request"].user.profile.business
        if not Customer.objects.filter(pk=value, business=business).exists():
            raise serializers.ValidationError("Customer not found.")
        return value

    def validate_items(self, value):
        product_ids = [row["product_id"] for row in value]
        if len(product_ids) != len(set(product_ids)):
            raise serializers.ValidationError("Each product can appear only once.")
        return value


class PaymentCreateSerializer(serializers.Serializer):
    amount = serializers.DecimalField(max_digits=14, decimal_places=2, min_value=Decimal("0.01"))
    method = serializers.ChoiceField(choices=Payment.Method.choices)
    reference = serializers.CharField(required=False, allow_blank=True, max_length=100)


class ReturnItemInputSerializer(serializers.Serializer):
    invoice_item_id = serializers.IntegerField()
    quantity = serializers.IntegerField(min_value=1)


class ReturnCreateSerializer(serializers.Serializer):
    reason = serializers.CharField(required=False, allow_blank=True, max_length=500)
    items = ReturnItemInputSerializer(many=True, allow_empty=False)

    def validate_items(self, value):
        ids = [row["invoice_item_id"] for row in value]
        if len(ids) != len(set(ids)):
            raise serializers.ValidationError("Each invoice item can appear only once.")
        return value


class ReturnItemSerializer(serializers.ModelSerializer):
    product_name = serializers.CharField(source="invoice_item.product.name", read_only=True)
    sku = serializers.CharField(source="invoice_item.product.sku", read_only=True)

    class Meta:
        model = ReturnItem
        fields = [
            "id",
            "invoice_item",
            "product_name",
            "sku",
            "quantity",
            "unit_price",
            "line_total",
        ]


class ReturnSerializer(serializers.ModelSerializer):
    invoice_number = serializers.IntegerField(source="invoice.number", read_only=True)
    customer_name = serializers.CharField(source="invoice.customer.name", read_only=True)
    items = ReturnItemSerializer(many=True, read_only=True)
    created_by_name = serializers.CharField(source="created_by.username", read_only=True)

    class Meta:
        model = Return
        fields = [
            "id",
            "number",
            "invoice",
            "invoice_number",
            "customer_name",
            "reason",
            "subtotal",
            "tax",
            "total",
            "items",
            "created_by_name",
            "created_at",
        ]
        read_only_fields = fields
