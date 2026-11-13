"""Failure contract of the production Session Runtime.

Every layer of the runtime raises this vocabulary: the orchestrating service,
the wire decoders that parse authority payloads, and the PostgreSQL authority
adapters.  The Control routes catch these types from
``services.session_runtime.service`` because that is the service module they
call, and the names stay importable there; the definitions live here so the
two lower layers do not have to import their caller.
"""


class PersistentSessionUnavailable(RuntimeError):
    """A required production authority is absent or unavailable."""


class PersistentSessionDenied(PermissionError):
    """The authenticated actor/binding cannot create this Session."""


class PersistentSessionTerminated(PersistentSessionDenied):
    """The authority explicitly revoked the session's binding or lifecycle."""


class PersistentSessionExpired(PersistentSessionDenied):
    """The stored profile is past its use window; only renewal may reissue it.

    It stays a ``PersistentSessionDenied`` so every existing caller keeps
    failing closed; callers that own renewal catch this narrower type.
    """


class PersistentSessionNotFound(LookupError):
    """No RLS-visible active Session/profile exists."""
