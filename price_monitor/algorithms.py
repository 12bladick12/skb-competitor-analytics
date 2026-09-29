"""In-app algorithm registry and an offline, zoomable SVG flowchart."""
from html import escape
from pathlib import Path

import streamlit as st

from .matching import VERSION, load_catalog

ALGORITHMS={'inductive':{'label':'Индуктивные датчики','version':VERSION,'document':'INDUCTIVE_MATCHING_RULES.md'}}

# Each decision has explicit outcomes; special types never fall through to the
# standard algorithm. This registry can gain capacitive/reed/optical algorithms.
NODES=[
    ('input',310,40,430,68,'start',['Модель конкурента','Характеристики и назначение']),
    ('normalize',310,145,430,80,'process',['Нормализовать поля и единицы','Сохранить пропуски и противоречия']),
    ('family',330,265,390,96,'decision',['Индуктивный датчик?']),
    ('other',810,275,280,76,'muted',['Другой тип / тип неизвестен','Отдельный алгоритм / проверка']),
    ('special',330,405,390,96,'decision',['Стандартное исполнение?']),
    ('specialReview',810,403,280,100,'muted',['NAMUR, Ex, давление, скорость,','щелевые, аналоговые:','проверить специальные параметры']),
    ('mandatory',310,545,430,108,'process',['Проверить обязательные условия','Корпус, диаметр, шаг, выход, NO/NC,','питание и монтаж']),
    ('compatible',330,697,390,96,'decision',['Обязательные условия','подтверждены?']),
    ('reject',15,707,250,76,'reject',['Есть несовместимость','Кандидат исключён']),
    ('unknown',810,707,280,76,'muted',['Не хватает данных','Отдельно: требует проверки']),
    ('purpose',310,842,430,90,'process',['Проверить важные свойства','Температура по назначению, Sn, материал,','подключение, IP, нагрузка, частота']),
    ('classification',330,977,390,96,'decision',['Важные свойства','соответствуют?']),
    ('close',15,987,250,76,'close',['Есть отличия','Близкий аналог с оговорками']),
    ('missingImportant',810,987,280,76,'muted',['Есть пропуски','Отдельно: требует проверки']),
    ('direct',310,1120,430,75,'direct',['Прямой аналог','Точное соответствие или достаточный запас']),
    ('rank',310,1240,430,106,'process',['Ранжировать внутри каждой группы','Сначала значимые потери, затем близость','длины; точное перед запасом при прочих равных']),
    ('show',310,1390,430,84,'start',['Показать модель, цены и отличия','Неразрешимые компромиссы: сохранить варианты']),
]
EDGES=[('input','normalize',''),('normalize','family',''),('family','other','Нет / ?'),('family','special','Да'),
       ('special','specialReview','Нет'),('special','mandatory','Да'),('mandatory','compatible',''),
       ('compatible','reject','Нет'),('compatible','unknown','Неизвестно'),('compatible','purpose','Да'),
       ('purpose','classification',''),('classification','close','Нет'),('classification','missingImportant','Неизвестно'),
       ('classification','direct','Да'),('direct','rank',''),('close','rank',''),('unknown','rank',''),('missingImportant','rank',''),
       ('specialReview','rank','Проверка'),('rank','show','')]


def flowchart_svg():
    positions={n[0]:n[1:] for n in NODES}
    colors={'start':('#7A1F2B','#FFFFFF'),'process':('#FFFFFF','#263448'),'decision':('#FCF7F8','#7A1F2B'),
            'direct':('#E9F5F1','#176557'),'close':('#FFF5E4','#8B5A13'),'muted':('#F0F3F7','#526175'),'reject':('#FBECEF','#922C3D')}
    pieces=['<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1130 1510" role="img" aria-labelledby="title desc">',
            '<title id="title">Алгоритм подбора индуктивных датчиков</title><desc id="desc">Обязательные условия, проверка важных свойств, три группы результатов и ранжирование.</desc>',
            '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path d="M 0 0 L 10 5 L 0 10 z" fill="#8D98A8"/></marker></defs>',
            '<rect width="1130" height="1510" fill="#FFFFFF"/>']
    for src,dst,label in EDGES:
        x,y,w,h,_,_=positions[src];xx,yy,ww,hh,_,_=positions[dst]
        if abs(y-yy)<50:
            if xx>x:sx,sy,tx,ty=x+w,y+h/2,xx,yy+hh/2
            else:sx,sy,tx,ty=x,y+h/2,xx+ww,yy+hh/2
            path=f'M {sx} {sy} L {tx} {ty}';lx=(sx+tx)/2;ly=sy-13
        elif src in ('close','unknown','missingImportant','specialReview'):
            side=60 if src=='close' else (1110 if src=='specialReview' else 780 if src=='unknown' else 1090)
            sx,sy=x+w/2,y+h;tx,ty=(xx if src=='close' else xx+ww),yy+hh/2
            path=f'M {sx} {sy} L {sx} {sy+20} L {side} {sy+20} L {side} {ty} L {tx} {ty}'
            lx=side-15 if side>740 else side+15;ly=ty-20
        else:
            sx,sy,tx,ty=x+w/2,y+h,xx+ww/2,yy
            path=f'M {sx} {sy} L {tx} {ty}';lx=sx+18;ly=(sy+ty)/2+5
        pieces.append(f'<path d="{path}" fill="none" stroke="#8D98A8" stroke-width="1.8" marker-end="url(#arrow)"/>')
        if label:pieces.append(f'<text x="{lx}" y="{ly}" text-anchor="middle" font-family="Arial,sans-serif" font-size="13" fill="#637387" paint-order="stroke" stroke="white" stroke-width="5">{escape(label)}</text>')
    for _,x,y,w,h,kind,lines in NODES:
        fill,text=colors[kind]
        if kind=='decision':shape=f'<polygon points="{x+w/2},{y} {x+w},{y+h/2} {x+w/2},{y+h} {x},{y+h/2}"'
        else:shape=f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h/2 if kind=="start" else 12}"'
        pieces.append(shape+f' fill="{fill}" stroke="{text if kind=="decision" else "#DAE0E8"}" stroke-width="1.4"/>')
        for i,line in enumerate(lines):
            ty=y+h/2+(i-(len(lines)-1)/2)*22+5
            pieces.append(f'<text x="{x+w/2}" y="{ty}" text-anchor="middle" font-family="Arial,sans-serif" font-size="{15 if len(line)>40 else 16}" font-weight="{600 if i==0 else 400}" fill="{text}">{escape(line)}</text>')
    pieces.append('</svg>');return ''.join(pieces)


def diagram_html(svg):
    return '''<!doctype html><html lang="ru"><head><meta charset="utf-8"><style>
    body{margin:0;font:14px Arial,sans-serif;color:#526175;background:#fff} .toolbar{display:flex;gap:8px;align-items:center;padding:10px 14px;border-bottom:1px solid #e3e8ee;position:sticky;top:0;background:white;z-index:2}button{background:white;border:1px solid #dce2e9;border-radius:7px;padding:8px 12px;color:#7a1f2b;cursor:pointer}button:hover{background:#fcf7f8}.hint{margin-left:auto;font-size:12px}#canvas{height:615px;overflow:auto;background:#fafbfc}#drawing{margin:auto;min-width:550px;}svg{display:block;width:100%;height:auto}</style></head><body>
    <div class="toolbar"><button id="out" aria-label="Уменьшить масштаб">−</button><span id="scale" aria-live="polite">100%</span><button id="in" aria-label="Увеличить масштаб">+</button><button id="fit">По ширине</button><span class="hint">Прокрутка — просмотр схемы</span></div>
    <div id="canvas" tabindex="0" aria-label="Блок-схема подбора с прокруткой"><div id="drawing">'''+svg+'''</div></div>
    <script>let zoom=1; const drawing=document.getElementById('drawing');function render(){drawing.style.width=(zoom*100)+'%';document.getElementById('scale').textContent=Math.round(zoom*100)+'%'}document.getElementById('in').onclick=()=>{zoom=Math.min(2.5,zoom+.25);render()};document.getElementById('out').onclick=()=>{zoom=Math.max(.5,zoom-.25);render()};document.getElementById('fit').onclick=()=>{zoom=1;render()};render();</script></body></html>'''


def render_algorithms():
    algorithm=st.selectbox('Группа продукции',list(ALGORITHMS),format_func=lambda k:ALGORITHMS[k]['label'],key='matching_algorithm_group')
    spec=ALGORITHMS[algorithm]
    st.subheader(spec['label'])
    st.caption('Согласованные правила подбора для сравнения цен. Типы продукции имеют самостоятельные алгоритмы; ёмкостные, герконовые и оптические будут добавлены отдельно.')
    svg=flowchart_svg()
    st.iframe(diagram_html(svg),height=680)
    a,b=st.columns([1,3])
    a.download_button('Скачать блок-схему SVG',svg.encode('utf-8'),file_name='inductive_matching.svg',mime='image/svg+xml',key='matching_flow_download')
    b.caption('Версия '+spec['version']+' · та же логика используется в сравнении цен.')
    st.markdown('**Прямые аналоги** — подтверждены обязательные и важные свойства. **Близкие аналоги** — есть объяснимые отличия. **Требуют проверки** — не хватает характеристик или нужны правила специального исполнения.')
    st.table([
        {'Правило':'Резьба','Как применяется':'Диаметр и шаг отдельно; по одному M18 шаг не угадывается.'},
        {'Правило':'Холодный климат','Как применяется':'После достаточности Tmin выбираем ближайшую длину: В (−60…+70 °C, +5 мм) перед А (−45…+85 °C, +15 мм).'},
        {'Правило':'Расстояние Sn','Как применяется':'Ближайшее абсолютное отклонение в обе стороны. Изменение Sn пока относится к близким вариантам: допуск прямого аналога не согласован.'},
        {'Правило':'Материал и подключение','Как применяется':'Изменения — близкие варианты; при прочих равных эти две уступки равноценны.'},
        {'Правило':'Лучшие характеристики','Как применяется':'Подтверждённый достаточный запас считается соответствием; при прочих равных точное значение предпочтительнее.'},
        {'Правило':'Несколько уступок','Как применяется':'Цена и сумма произвольных весов не решают выбор. Неразрешимые компромиссы сохраняются с одинаковым приоритетом.'},
        {'Правило':'Специальные исполнения','Как применяется':'NAMUR, Ex, высокое давление, контроль скорости, щелевые и аналоговые проверяются отдельно. Их признаки могут сочетаться.'},
    ])
    with st.expander('Полные правила и открытые вопросы'):
        path=Path(__file__).parents[1]/'docs/prices'/spec['document']
        st.markdown(path.read_text(encoding='utf-8'))
    with st.expander('Номенклатура и версия справочника'):
        catalog,_=load_catalog()
        st.write(f'Активных индуктивных датчиков: {len(catalog["products"])}. Снимок: {catalog["snapshot_date"]}. Последнее изменение исходного Google-файла: {catalog["source_modified_at"]}.')
        st.link_button('Открыть базу номенклатуры',catalog['source_url'])
        st.caption('Исторические подборы использованы для проверки правил. Они не считаются автоматически подтверждёнными прямыми аналогами.')
