"""Shared error types for module imports AND direct script entrypoints."""
class GateFailure(RuntimeError):
    pass

class InfrastructureUnavailable(GateFailure):
    pass

class ProviderEvidenceError(InfrastructureUnavailable):
    """Malformed evidence prevents verification; does not prove app failure."""

class AuthenticationRequired(GateFailure):
    pass

class PendingEvidence(GateFailure):
    """Expected bounded readiness wait; never a PASS."""
