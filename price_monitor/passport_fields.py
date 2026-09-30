"""Validation of geometry entered and confirmed by an employee."""
import math

FIELDS = {
    'body_shape': 'Форма корпуса', 'tip_shape': 'Форма чувствительного элемента',
    'connection_shape': 'Присоединение', 'connector_shape': 'Электрический разъём',
    'installation': 'Исполнение: стандартное / погружное',
    'thread': 'Присоединительная резьба', 'active_length_mm': 'Длина чувствительного элемента, мм',
    'immersion_length_mm': 'Длина выступающей части, мм', 'body_length_mm': 'Длина корпуса, мм',
    'tip_diameter_mm': 'Диаметр наконечника, мм',
}

def validate(result, pages):
    if not isinstance(result,dict) or set(result)!={'fields','notes'} or not isinstance(result['notes'],str):
        raise ValueError('Неверная структура характеристик')
    if not isinstance(result['fields'],list) or len(result['fields'])>200: raise ValueError('Неверный список размеров')
    clean=[]
    required={'name','value','unit','page','bbox','evidence','model','datum'}
    for field in result['fields']:
        if not isinstance(field,dict) or set(field)!=required or field['name'] not in FIELDS: raise ValueError('Неизвестное поле')
        if type(field['page']) is not int or not 1<=field['page']<=pages: raise ValueError('Неверная страница')
        box=field['bbox']
        if not isinstance(box,list) or len(box)!=4 or any(type(v) not in (int,float) or not math.isfinite(v) or not 0<=v<=1 for v in box): raise ValueError('Неверная область чертежа')
        if box[0]>=box[2] or box[1]>=box[3]: raise ValueError('Пустая область чертежа')
        if any(not isinstance(field[x],str) or len(field[x])>2000 for x in ('unit','evidence','model','datum')): raise ValueError('Неверное основание')
        if field['value'] is None: continue
        if field['name'].endswith('_mm'):
            if type(field['value']) not in (int,float) or not math.isfinite(field['value']) or not 0<field['value']<=100000 or field['unit']!='mm' or not field['datum'].strip():
                raise ValueError('Размер не имеет единицы или базы измерения')
        elif not isinstance(field['value'],str) or len(field['value'])>300 or field['unit']!='': raise ValueError('Неверное описание формы')
        if field['name']=='installation' and field['value'] not in ('standard','immersible'): raise ValueError('Неизвестное исполнение')
        if not field['evidence'].strip(): raise ValueError('Нет основания значения')
        clean.append(dict(field))
    return {'fields':clean,'notes':result['notes'][:4000]}



