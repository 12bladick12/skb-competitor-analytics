"""Conservative manufacturer-specific decoding of published ordering codes.

Only fully recognized standard families enter these rules. Unknown suffixes do
not inherit series defaults. Nominal housing lengths never become measured ones.
"""
import re

AUTONICS_SOURCE = 'https://www.autonics.com/in/data/manual/en/PR_DC_3-wire'
BESKONTA_SOURCE = 'https://beskonta.ru/informaciya/rasshifrovka-markirovki/'
LANBAO_SOURCE = 'https://www.cnlanbaosensor.com/uploads/Product%C2%A0summary-2024.pdf'


def autonics(code):
    m = re.fullmatch(r'PR(?P<spatter>A?)(?P<conn>CM|W)?(?P<body>S|L)?(?P<size>08|12|18|30)-(?P<sn>1\.5|2|4|5|8|10|15)D(?P<out>N2|P2|N|P)(?:-(?P<cable>V))?', code)
    if not m: return None
    size = int(m['size']); sn = float(m['sn'])
    flush = {8:1.5, 12:2., 18:5., 30:10.}
    nonflush = {8:2., 12:4., 18:8., 30:15.}
    if sn not in (flush[size], nonflush[size]): return None
    if m['spatter'] and (size == 8 or sn != flush[size] or m['conn'] == 'W'): return None
    if m['body'] == 'S' and (size != 12 or m['conn']): return None
    if m['body'] == 'L' and ((m['conn']=='CM' and size in (8,12)) or (m['conn']=='W' and size==12)): return None
    is_flush = sn == flush[size]
    values = dict(body_type='threaded', diameter=float(size), pitch=1.5 if size==30 else 1., sn=sn,
                  mount='flush' if is_flush else 'non-flush', output='PNP' if m['out'].startswith('P') else 'NPN',
                  function='NC' if m['out'].endswith('2') else 'NO', voltage_type='DC', vmin=10., vmax=30.,
                  load=200., tmin=-25., tmax=70., ip=('67',), wire_count=3.,
                  material='stainless' if size==8 and m['conn']=='CM' else 'brass',
                  connection={'CM':'connector','W':'cable+connector',None:'cable'}[m['conn']],
                  frequency=({8:1500.,12:1500.,18:500.,30:400.} if is_flush else {8:1000.,12:500.,18:350.,30:200.})[size])
    if m['conn']: values.update(connector='m12',pin_count=4.)
    return dict(values=values, source=AUTONICS_SOURCE, section='PR DC 3-wire: Ordering Information, Specifications, TAP',
                notes=['Код длины Normal/Short/Long не заменяет точный размер корпуса.'])


def beskonta(code):
    m = re.fullmatch(r'SIS-(?P<size>12|18|30)(?P<mount>V|N)(?P<length>33|55|75)(?:-(?P<conn>P12|P01|F|X))?-(?P<function>NONC|NO|NC)-(?P<out>PNP|NPN|ACR|DCR|AC|DC)-(?P<adjust>R?)(?P<sn>\d+(?:[.,]\d+)?)-(?P<material>ST|TF)(?P<mods>(?:-(?:IP|C1|H1))*)', code)
    if not m: return None
    mods = m['mods'].split('-')[1:]
    if len(mods)!=len(set(mods)) or ('C1' in mods and 'H1' in mods): return None
    output = m['out']; ac = output.startswith('AC')
    values = dict(body_type='threaded', diameter=float(m['size']), mount='flush' if m['mount']=='V' else 'non-flush',
                  function={'NO':'NO','NC':'NC','NONC':'NO/NC'}[m['function']],
                  output=output if output in ('PNP','NPN') else 'relay' if output.endswith('R') else '2-wire',
                  voltage_type='AC' if ac else 'DC',vmin=20. if ac else 10.,vmax=250. if ac else 30.,
                  sn=float(m['sn'].replace(',','.')),material='stainless' if m['material']=='ST' else 'plastic',
                  connection='connector' if m['conn'] in ('P12','P01') else 'cable')
    # The official general legend says M30x1, the catalog says M30x1.5.
    # Do not resolve that contradiction by guessing; leave the card authoritative.
    if m['size'] in ('12','18'): values['pitch']=1.
    if m['conn']=='P12': values.update(connector='m12',pin_count=4.)
    if m['conn']=='P01': values['pin_count']=4.
    if 'IP' in mods: values['ip']=('68',)
    # 'up to IP67' is not proof of a concrete protection degree.
    values.update(tmin=-45. if 'C1' in mods else -15. if 'H1' in mods else -25.,
                  tmax=65. if 'C1' in mods else 105. if 'H1' in mods else 75.)
    notes=['Базовая длина из обозначения сохранена отдельно от измеренной длины корпуса; «до IP67» не подставляется как IP67.']
    if m['size']=='30': notes.append('Шаг M30 противоречиво указан в общей легенде и каталоге; нужен точный размер из карточки.')
    return dict(values=values,source=BESKONTA_SOURCE,section='Индуктивные датчики SIS',notes=notes,
                extras={'Базовая длина в обозначении':m['length'],'Регулируемое Sn':bool(m['adjust'])})


def lanbao(code):
    # Restrict shape X to threaded LR; square sizes are not diameters.
    m = re.fullmatch(r'LR(?P<size>08|12|18|30)X(?P<material>G|S|V)?(?P<body>A|B|C|D)(?P<mount>F|N)(?P<sn>\d{2})(?P<supply>A|B|D|H|L|S|E)(?P<out>N|P|B|L|T)(?P<function>O|C|R|B)(?P<feature>W[1-4]?|Y|B|J|U|A|Q|G|Z)?(?:-(?P<conn>E1|E2|E3|E5|F\d+|D))?', code)
    if not m: return None
    # Small/high-pressure series also encode fractional distances (15 = 1.5).
    # That variant needs its exact table; the general integer key is insufficient.
    if m['sn']=='15' and (m['size']=='08' or m['feature']=='B'):return None
    supply = {'A':('AC',20.,250.),'B':('AC',90.,250.),'D':('DC',10.,30.),'H':('DC',20.,30.),
              'L':('DC',15.,30.),'S':('AC/DC',20.,250.),'E':('DC',10.,60.)}[m['supply']]
    values=dict(body_type='threaded',diameter=float(m['size']),sn=float(m['sn']),
                voltage_type=supply[0],vmin=supply[1],vmax=supply[2],
                output={'N':'NPN','P':'PNP','B':'2-wire','L':'2-wire','T':'2-wire'}[m['out']],
                function={'O':'NO','C':'NC','R':'NO/NC','B':'configurable'}[m['function']])
    # Published Y tables constrain distances; do not accept impossible mixtures.
    if m['feature']=='Y' and int(m['sn']) != {'12':{'F':4,'N':8},'18':{'F':8,'N':12},'30':{'F':15,'N':22},'08':{'F':2,'N':4}}[m['size']][m['mount']]:return None
    # Extended-distance F models may be quasi-flush. Exact model tables override.
    uncertain_flush=m['mount']=='F' and (m['feature']=='Y' or float(m['sn'])>{'08':1.5,'12':2.,'18':5.,'30':10.}[m['size']])
    if not uncertain_flush:values['mount']='flush' if m['mount']=='F' else 'non-flush'
    if m['material']: values['material']={'G':'stainless','S':'plastic','V':'aluminium'}[m['material']]
    conn=m['conn']
    values['connection']='cable' if not conn else 'cable+connector' if conn.startswith('F') else 'terminals' if conn=='D' else 'connector'
    if conn in ('E1','E2','E3','E5'):
        values['connector']='m8' if conn in ('E1','E3') else 'm12'
        values['pin_count']=float({'E1':3,'E2':4,'E3':4,'E5':5}[conn])
    feature=m['feature']
    if feature and feature.startswith('W'):
        values['tmin'],values['tmax']={'W':(-25.,120.),'W1':(-40.,70.),'W2':(-25.,100.),'W3':(-40.,85.),'W4':(-25.,180.)}[feature]
    return dict(values=values,source=LANBAO_SOURCE,section='A01-004: Naming rules',
                special=['pressure'] if feature=='B' else ['speed'] if feature=='J' else [],
                review=uncertain_flush or m['function']=='B',
                notes=['Номинальная категория длины не переводится в миллиметры; стандартная температура, IP, ток, частота и шаг без точной таблицы не выводятся.'])


def pepperl_fuchs(code):
    m=re.fullmatch(r'N(?P<series>B|C|E|R)(?P<mount>B|N)(?P<sn>\d+(?:[.,]\d+)?)-(?P<size>\d+(?:[.,]\d+)?)(?P<thread>G?)(?P<material>M|S|H|K)(?P<length>\d+)-(?P<out>E0|E1|E2|E3|A0|A2)(?:-(?P<cable>\d+(?:[.,]\d+)?)M)?(?:-(?P<conn>V1|V3|V13))?',code)
    if not m:return None
    output=m['out']
    values=dict(body_type='threaded' if m['thread'] else 'smooth',diameter=float(m['size'].replace(',','.')),
                sn=float(m['sn'].replace(',','.')),mount='flush' if m['mount']=='B' else 'non-flush',
                output='PNP' if output in ('E2','E3','A2') else 'NPN',
                function='NO/NC' if output.startswith('A') else 'NC' if output in ('E1','E3') else 'NO',
                voltage_type='DC',wire_count=4. if output.startswith('A') else 3.,
                connection='cable+connector' if m['conn'] and m['cable'] else 'connector' if m['conn'] else 'cable')
    # Modern declarations only promise a metal sleeve for GM; no alloy default.
    if m['material'] in ('S','H','K'):values['material']='plastic' if m['material']=='K' else 'stainless'
    if m['conn']:values['connector']='m8' if m['conn']=='V3' else 'm12'
    return dict(values=values,source='https://files.pepperl-fuchs.com/online-catalogs/245613/files/assets/basic-html/page37.html',
                section='Type code, inductive standard sensors',extras={'Длина до конца резьбы в обозначении':m['length']},
                notes=['Длина до конца резьбы не подставляется как общая длина корпуса. Диапазон питания, шаг, IP, температура и ток берутся из карточки.'])


def teko(code):
    code=re.sub(r'^ДАТЧИКИНДУКТИВНЫЙ','',code)
    # General switching series only: analog/NAMUR/speed/ex have separate keys.
    m=re.fullmatch(r'IS(?P<mount>B|N)(?P<body>BS|FS|A|B|C|D|E|F|G|H|I|L|M)(?P<conn>F|C|T|G)?(?P<size>\d{1,3})(?P<material>A|S|F|B|P)(?P<ip>5|8)?-(?P<wires>[2345])(?P<function>[123])(?P<out>P|N)?(?P<ground>G?)(?P<shield>S?)-(?P<adjust>R?)(?P<sn>\d+(?:[.,]\d+)?)(?P<load>[ABCDEFGHI]|M)?(?:-(?P<led>L?)(?P<protection>Z|E|P|T)?(?P<connector>S401|S402|S40|S4|S27|R181|R18|R14|R11|R10|R9|R7|R4)?)?(?:-(?P<temp>C1|C2|CH|S|C|T|H|K|D|Q|G))?',code)
    if not m:return None
    if m['wires'] in ('3','4','5') and not m['out']:return None
    if m['wires']=='2' and m['out']:return None
    body=m['body'];conn=m['conn']
    values=dict(mount='flush' if m['mount']=='B' else 'non-flush',
                body_type='threaded' if body in ('A','B','BS','E','F','FS') else 'smooth' if body in ('C','D','G','H') else 'rectangular',
                material={'A':'aluminium','S':'stainless','F':'steel','B':'brass','P':'plastic'}[m['material']],
                voltage_type='DC',vmin=10.,vmax=30.,wire_count=float(m['wires']),
                output={'P':'PNP','N':'NPN',None:'2-wire'}[m['out']],
                function={'1':'NO','2':'NC','3':'NO/NC'}[m['function']],sn=float(m['sn'].replace(',','.')),
                connection='connector' if conn=='C' else 'terminals' if conn=='T' else 'cable',
                ip=({'5':'65','8':'68',None:'67'}[m['ip']],))
    if m['connector'] and conn not in ('C','T'):values['connection']='cable+connector'
    # Type-size is a catalog index with multiple possible diameters, not mm.
    if m['load']:values['load']={'A':50.,'B':100.,'C':150.,'D':200.,'E':250.,'F':400.,'G':500.,'H':750.,'I':1000.,'M':20.}[m['load']]
    if m['temp']:
        values['tmin'],values['tmax']={'C':(-45.,65.),'C1':(-45.,90.),'T':(-25.,75.),'H':(-15.,105.),'CH':(-40.,105.),'S':(-5.,120.),'K':(0.,150.),'D':(-60.,65.),'C2':(-60.,90.),'Q':(-15.,105.),'G':(-5.,120.)}[m['temp']]
    return dict(values=values,source='https://teko-com.ru/useful-info/sistemy-oboznachenija/sistema-oboznacheniya-induktivnykh-vyklyuchateley/',
                section='Система обозначения индуктивных выключателей',
                extras={'Индекс типоразмера':m['size']},
                notes=['Индекс типоразмера не является диаметром. Размеры, шаг, стандартная температура и частота не выводятся из индекса.'])


DECODERS={'Autonics':autonics,'BESKONTA':beskonta,'LANBAO':lanbao,'Pepperl+Fuchs':pepperl_fuchs,'ТЕКО':teko}
