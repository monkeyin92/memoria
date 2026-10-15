"""Install Scheme A child-memory stores on the Control API app."""

from __future__ import annotations

from services.control_api.app.wiring import Wiring
from services.guardian.subject_activity import SubjectActivityStore
from services.memory_scope.child_layer_retention import ChildLayerRetentionWorker
from services.memory_scope.child_layers import ChildLayerStore


async def install_child_compliance(w: Wiring) -> None:
    app, settings = w.app, w.settings
    app.state.child_layer_store = None
    app.state.subject_activity_store = None
    app.state.child_layer_retention_worker = None
    if not w.live:
        return
    api_dsn = settings.memory_api_database_url.get_secret_value().strip()
    maintenance_dsn = settings.memory_maintenance_database_url.get_secret_value().strip()
    if api_dsn and maintenance_dsn:
        layers = ChildLayerStore(api_dsn=api_dsn, maintenance_dsn=maintenance_dsn)
        await layers.initialize()
        app.state.child_layer_store = layers
        w.on_close(layers.close)
    guardian_dsn = settings.guardian_database_url.get_secret_value().strip()
    if guardian_dsn:
        activity = SubjectActivityStore(guardian_dsn)
        await activity.initialize()
        app.state.subject_activity_store = activity
        w.on_close(activity.close)
    if app.state.child_layer_store is not None:
        worker = ChildLayerRetentionWorker(
            app.state.child_layer_store, app.state.subject_activity_store
        )
        w.start(worker)
        app.state.child_layer_retention_worker = worker
