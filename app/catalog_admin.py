"""Independent admin application assembly over the shared catalog domain."""

from fastapi import FastAPI

from app.catalog.repository import CatalogRepository
from app.catalog_routes import register_catalog_routes


def create_catalog_admin_app(*, engine, actor_provider, schema="monocorpus", preview_url_provider=None):
    """The host supplies authentication and private-asset delivery.

    The caller owns the process-shared engine lifecycle. This app never runs
    migrations, loads local runtime state, starts workflows, or reads secrets.
    """
    app = FastAPI(title="Manzara catalog administration")
    repository = CatalogRepository(engine, schema=schema)
    app.state.catalog = repository
    register_catalog_routes(app, repository_provider=lambda: repository,
                            actor_provider=actor_provider, preview_url_provider=preview_url_provider)
    return app
