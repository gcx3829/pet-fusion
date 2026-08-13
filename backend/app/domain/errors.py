class DomainError(Exception):
    code = "DOMAIN_ERROR"
    status_code = 422

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFoundError(DomainError):
    code = "NOT_FOUND"
    status_code = 404


class ConflictError(DomainError):
    code = "CONFLICT"
    status_code = 409


class UploadValidationError(DomainError):
    code = "INVALID_IMAGE_UPLOAD"
    status_code = 422


class SourceManifestMismatchError(DomainError):
    code = "SOURCE_MANIFEST_MISMATCH"
    status_code = 409


class ConfigurationError(DomainError):
    code = "INVALID_CONFIGURATION"
    status_code = 500


class UnsupportedMilestoneActionError(DomainError):
    code = "ACTION_NOT_AVAILABLE"
    status_code = 422
