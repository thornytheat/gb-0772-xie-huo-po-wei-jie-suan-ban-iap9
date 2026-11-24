from pathlib import Path
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, RedirectResponse
from jinja2 import Environment,FileSystemLoader,select_autoescape

from app.archive.appeal import issue
from app.archive.clocks import confirm as confirm_clock
from app.archive.clocks import public_review
from app.archive.clocks import receive as receive_clock
from app.archive.clocks import recompute_for_docket
from app.clearing import Clearing
from app.curb.timeaxis import local
from app.problems import DocketError,LABEL
from app.setup import POLICY
from app.source.packet import bundle

STEPS={'intake':'停靠与预约采信','identity':'车牌匿名关联','occupancy':'占用区间重建','pricing':'首免与升级价',
       'settlement':'逐单与泊位对账','history':'清算批次复跑','appeal':'理赔证据签发'}
workbench=APIRouter()
views=Environment(loader=FileSystemLoader(Path(__file__).parent/'templates'),
                  autoescape=select_autoescape(('html','j2')))


def yuan(cents):
    prefix='−' if cents<0 else ''
    weight,fraction=divmod(abs(cents),100)
    return f'{prefix}¥{weight:,}.{fraction:02d}'


@workbench.get('/',response_class=HTMLResponse)
@workbench.get('/clearing/{screen}',response_class=HTMLResponse)
def screen(request:Request,screen:str='overview',sample:str='dirty',key:str|None=None):
    if screen!='overview' and screen not in STEPS:
        raise DocketError('STAGE_BAD','没有这项清算工序',404)
    docket=request.app.state.docket
    clocks=request.app.state.clocks
    signed=None
    live_clearing=None
    if key:
        record=docket.require(key)
        report,meta=record['evidence'],record['manifest']
        if meta['source'] in {'clean','dirty'}:
            sample=meta['source']
        if screen=='appeal':
            signed=issue(record)
    else:
        live_clearing=Clearing(bundle(sample),docket.secret)
        report=jsonable_encoder(live_clearing.report())
        meta={'rule':POLICY,'survey':jsonable_encoder(vars(live_clearing.survey))}
    stages=list(STEPS)
    index=stages.index(screen) if screen in stages else -1
    history=docket.manifests() if screen=='history' else []
    path=lambda page: str(request.url_for('screen',screen=page).include_query_params(sample=sample,**({'key':key} if key else {})))

    clock_state=clocks.load(key) if key else None
    clock_notice=CLOCK_NOTICE.get(request.query_params.get('notice'),request.query_params.get('notice'))
    clock_review=None
    cw_stay=request.query_params.get('cw_stay')
    cw_shift=request.query_params.get('cw_shift')
    if screen=='settlement' and cw_stay and cw_shift not in (None,''):
        try:
            shift=int(cw_shift)
            corrections=[{'stay':cw_stay,'shift_minutes':shift}]
            if key:
                clock_review=recompute_for_docket(docket,key,corrections)['review']
            elif live_clearing is not None:
                clock_review=public_review(live_clearing.clock_review(corrections))
        except (ValueError,KeyError):
            clock_review=None
            clock_notice='校时参数无法用于当前停靠单'

    return views.get_template('desk.html').render(screen=screen,steps=STEPS,index=index,next_step=stages[index+1] if index<6 else None,
        previous=stages[index-1] if index>0 else None,path=path,key=key,sample=sample,report=report,meta=meta,
        history=history,signed=signed,label=LABEL,local=local,yuan=yuan,
        clock_state=clock_state,clock_review=clock_review,clock_notice=clock_notice,
        notice_code=request.query_params.get('notice'),cw_stay=cw_stay,cw_shift=cw_shift)


CLOCK_NOTICE={
    'applied':'晚到校时已登记，当前为候选结论，确认后才改写对账应收。',
    'stale':'这是更旧的校时；新校时已生效，重跑旧校时不改换当前结论。',
    'duplicate':'同一校时记录已处理，结论保持不变。',
    'confirmed':'校时已确认，对账页已给出重算应收。',
    'missing':'请先选择要校时的停靠单和时差。',
}


@workbench.post('/clearing/clock')
async def clock_action(request:Request):
    content_type=request.headers.get('content-type','')
    if 'application/json' in content_type:
        data=await request.json()
    else:
        raw=await request.body()
        data={key:values[-1] for key,values in parse_qs(raw.decode()).items()}
    key=(data.get('key') or '').strip()
    docket=request.app.state.docket
    clock_store=request.app.state.clocks
    docket.require(key)
    action=data.get('action','apply')
    target=data.get('target') or data.get('stay')
    notice='applied'
    try:
        shift=int(data.get('shift') or 0)
        seq=int(data['seq']) if data.get('seq') else None
    except (TypeError,ValueError):
        return RedirectResponse(f'/clearing/settlement?key={key}&notice=missing#clock-desk',status_code=303)
    if action=='confirm':
        state=clock_store.load(key)
        body={'seq':seq} if seq is not None else {}
        state,outcome=confirm_clock(state,body,lambda corr:recompute_for_docket(docket,key,corr))
        notice='confirmed' if outcome.get('changed') else ('duplicate' if outcome.get('reused') else 'stale')
    else:
        if not target or data.get('shift') in (None,''):
            return RedirectResponse(f'/clearing/settlement?key={key}&notice=missing',status_code=303)
        event={'corrections':[{'stay':target,'shift_minutes':shift}]}
        if seq is not None:
            event['seq']=seq
        if data.get('id'):
            event['id']=data['id']
        if action=='rerun_old':
            try:
                event.setdefault('seq',int(data.get('old_seq') or 1))
            except (TypeError,ValueError):
                event.setdefault('seq',1)
        state=clock_store.load(key)
        state,outcome=receive_clock(state,event,lambda corr:recompute_for_docket(docket,key,corr))
        notice=('duplicate' if outcome.get('reused') else 'stale' if outcome.get('stale') else 'applied')
    clock_store.save(key,state)
    return RedirectResponse(f'/clearing/settlement?key={key}&notice={notice}#clock-desk',status_code=303)
