"""Human-readable product specifications, with one row per property in XLSX."""
def export_tables(rows):
    products,properties=[],[]
    for original in rows:
        row={k:v for k,v in original.items() if k not in ('details_json','characteristics_json','documents_json','_specifications')}
        detail=original.get('_specifications',{})
        attrs=detail.get('attributes',[])
        row['Характеристик']=len(attrs)
        row['Статус характеристик']=('Получены' if attrs else 'Нет в сохранённом ответе')
        row['Описание']=detail.get('description','')
        row['Документы']='\n'.join(f"{d.get('name','')}: {d.get('url','')}" for d in detail.get('documents',[]))
        for prop in attrs:
            name=str(prop.get('name','')).strip();value=str(prop.get('value',''))
            group=str(prop.get('group','')).strip()
            if not name:continue
            key='Характеристика: '+(group+' / ' if group else '')+name
            row[key]=(str(row[key])+'\n'+value) if key in row and row[key]!=value else value
            properties.append({'Источник':original.get('source',''),'Производитель':original.get('manufacturer',''),
                'Артикул':original.get('article',''),'ID модели':original.get('rule_id',''),
                'Запуск':original.get('run_id',''),'Проверено (UTC)':original.get('checked_at',''),
                'Ссылка':original.get('url') or original.get('product_url',''),
                'Группа':group,'Характеристика':name,'Значение':value})
        products.append(row)
    return products,properties
