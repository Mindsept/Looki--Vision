class DomainError(Exception):
    def __init__(self, status: int, code: str, message: str, *, request_id=None):
        self.status = status
        self.code = code
        self.message = message
        self.request_id = str(request_id) if request_id else None
        super().__init__(message)
