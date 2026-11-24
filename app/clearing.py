from functools import cached_property

from app.billing.rates import price
from app.billing.settle import accounting
from app.curb.bookings import at_arrival,revisions
from app.curb.calibrate import assess
from app.curb.detections import prepare
from app.curb.grid import collisions
from app.curb.merge import stays
from app.source.packet import load
from app.source.plates import scrub

STAGES=('intake','identity','occupancy','pricing','settlement')


class Clearing:
    def __init__(self, files, secret):
        self.survey,self.sheets=load(files)
        self.secret=secret

    @cached_property
    def anonymous(self):
        return {name:scrub(rows,self.secret) for name,(rows,_) in self.sheets.items()}

    @cached_property
    def intake(self):
        detections, sensor_issues=prepare(self.anonymous['sensors'][0],self.survey)
        bookings, booking_issues=revisions(self.anonymous['bookings'][0],self.survey)
        sheets=[]
        for name,(_,report) in self.sheets.items():
            _,invalid_plates=self.anonymous[name]
            sheets.append(report | {'bad_plates':invalid_plates})
        return {'sheets':sheets,'sensor_issues':sensor_issues,'booking_issues':booking_issues,
                'kept_sensors':len(detections),'booking_ids':len(bookings)}

    @cached_property
    def combined(self):
        readings,_=prepare(self.anonymous['sensors'][0],self.survey)
        return stays(readings)

    @cached_property
    def occupancy(self):
        combined=self.combined
        _,disputed=self.coverage
        return {'stays':combined,'removed_overlap_minutes':sum(s['observed_minutes']-s['minutes'] for s in combined),
                'disputes':[{'berth':berth,'at':when,'code':code} for (berth,when),code in sorted(disputed.items())]}

    @cached_property
    def coverage(self):
        return collisions(self.combined)

    @cached_property
    def identity(self):
        aliases,issues=revisions(self.anonymous['bookings'][0],self.survey)
        matched=[]
        for stay in self.occupancy['stays']:
            booking,reason,history=at_arrival(stay,aliases)
            matched.append(stay | {'booking':booking,'association':reason,'candidates':history})
        return {'stays':matched,'booking_issues':issues}

    @cached_property
    def pricing(self):
        items=price(self.identity['stays'],self.coverage[1])
        return {'stays':items,'due_cents':sum(s['due_cents'] for s in items)}

    @cached_property
    def settlement(self):
        return accounting(self.pricing['stays'],self.anonymous['payments'][0],self.survey,*self.coverage)

    @cached_property
    def histories(self):
        histories,_=revisions(self.anonymous['bookings'][0],self.survey)
        return histories

    @property
    def event_to_stay(self):
        mapping={}
        for stay in self.occupancy['stays']:
            for detection in stay.get('detections',[]):
                event=(detection.get('fields') or {}).get('event')
                if event is not None:
                    mapping[str(event)]=stay['stay']
        return mapping

    def resolve_corrections(self,corrections):
        """允许校时记录用杆侧 event 编号或序号定位到合并停靠单。"""
        from app.curb.calibrate import correction_list
        event_map=self.event_to_stay
        total=len(self.occupancy['stays'])
        by_id={s['stay']:s['stay'] for s in self.occupancy['stays']}
        resolved=[]
        for key,spec in correction_list(corrections):
            target=key
            event_value=None
            if isinstance(spec,dict):
                event_value=spec.get('event') or spec.get('event_id') or spec.get('record')
            if target is None and event_value is not None:
                target=event_value
            if isinstance(target,str) and target in event_map:
                target=event_map[target]
            elif isinstance(target,str) and target.startswith('event:') and target[6:] in event_map:
                target=event_map[target[6:]]
            elif isinstance(target,str) and target in by_id:
                target=by_id[target]
            elif isinstance(key,int) and 1 <= key <= total:
                # 外部常按 1 基序号指单；stay 哈希无法据此匹配时转成 0 基位置。
                target=key-1
            if isinstance(spec,dict) and event_value is not None and str(event_value) in event_map:
                spec=dict(spec); spec.setdefault('stay',event_map[str(event_value)])
            resolved.append((target,spec))
        return resolved

    def clock_review(self,corrections=None,*,move='both',after=None):
        resolved=self.resolve_corrections(corrections) if corrections is not None else corrections
        return assess(self.occupancy['stays'],after=after,histories=self.histories,
                      corrections=resolved,move=move)

    # 同一“双校时交班复核”能力的常见命名。
    def calibrate(self,corrections=None,**kwargs):
        return self.clock_review(corrections,**kwargs)

    release_plan=calibrate
    compare_clocks=calibrate

    class ReviewView(dict):
        """无参访问即默认复核结论（可下标），加修正调用即重新复核。"""
        def __init__(self,owner):
            super().__init__()
            self._owner=owner
            self.update(owner.clock_review(None))

        def __call__(self,corrections=None,**kwargs):
            return self._owner.clock_review(corrections,**kwargs)

    @cached_property
    def review(self):
        return Clearing.ReviewView(self)

    @cached_property
    def handover(self):
        return Clearing.ReviewView(self)

    @cached_property
    def calibration(self):
        return self.clock_review(None)

    @cached_property
    def clock(self):
        return self.clock_review(None)

    @cached_property
    def clocks(self):
        return self.clock_review(None)

    def report(self):
        return {stage:getattr(self,stage) for stage in STAGES}
