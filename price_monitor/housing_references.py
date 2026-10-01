"""Reviewed housing tables, scoped to complete manufacturer model codes.

Geometry is a separate source from the electrical ordering key. A diameter alone
is never enough to look up a pitch. See docs/prices/THREAD_GEOMETRY_REVIEW.md.
"""
import re

LANBAO_CATALOG = 'https://flextronicbg.com/wp-content/uploads/2018/10/Inductive-Sensors.pdf'


def reference(source, section, **values):
    return dict(source=source, section=section, values=values)


def lanbao_housing(code):
    # Overview printed pp. 15, 19 (PDF 9, 13) specifies these B housings.
    # No inheritance to G/S/V materials, A/C/D lengths or special functions.
    m = re.fullmatch(r'LR(?P<size>12|18|30)XB(?P<mount>F|N)(?P<sn>\d{2})(?P<supply>D|A)(?P<out>N|P|L|T)(?P<function>O|C|R)(?P<extended>Y?)(?P<conn>-E2)?', code)
    if not m:
        return None
    size = int(m['size'])
    transistor = m['supply']=='D' and m['out'] in ('N','P')
    two_wire = (m['supply'],m['out']) in (('D','L'),('A','T')) and m['function'] in ('O','C')
    if not (transistor or two_wire):return None
    if m['extended'] and size == 30 and m['function']=='R':return None
    distances = ({12:(4,8),18:(8,12),30:(15,22)} if m['extended'] else
                 {12:(2,4),18:(5,8),30:(10,15)})
    nonflush = m['mount'] == 'N'
    if int(m['sn']) != distances[size][nonflush]:
        return None
    page = 19 if m['extended'] else 15
    result = reference(LANBAO_CATALOG+'#page='+str(page-6),
        f'LANBAO Inductive Sensors 2016, печатные стр. {page}–{page+1} (PDF {page-6}–{page-5}), строка LR{size}X '+('DC 3/4 Wires' if transistor else 'DC 2 Wires' if m['supply']=='D' else 'AC 2 Wires'),
        body_type='threaded', diameter=float(size), pitch=1.5 if size == 30 else 1.)
    # The overview length is for the flush housing. Use exact dimensional tables
    # for overall length, including the protruding non-flush sensing face.
    if not m['extended'] and transistor and size == 12:
        result['values']['length'] = (63. if m['conn'] else 51.) + (4. if nonflush else 0.)
        result['field_sources'] = {'length': reference(
            'https://www.cnlanbaosensor.com/uploads/LR12X-DC-34'+('-E2' if m['conn'] else '')+'.pdf',
            'LR12X DC 3/4 wires, Ver. A 04/T, стр. 1: Dimensions / Flush, Non-flush')}
    elif not m['extended'] and transistor and m['conn']:
        printed_page = 33 if size == 18 else 34
        projection = {18:8,30:12}[size] if nonflush else 0
        result['values']['length'] = 63. + projection
        result['field_sources'] = {'length': reference(LANBAO_CATALOG+f'#page={printed_page-6}',
            f'LANBAO 2016, стр. {printed_page} (PDF {printed_page-6}), чертёж -E2: 63 мм'+
            (f' + выступ {projection} мм' if nonflush else ''))}
    return result


def autonics_housing(code):
    # PR AC and DC 2-wire have their own manuals; electrical values from DC
    # 3-wire must not be inherited by these geometrically compatible families.
    ac = re.fullmatch(r'PR(?P<spatter>A?)(?P<conn>CM|W)?(?P<long>L?)(?P<size>12|18|30)-(?P<sn>2|4|5|8|10|15)A[OC]', code)
    dc = re.fullmatch(r'PR(?P<spatter>A?)(?P<conn>CM|W)?T(?P<size>08|12|18|30)-(?P<sn>1\.5|2|4|5|8|10|15)[DX][OC](?:-(?:I|V|IV))?', code)
    m = ac or dc
    if not m:
        return None
    size = int(m['size'])
    flush, nonflush = {8:(1.5,2.),12:(2.,4.),18:(5.,8.),30:(10.,15.)}[size]
    if float(m['sn']) not in (flush, nonflush):
        return None
    if m['spatter'] and (size == 8 or float(m['sn']) != flush):
        return None
    if ac and (size == 12 and ac['long'] or ac['spatter'] and (ac['conn'] or ac['long'])):
        return None
    if dc and 'X' in code and (size == 8 or dc['conn']=='CM'):
        return None
    return reference('https://www.autonics.com/in/data/manual/en/PR_'+('AC' if ac else 'DC')+'_2-wire',
        'PR '+('AC' if ac else 'DC')+' 2-wire: Ordering Information / Specifications / Dimensions, TAP (стр. 2–3)',
        body_type='threaded', diameter=float(size), pitch=1.5 if size == 30 else 1.)


# Model whitelists from the dimension tables, not generated neighboring codes.
PF_4 = ('NBB0,6-4GM22-E0','NBB0,6-4GM22-E1','NBB0,6-4GM22-E2','NBB0,6-4GM22-E3',
        'NBB1-4GM22-E0','NBB1-4GM22-E2','NBB0,6-4GM22-E0-0,3M-V3',
        'NBB0,6-4GM22-E2-0,3M-V3','NBB1-4GM22-E0-0,3M-V3','NBB1-4GM22-E2-0,3M-V3')
PF_5 = ('NBB0,8-5GM25-E0','NBB0,8-5GM25-E1','NBB0,8-5GM25-E2','NBB0,8-5GM25-E3',
        'NBB1,5-5GM25-E2','NBB0,8-5GM25-E0-0,3M-V3','NBB0,8-5GM25-E2-0,3M-V3',
        'NBB0,8-5GM25-E0-V3','NBB0,8-5GM25-E1-V3','NBB0,8-5GM25-E2-V3',
        'NBB1,5-5GM25-E2-V3','NBB1,5-5GM25-E3-V3')
PF_TABLES = (
    (82,4.,.5,PF_4), (83,5.,.5,PF_5),
    (90,8.,1.,('NCB1,5-8GM40-Z0','NCB1,5-8GM40-Z1','NCB1,5-8GM50-Z0-V3','NCB1,5-8GM50-Z1-V3')),
    (110,12.,1.,('NBB2-12GM50-E0','NBB2-12GM50-E1','NBB2-12GM50-E2','NBB2-12GM60-A0',
                'NBB2-12GM60-A2','NBB2-12GM50-E0-V1','NBB2-12GM50-E1-V1','NBB2-12GM50-E2-V1',
                'NBB2-12GM60-E2-V1','NBB2-12GM60-A0-V1','NBB2-12GM60-A2-V1')),
    (130,18.,1.,('NBB5-18GK50-E0','NBB5-18GK50-E2','NBN8-18GK50-A2','NBN8-18GK50-E0','NBN8-18GK50-E2')),
    (145,30.,1.5,('NCB15-30GM50-Z4','NCB15-30GM50-Z5','NCB15-30GM50-Z4-V1','NCB15-30GM50-Z5-V1')),
)


def housing_reference(brand, code):
    if brand == 'LANBAO':
        return lanbao_housing(code)
    if brand == 'Autonics':
        return autonics_housing(code)
    if brand == 'Pepperl+Fuchs':
        canonical = code.replace('.', ',')
        for page,diameter,pitch,models in PF_TABLES:
            if canonical in models:
                return reference(f'https://files.pepperl-fuchs.com/online-catalogs/911360/files/assets/basic-html/page{page}.html',
                    f'Sensors and Systems, печатная стр. {page-2}: Model Number / Dimensions, Diameter D (barrel thread)',
                    body_type='threaded', diameter=diameter, pitch=pitch)
    return None
