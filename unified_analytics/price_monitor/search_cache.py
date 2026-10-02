"""Publish names first; refresh characteristics without discarding a usable catalog."""
from dataclasses import dataclass
import logging
import threading
import time

from .comparison_groups import ComparisonIndex,own_record,OUR_BRAND
from .matching_normalize import Sensor,normalize_sensor


class IdentityIndex:
    def __init__(self,rows,own_sensors,own_prices,unmatched):
        self.records={}
        for row in rows:
            entry='competitor:'+str(row['rule_id'])
            self.records[entry]={**row,'entry_id':entry,'brand':row['manufacturer'],
                'model':row['article'],'is_ours':False,
                'sensor':Sensor(str(row['rule_id']),row['article'],article=row['article'])}
        for sensor in own_sensors:
            record=own_record(sensor,own_prices.get(sensor.id));self.records[record['entry_id']]=record
        for price in unmatched:
            sensor=normalize_sensor({'code':'price:'+price['article'],'model':price['source_model'],
                                     'article':price['article'],'manufacturer':OUR_BRAND})
            record=own_record(sensor,price);self.records[record['entry_id']]=record


@dataclass(frozen=True)
class CatalogState:
    index: object
    complete: bool
    loading: bool
    generation: int
    loaded: int
    updated_at: float
    error: str


class SearchCatalogCache:
    """One background reader per cached service; failures retain the last good value."""
    def __init__(self,library,*,catalog_loader=None,refresh_seconds=900,clock=time.monotonic):
        self.library=library
        if catalog_loader is None:
            from .matching import load_catalog
            catalog_loader=load_catalog
        self.catalog_loader=catalog_loader;self.refresh_seconds=refresh_seconds;self.clock=clock
        self.guard=threading.Lock();self.stopping=threading.Event()
        self.index=None;self.complete=False;self.loading=False;self.generation=0
        self.loaded=0;self.updated_at=0;self.error='';self.next_refresh=0;self.failures=0
        self.thread=None

    def state(self):
        with self.guard:
            return CatalogState(self.index,self.complete,self.loading,self.generation,
                                self.loaded,self.updated_at,self.error)

    def request_refresh(self,force=False):
        with self.guard:
            if self.stopping.is_set() or self.loading or (not force and self.clock()<self.next_refresh):return
            self.loading=True;self.loaded=0;self.error=''
            self.thread=threading.Thread(target=self._build,name='price-search-catalog',daemon=True)
            self.thread.start()

    def _build(self):
        try:
            from .own_prices import OwnPrices
            _,matcher=self.catalog_loader()
            prices=OwnPrices(self.library.repo).current()
            own={row['catalog_id']:row for row in prices if row['catalog_id']}
            unmatched=[row for row in prices if not row['catalog_id']]
            # A refresh keeps the complete old index visible until replacement.
            if self.state().index is None:
                identities=IdentityIndex(self.library.iter_search_identities(),matcher.products,own,unmatched)
                with self.guard:
                    self.index=identities;self.generation+=1
            if self.stopping.is_set():return

            def rows():
                for count,row in enumerate(self.library.iter_search_products(),1):
                    if self.stopping.is_set():raise InterruptedError()
                    if count%100==0:
                        with self.guard:self.loaded=count
                    yield row
            full=ComparisonIndex(rows(),matcher.products,own,unmatched,
                                 reference_loader=self.library.comparison_reference)
            with self.guard:
                self.index=full;self.complete=True;self.generation+=1;self.error=''
                self.updated_at=time.time();self.failures=0
                self.next_refresh=self.clock()+self.refresh_seconds
        except Exception as exc:
            with self.guard:
                self.failures+=1;self.error=type(exc).__name__
                self.next_refresh=self.clock()+min(300,30*2**min(self.failures-1,4))
            logging.getLogger(__name__).warning('Search refresh will retry (%s)',type(exc).__name__)
        finally:
            with self.guard:self.loading=False

    def close(self):
        self.stopping.set()
        if self.thread:self.thread.join(timeout=1)
