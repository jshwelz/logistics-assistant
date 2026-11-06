class ApiError(Exception):
    def __init__(self, status_code: int, error_code: str):
        self.status_code = status_code
        self.error_code = error_code
        super().__init__(error_code)

    def body(self) -> dict[str, str]:
        return {"error_code": self.error_code}


class FileRejected(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class IngestionError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


class PermanentIngestionError(IngestionError):
    pass


class TransientIngestionError(IngestionError):
    pass


class LostLease(Exception):
    pass


class StorageFailed(Exception):
    pass


class ProviderFailed(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


FILE_REJECT_STATUS = {
    "empty_upload": 400,
    "zip_unsafe": 400,
    "invalid_container": 400,
    "payload_too_large": 413,
    "unsupported_file": 415,
    "encrypted": 400,
}
