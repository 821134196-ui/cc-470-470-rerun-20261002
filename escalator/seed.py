"""演示数据：楼层扶梯、四类岗位人员。"""

PEOPLE = [
    {"id": "op1",   "name": "王建国", "role": "operator",   "role_name": "值班员"},
    {"id": "op2",   "name": "李秀兰", "role": "operator",   "role_name": "值班员"},
    {"id": "insp1", "name": "张晨",   "role": "inspector",  "role_name": "巡检员"},
    {"id": "insp2", "name": "赵雪",   "role": "inspector",  "role_name": "巡检员"},
    {"id": "rep1",  "name": "孙志强", "role": "repairer",   "role_name": "维修工"},
    {"id": "rep2",  "name": "周磊",   "role": "repairer",   "role_name": "维修工"},
    {"id": "sup1",  "name": "陈敏",   "role": "supervisor", "role_name": "主管"},
    {"id": "svc1",  "name": "刘芳",   "role": "service",    "role_name": "客服"},
]

PERSON_BY_ID = {p["id"]: p for p in PEOPLE}

# 值班员可启动；巡检员负责检查/急停/复检/审批（不可审批本人复检）；
# 维修工处置工单；客服仅可发布绕行信息；主管可调方向/派单/审批。
ROLE_RIGHTS = {
    "operator":   {"label": "值班员", "rights": {"start", "stop", "reopen", "barrier"}},
    "inspector":  {"label": "巡检员", "rights": {"check", "estop", "report", "recheck", "review"}},
    "repairer":   {"label": "维修工", "rights": {"report", "dispatch", "arrive", "repair", "extend"}},
    "supervisor": {"label": "主管",   "rights": {"direction", "report", "dispatch", "review", "extend"}},
    "service":    {"label": "客服",   "rights": {"detour"}},
}

ESCALATORS = [
    {"id": "EA", "name": "东翼 1 号扶梯", "low_floor": "F1", "high_floor": "F2",
     "planned_direction": "up",   "schedule_start": "09:55", "schedule_stop": "22:00"},
    {"id": "EB", "name": "东翼 2 号扶梯", "low_floor": "F1", "high_floor": "F2",
     "planned_direction": "down", "schedule_start": "09:55", "schedule_stop": "22:00"},
    {"id": "EC", "name": "中庭 1 号扶梯", "low_floor": "F2", "high_floor": "F3",
     "planned_direction": "up",   "schedule_start": "09:55", "schedule_stop": "22:00"},
    {"id": "ED", "name": "中庭 2 号扶梯", "low_floor": "F2", "high_floor": "F3",
     "planned_direction": "down", "schedule_start": "10:00", "schedule_stop": "22:00"},
    {"id": "EE", "name": "西翼 1 号扶梯", "low_floor": "F3", "high_floor": "F4",
     "planned_direction": "up",   "schedule_start": "10:00", "schedule_stop": "21:30"},
]

FLOORS = ["F1", "F2", "F3", "F4"]
