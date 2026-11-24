class DocketError(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code, self.status = code, status


LABEL = {
    'WIDTH':'表单列数错误','BLANK':'空白记录','TIMESTAMP':'时间戳无效','INTERVAL':'区间倒置',
    'UNKNOWN_BERTH':'泊位不存在','RANGE':'窗外记录','UNSET':'字段缺失','DUPLICATE':'重复记录',
    'ID_CONFLICT':'同编号冲突','PLATE':'车牌格式错误','BAD_STATUS':'预约状态无效',
    'PLATE_OVERLAP':'多个车在同一泊位','BERTH_OVERLAP':'同车同时出现在多个泊位',
    'FREE':'预约首免','BASE':'基础价','UPGRADE':'升级价','CANCELLED':'到达前预约撤销',
    'BOOKED':'预约已关联','WALK_IN':'无生效预约','GAP':'实收差价','MATCH':'实收一致',
    'NO_PAYMENT':'无实收回执','ORPHAN':'游离实收','ATTACH_CONFLICT':'预约版本冲突',
    'AMOUNT':'金额无效','UNKNOWN_SENSOR':'杆侧来源缺失','NONE':'无占位',
    'SETTLE':'可先结','READY':'可先结','RELEASE':'可先结','CONFIRMABLE':'可确认应收',
    'HOLD':'待核','PENDING':'待核',
    'DUE_CHANGED':'两时刻应收不同','BOOKING_FLIP':'到达越过预约撤销时刻',
    'OVERLAP':'校时诱发同车跨泊位重叠','TIER_JUMP':'时差致后一次停靠跳档',
    'APPLIED':'新校时已生效','STALE':'旧校时重跑未改结论','DUPLICATE':'校时记录重复',
    'CONFIRMED':'校时已确认','CLOCK_OLD_IGNORED':'旧校时不改换当前结论',
    'CLOCK_APPLIED':'校时结论已更新','CLOCK_CONFIRMED':'重算应收已确认',
}
