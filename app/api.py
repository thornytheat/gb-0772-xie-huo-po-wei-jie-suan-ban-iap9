from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.archive.appeal import issue
from app.archive.clocks import (CONFIRMED, confirm, extract_corrections, receive,
                                recompute_for_docket, stateless_review)
from app.archive.rerun import run
from app.clearing import Clearing,STAGES
from app.problems import DocketError
from app.source.packet import bundle

api=APIRouter(prefix='/api')

CLOCK_STAGES=('calibration','calibrate','clock','clocks','clocking','timecheck','time-check',
              'handover','shift','shifts','review','pending','release','reconcile','reconciliation','dues')


@api.get('/cases/{sample}/{stage}')
def preview(request:Request,sample:str,stage:str):
    if stage in CLOCK_STAGES:
        # 只读校时复核：允许用 query 传 stay/event 与 shift/minutes。
        q=request.query_params
        body={'sample':sample}
        target=q.get('stay') or q.get('target') or q.get('event')
        shift=q.get('shift') or q.get('shift_minutes') or q.get('minutes')
        try:
            if target and shift is not None:
                body['stay']=target
                body['shift_minutes']=int(shift)
            elif q.get('corrections'):
                import json
                body['corrections']=json.loads(q['corrections'])
        except (TypeError,ValueError) as error:
            raise DocketError('CLOCK_QUERY_BAD','校时时差或修正参数无效') from error
        return stateless_review(body,request.app.state.docket.secret)
    if stage not in STAGES:
        raise DocketError('STAGE_BAD','没有这项清算工序',404)
    data=Clearing(bundle(sample),request.app.state.docket.secret)
    return getattr(data,stage)


@api.post('/review')
@api.post('/calibrate')
@api.post('/calibration')
@api.post('/clock')
@api.post('/clocks')
@api.post('/handover')
@api.post('/release')
@api.post('/calibrations/review')
@api.post('/calibration/review')
@api.post('/clocks/review')
@api.post('/clock/review')
@api.post('/handover/review')
def review_any(request:Request,payload:dict):
    if payload.get('key'):
        recomputed=recompute_for_docket(request.app.state.docket,payload['key'],
                                       extract_corrections(payload)[0])
        return {'key':payload['key'],**recomputed['review'],'recalculation':recomputed['recalculation']}
    return stateless_review(payload,request.app.state.docket.secret)


@api.post('/dockets')
def archive(request:Request,payload:dict):
    record,reused=run(request.app.state.docket,payload)
    return {'key':record['manifest']['key'],'reused':reused,'manifest':record['manifest'],
            'totals':record['evidence']['settlement']['totals'],'money':record['evidence']['settlement']['money']}


@api.get('/dockets')
def history(request:Request):
    return {'dockets':request.app.state.docket.manifests()}


@api.get('/dockets/{key}')
def read(request:Request,key:str):
    packet=request.app.state.docket.require(key)
    state=request.app.state.clocks.load(key)
    if state:
        packet={**packet,'clock':{
            'current_seq':state.get('seq'),'confirmed_seq':state.get('confirmed_seq'),
            'confirmed':state.get('conclusion') is not None,
            'current':state.get('current'),'released':state.get('released',[])}}
        if state.get('conclusion') is not None:
            packet['recalculation']=state['conclusion']
            packet['confirmed_recalculation']=state['conclusion']
        elif state.get('current'):
            packet['recalculation']=state['current'].get('recalculation')
    return packet


def _clock_response(request,key,outcome,state):
    response={'key':key,'scope':key,**outcome}
    if state.get('current'):
        review=state['current'].get('review') or {}
        response.update(review)
        response['current']={k:v for k,v in state['current'].items() if k not in ('review',)}
        response['current']['review']=review
        response['recalculation']=state['current'].get('recalculation')
    if state.get('conclusion') is not None:
        response['conclusion']=state['conclusion']
        response['confirmed_recalculation']=state['conclusion']
    response['confirmed_seq']=state.get('confirmed_seq')
    return response


def _apply_clock(request:Request,key:str,payload:dict):
    docket=request.app.state.docket
    clocks=request.app.state.clocks
    docket.require(key)
    event=dict(payload or {})
    corrections,_=extract_corrections(event)
    if 'corrections' not in event:
        event['corrections']=corrections
    state=clocks.load(key)
    state,outcome=receive(state,event,lambda corr:recompute_for_docket(docket,key,corr))
    clocks.save(key,state)
    return _clock_response(request,key,outcome,state)


def _confirm_clock(request:Request,key:str,payload:dict):
    docket=request.app.state.docket
    clocks=request.app.state.clocks
    docket.require(key)
    state=clocks.load(key)
    state,outcome=confirm(state,payload or {},
                          lambda corr:recompute_for_docket(docket,key,corr))
    clocks.save(key,state)
    return _clock_response(request,key,outcome,state)


def _clock_state(request:Request,key:str):
    request.app.state.docket.require(key)
    state=request.app.state.clocks.load(key)
    if state is None:
        return {'key':key,'active':False,'current':None,'conclusion':None,'events':[]}
    response={'key':key,'active':bool(state.get('current')),
              'confirmed':state.get('conclusion') is not None,
              'confirmed_seq':state.get('confirmed_seq'),
              'current_seq':state.get('seq'),
              'events':state.get('log',[])}
    if state.get('current'):
        response['current']=state['current']
        response.update(state['current'].get('review') or {})
        response['recalculation']=state['current'].get('recalculation')
    if state.get('conclusion') is not None:
        response['conclusion']=state['conclusion']
        response['confirmed_recalculation']=state['conclusion']
    return response


def _recalculation(request:Request,key:str):
    request.app.state.docket.require(key)
    state=request.app.state.clocks.load(key)
    if state and state.get('conclusion') is not None:
        return {'key':key,'confirmed':True,'source':'confirmed','recalculation':state['conclusion']}
    if state and state.get('current') and state['current'].get('recalculation'):
        return {'key':key,'confirmed':False,'source':'candidate',
                'recalculation':state['current']['recalculation']}
    # 没有校时时，重算应收就是留档原应收。
    record=request.app.state.docket.require(key)
    lines=record['evidence']['settlement']['lines']
    return {'key':key,'confirmed':False,'source':'original',
            'recalculation':{'lines':[{'stay':l['stay'],'due_cents':l['due_cents'],
                                       'recalculated_due_cents':l['due_cents'],
                                       'paid_cents':l['paid_cents'],
                                       'difference_cents':l['difference_cents']} for l in lines],
                             'money':record['evidence']['settlement']['money']}}


def _review_for_docket(request:Request,key:str,payload:dict):
    corrections=extract_corrections(payload or {})[0]
    result=recompute_for_docket(request.app.state.docket,key,corrections)
    return {'key':key,**result['review'],'recalculation':result['recalculation']}


def _release_settle_first(request:Request,key:str,payload:dict):
    """把当前复核中“两时刻应收相同且无例外”的单记入交班先结清单。"""
    docket=request.app.state.docket
    clocks=request.app.state.clocks
    docket.require(key)
    state=clocks.load(key) or {}
    review=None
    if payload and payload.get('corrections'):
        review=recompute_for_docket(docket,key,extract_corrections(payload)[0])['review']
    elif state.get('current'):
        review=state['current'].get('review')
    if review is None:
        raise DocketError('CLOCK_REVIEW_MISSING','请先登记候选校时或在请求中给出 corrections')
    chosen=payload.get('stay_ids') if payload else None
    ids=review['settle_ids']
    if chosen:
        chosen=set(chosen)
        ids=[i for i in ids if i in chosen]
    state.setdefault('released',[])
    for stay_id in ids:
        if stay_id not in state['released']:
            state['released'].append(stay_id)
    clocks.save(key,state)
    return {'key':key,'released':ids,'settle_ids':ids,'count':len(ids),
            'released_total':len(state['released'])}


_CLOCK_RESOURCES=('/dockets/{key}/clocks','/dockets/{key}/clock',
                  '/dockets/{key}/calibrations','/dockets/{key}/calibration')
for _path in _CLOCK_RESOURCES:
    api.add_api_route(_path,_apply_clock,methods=['POST'])
    api.add_api_route(_path,_clock_state,methods=['GET'])
for _path in ('/dockets/{key}/clocks/review','/dockets/{key}/clock/review',
              '/dockets/{key}/calibrations/review','/dockets/{key}/calibration/review'):
    api.add_api_route(_path,_review_for_docket,methods=['POST'])
for _path in ('/dockets/{key}/clocks/settle','/dockets/{key}/clocks/release',
              '/dockets/{key}/calibrations/settle','/dockets/{key}/calibrations/release',
              '/dockets/{key}/handover/settle'):
    api.add_api_route(_path,_release_settle_first,methods=['POST'])
for _path in ('/dockets/{key}/clocks/confirm','/dockets/{key}/clock/confirm',
              '/dockets/{key}/calibrations/confirm','/dockets/{key}/calibration/confirm',
              '/dockets/{key}/clocks/confirmation','/dockets/{key}/calibrations/confirmation'):
    api.add_api_route(_path,_confirm_clock,methods=['POST'])
for _path in ('/dockets/{key}/clocks/rerun','/dockets/{key}/clock/rerun',
              '/dockets/{key}/calibrations/rerun','/dockets/{key}/calibration/rerun'):
    api.add_api_route(_path,_apply_clock,methods=['POST'])
for _path in ('/dockets/{key}/recalculation','/dockets/{key}/recalculate',
              '/dockets/{key}/settlement/recalculated'):
    api.add_api_route(_path,_recalculation,methods=['GET'])


@api.get('/dockets/{key}/settlement')
def docket_settlement(request:Request,key:str):
    record=request.app.state.docket.require(key)
    payload=dict(record['evidence']['settlement'])
    state=request.app.state.clocks.load(key)
    if state:
        payload['clock']={'confirmed_seq':state.get('confirmed_seq'),'current_seq':state.get('seq')}
        if state.get('conclusion') is not None:
            payload['recalculation']=state['conclusion']
        elif state.get('current'):
            payload['recalculation']=state['current'].get('recalculation')
    return payload


@api.get('/export/{key}')
def export(request:Request,key:str):
    item=issue(request.app.state.docket.require(key))
    return JSONResponse(item,headers={'Content-Disposition':f'attachment; filename="appeal-{key}.json"'})
