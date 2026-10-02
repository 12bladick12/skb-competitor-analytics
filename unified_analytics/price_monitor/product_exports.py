"""Human-readable product specifications, with one row per property in XLSX."""
from .product_labels import display_article
from .price_terms import terms_for
from .automatic_price_terms import current_terms


def export_tables(rows):
    products,properties=[],[]
    for original in rows:
        row={k:v for k,v in original.items() if not k.startswith('_') and k not in ('details_json','characteristics_json','documents_json','last_price_details_json','last_price_text')}
        if 'article' in row:row['article']=display_article(original)
        terms=current_terms(original) if 'last_price' in original and 'details_json' not in original else terms_for(original)
        row['НДС в цене']={'gross':'С НДС','net':'Без НДС'}.get(terms.get('basis'),'Не указан')
        detail=original.get('_specifications',{})
        raw=detail.get('attributes',[])
        automatic=original.get('_enrichment',{}).get('attributes',[])
        attrs=raw+automatic
        row['Исходных характеристик']=len(raw)
        row['Автоматически дозаполнено']=len(automatic)
        row['Характеристик']=len(attrs)
        row['Статус характеристик']=('Получены' if attrs else 'Нет в сохранённом ответе')
        row['Описание']=detail.get('description','')
        for prop in attrs:
            name=str(prop.get('name','')).strip();value=str(prop.get('value',''))
            group=str(prop.get('group','')).strip()
            if not name:continue
            key='Характеристика: '+(group+' / ' if group else '')+name
            row[key]=(str(row[key])+'\n'+value) if key in row and row[key]!=value else value
            properties.append({'Источник':original.get('source',''),'Производитель':original.get('manufacturer',''),
                'Артикул':display_article(original),'ID модели':original.get('rule_id',''),
                'Запуск':original.get('run_id',''),'Проверено (UTC)':original.get('checked_at',''),
                'Ссылка':original.get('url') or original.get('product_url',''),
                'Группа':group,'Характеристика':name,'Значение':value,
                'Источник дозаполнения':prop.get('source_url',''),'Основание':prop.get('evidence',''),
                'Версия правила':prop.get('rule_version','')})
        products.append(row)
    return products,properties
