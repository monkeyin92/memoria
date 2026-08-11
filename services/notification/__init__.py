"""Multi-role notification state machine (PR-15).

Domain: ``services.notification.domain``
Store seam: ``services.notification.repository``
Facade: ``services.notification.service.NotificationService``
Adapters: in-memory, SQLite (dev), PostgreSQL / FORCE RLS (production).

Real delivery-channel adapters (``DeliveryChannelPort``) are implemented
outside this package; this package stores no credentials and only records a
delivery when the adapter returns a ``channel_receipt_id``.
"""

from services.notification.domain import (
    BackoffPolicy,
    CancelReason,
    Channel,
    ContentPolicyError,
    DeliveryAttempt,
    DeliveryReceipt,
    IntentKind,
    NotificationConflictError,
    NotificationError,
    NotificationFencingError,
    NotificationIntent,
    NotificationNotFoundError,
    NotificationPolicyError,
    NotificationStateError,
    RecipientBinding,
    RecipientRole,
    RecipientSpec,
    RelationshipInactiveError,
    render_notification_content,
)
from services.notification.repository import (
    ChannelResult,
    DeliveryChannelPort,
    NotificationAuditEvent,
    NotificationOutboxEvent,
)
from services.notification.service import DispatchSummary, NotificationService

__all__ = [
    "BackoffPolicy",
    "CancelReason",
    "Channel",
    "ChannelResult",
    "ContentPolicyError",
    "DeliveryAttempt",
    "DeliveryChannelPort",
    "DeliveryReceipt",
    "DispatchSummary",
    "IntentKind",
    "NotificationAuditEvent",
    "NotificationConflictError",
    "NotificationError",
    "NotificationFencingError",
    "NotificationIntent",
    "NotificationNotFoundError",
    "NotificationOutboxEvent",
    "NotificationPolicyError",
    "NotificationService",
    "NotificationStateError",
    "RecipientBinding",
    "RecipientRole",
    "RecipientSpec",
    "RelationshipInactiveError",
    "render_notification_content",
]
