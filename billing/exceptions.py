class InsufficientStock(Exception):
    def __init__(self, sku, available, requested):
        self.sku = sku
        self.available = available
        self.requested = requested
        super().__init__(f"{sku}: {available} available, {requested} requested")


class InvalidInvoiceState(Exception):
    """Raised when an operation is not allowed for the invoice's current state."""


class InvalidReturn(Exception):
    """Raised when a return request is invalid."""


class InvalidAmount(Exception):
    """Raised when a payment amount is not positive."""


class Overpayment(Exception):
    """Raised when a payment exceeds the invoice balance."""
