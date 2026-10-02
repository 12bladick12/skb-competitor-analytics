"""Open an existing cloud database without running migrations in a page request."""
import atexit
import logging
import threading

from .storage import Store


class DatabaseSchemaError(RuntimeError):
    pass


def verify_schema(repository):
    rows=repository.batch("""SELECT version,
        to_regclass('price_monitor.own_price_imports') IS NOT NULL
        AND to_regclass('price_monitor.own_product_prices') IS NOT NULL
        AND to_regclass('price_monitor.automatic_price_terms') IS NOT NULL
        AND to_regclass('price_monitor.product_enrichment') IS NOT NULL AS ready,
        (SELECT count(*) FROM pg_trigger WHERE tgrelid=to_regclass('price_monitor.passport_jobs')
            AND tgname IN ('retired_passport_insert','retired_passport_claim') AND NOT tgisinternal) AS fences
        FROM schema_version WHERE id=1""")
    if not rows or rows[0]['version']!=1 or not rows[0]['ready'] or rows[0]['fences']!=2:
        raise DatabaseSchemaError('Existing price schema needs migration')


class CatalogMaintenance:
    """Bound metadata backfill to one batch; a retry never blocks a visitor."""
    def __init__(self,repository):
        self.repository=repository
        self.stopping=threading.Event()
        self.thread=threading.Thread(target=self._loop,name='price-catalog-maintenance',daemon=True)
        self.thread.start()

    def _loop(self):
        from .scope import refresh_scope
        while not self.stopping.is_set():
            try:
                changed=refresh_scope(self.repository,max_batches=1)
                delay=15 if changed else 300
            except Exception as exc:
                logging.getLogger(__name__).warning('Catalog metadata will retry (%s)',type(exc).__name__)
                delay=60
            self.stopping.wait(delay)

    def close(self):
        self.stopping.set()
        self.thread.join(timeout=2)


def open_cloud_services(settings):
    from .cloud import EmbeddedWorker,retire_legacy_workers
    from .enrichment import EnrichmentWorker
    retire_legacy_workers()
    store=Store(postgres=settings,initialize=False)
    started=[]
    try:
        verify_schema(store.catalog)
        worker=EmbeddedWorker(store);started.append(worker)
        store.enrichment_worker=EnrichmentWorker(store.catalog);started.append(store.enrichment_worker)
        store.catalog_maintenance=CatalogMaintenance(store.catalog);started.append(store.catalog_maintenance)
    except Exception:
        for service in reversed(started):service.close()
        store.pg.close()
        raise
    atexit.register(store.pg.close)
    for service in started:atexit.register(service.close)
    return store,worker
