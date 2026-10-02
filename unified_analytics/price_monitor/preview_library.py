"""Local preview adapter; explicitly a snapshot of latest prices, not full history."""
from .library import Library

class PreviewLibrary(Library):
    def __init__(self,repository,snapshot):
        super().__init__(repository)
        self.snapshot=snapshot
        self.data_version=snapshot.get('snapshot_at','')

    def comparison_products(self):
        return self.export_products()

    def iter_comparison_products(self):
        yield from self.export_products()

    def iter_search_products(self):
        yield from self.export_products()

    def iter_search_identities(self):
        yield from self.export_products()

    def comparison_prices(self,ids):
        fields=('rule_id','status','checked_at','price_text','last_price','last_currency',
                'price_checked_at','last_price_text','_price_snapshot_terms')
        return {r['rule_id']:{k:r.get(k) for k in fields} for r in self.export_products() if r['rule_id'] in ids}

    def export_products(self,query='',source='',brand='',selected=False):
        rows=[dict(r) for r in self.snapshot['products']]
        from .price_terms import parse_terms
        for row in rows:
            if row.get('price_checked_at')!=row.get('checked_at'):
                row['_price_snapshot_terms']=parse_terms(row.get('last_price_text'))
        if query:rows=[r for r in rows if query.casefold() in (r['article']+' '+r['manufacturer']+' '+r.get('title','')).casefold()]
        if source:rows=[r for r in rows if r['source']==source]
        if brand:rows=[r for r in rows if r['manufacturer']==brand]
        if selected:rows=[r for r in rows if r.get('selected')]
        return rows

    def history(self,rule_ids,start=None,end=None):
        from .automatic_price_terms import current_terms
        rows=[]
        for original in self.export_products():
            if original['rule_id'] not in rule_ids or not original.get('last_price'):continue
            stamp=original.get('price_checked_at')
            if not stamp or (start and stamp<start) or (end and stamp>=end):continue
            rows.append({**original,'price':original['last_price'],'currency':original.get('last_currency'),
                         '_specifications':{**original.get('_specifications',{}),'price_terms':current_terms(original)},
                         'status':'priced','checked_at':stamp,'id':original['rule_id'],'run_id':None})
        return rows

    def with_specifications(self,rows):return rows
