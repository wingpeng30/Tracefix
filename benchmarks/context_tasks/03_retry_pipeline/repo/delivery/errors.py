"""投递层稳定异常。"""


class DeliveryError(Exception):
    pass


class NetworkError(DeliveryError):
    pass


class InvalidPayload(DeliveryError):
    pass


class HttpError(DeliveryError):
    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f"HTTP {status}")
