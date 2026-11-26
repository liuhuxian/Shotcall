# -*- coding: utf-8 -*-
"""00–09 阶段定义：与导演台无关，只描述"项目长什么样"。

阶段编号、目录名、每阶段的产出物模式（用于识别成果落在哪一阶段）。
"""

STAGES = [
    {"no": "00", "dir": "00-原始资料", "name": "原始资料",
     "desc": "原著、素材、来源说明", "patterns": [r"\.txt$", r"\.docx?$", r"\.pdf$"]},
    {"no": "01", "dir": "01-概念锚定", "name": "概念锚定",
     "desc": "故事内核、调性、一句话梗概", "patterns": [r"synopsis", r"concept", r"logline"]},
    {"no": "02", "dir": "02-角色圣经", "name": "角色圣经",
     "desc": "人物弧光、关系、说话方式", "patterns": [r"character"]},
    {"no": "03", "dir": "03-世界观", "name": "世界观",
     "desc": "时空、社会规则、场景体系", "patterns": [r"world", r"setting"]},
    {"no": "04", "dir": "04-分场大纲", "name": "分场大纲",
     "desc": "分集与分场事件表", "patterns": [r"treatment", r"outline", r"beat"]},
    {"no": "05", "dir": "05-正式剧本", "name": "正式剧本",
     "desc": "可拍摄剧本、时长复核、改编对账", "patterns": [r"screenplay", r"script"]},
    {"no": "06", "dir": "06-剧本压力测试", "name": "剧本压力测试",
     "desc": "结构体检与必改项", "patterns": [r"stress", r"audit"]},
    {"no": "07", "dir": "07-图像资产", "name": "图像资产",
     "desc": "角色卡、场景、道具、绑定图", "patterns": [r"assets?", r"asset-bindings"]},
    {"no": "08", "dir": "08-表演设计", "name": "表演设计",
     "desc": "逐场情绪、目标、节拍、表演段落", "patterns": [r"acting"]},
    {"no": "09", "dir": "09-镜头与视频提示词", "name": "镜头与视频提示词",
     "desc": "镜头表、生成单元、H3 提示词、生成结果", "patterns": [r"shot-plan", r"generation-units",
                                                              r"minimax-h3", r"director-plan",
                                                              r"scene-timing", r"continuity-ledger"]},
]

STATUS = {
    "draft": "草稿待确认",
    "confirmed": "已确认-当前",
    "superseded": "历史已替代",
    "conclusion": "已完成-结论",
    "expired": "已过期-禁止使用",
    "missing": "文件缺失",
}

# 成果状态字符串 → 归一化键
STATUS_MAP = {v: k for k, v in STATUS.items()}


def by_no() -> dict:
    return {s["no"]: s for s in STAGES}


def stage_of_kind(kind: str) -> str:
    """从登记表的 kind（如 '05 生产拆解（07入口派生）'）里取阶段号。"""
    head = (kind or "").strip()[:2]
    return head if head.isdigit() else ""


def stage_of_path(path: str) -> str:
    head = (path or "").split("/")[0][:2]
    return head if head.isdigit() else ""
