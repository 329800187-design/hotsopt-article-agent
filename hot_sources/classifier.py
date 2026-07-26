from __future__ import annotations


CATEGORIES = ["社会民生", "财经商业", "科技数码", "体育赛事", "娱乐影视", "教育职场", "健康科普", "国际时事", "综合热点"]

SOURCE_CATEGORY_MAP = {
    "社会": "社会民生", "民生": "社会民生", "财经": "财经商业", "商业": "财经商业", "科技": "科技数码", "数码": "科技数码", "体育": "体育赛事", "娱乐": "娱乐影视", "影视": "娱乐影视", "教育": "教育职场", "职场": "教育职场", "健康": "健康科普", "国际": "国际时事", "时事": "国际时事",
}

CATEGORY_RULES = {
    "社会民生": ["民生", "家庭", "婚姻", "夫妻", "老人", "孩子", "学校", "医院", "餐厅", "服务员", "小区", "工资", "就业", "消费", "农村", "随礼", "房价", "交通", "事故", "社区", "物业", "外卖", "快递", "水电", "暴雨", "台风", "地震"],
    "财经商业": ["股票", "A股", "基金", "银行", "人民币", "美元", "黄金", "房价", "经济", "公司", "上市", "裁员", "市场", "消费"],
    "科技数码": ["人工智能", "AI", "芯片", "手机", "电脑", "机器人", "科技", "卫星", "汽车", "新能源", "互联网", "模型", "iPhone", "iPad", "Mac", "华为", "小米", "OPPO", "vivo", "5G", "6G", "app", "App", "软件", "游戏"],
    "体育赛事": ["世界杯", "奥运", "亚运", "足球", "篮球", "网球", "冠军", "比赛", "球员", "运动员", "联赛", "CBA", "NBA", "决赛", "夺冠", "赛季", "中超", "英超", "跑步", "健身", "马拉松"],
    "娱乐影视": ["明星", "演员", "电影", "电视剧", "综艺", "歌手", "演唱会", "娱乐", "导演", "票房"],
    "教育职场": ["高考", "中考", "大学", "高校", "老师", "学生", "教育", "职场", "面试", "考试", "毕业"],
    "健康科普": ["健康", "医院", "医生", "疾病", "癌症", "流感", "药", "饮食", "养生", "近视"],
    "国际时事": ["美国", "俄罗斯", "乌克兰", "欧洲", "日本", "韩国", "以色列", "伊朗", "国际", "外交", "联合国", "南海", "俄", "中俄", "中美", "北约", "欧盟", "制裁", "冲突", "峰会", "普京", "特朗普", "关税", "贸易战", "出口", "进口", "外交部", "使馆", "大使", "边境", "会谈", "协议", "访华"],
}


def classify_topic(title: str, summary: str = "", source_category: str = "") -> str:
    normalized_source = normalize_category(source_category, title, summary) if source_category else ""
    if normalized_source:
        return normalized_source
    text = f"{title} {summary}"
    scores = {category: sum(text.count(keyword) for keyword in keywords) for category, keywords in CATEGORY_RULES.items()}
    best_category, best_score = max(scores.items(), key=lambda item: item[1])
    return best_category if best_score else "综合热点"


def normalize_category(value: str, title: str = "", summary: str = "") -> str:
    candidate = str(value or "").strip()
    if candidate in CATEGORIES:
        return candidate
    if candidate in SOURCE_CATEGORY_MAP:
        return SOURCE_CATEGORY_MAP[candidate]
    if candidate in {"新闻", "热榜", "推荐", "其他", "未分类", "未知"} or not candidate:
        return classify_topic(title, summary, "") if (title or summary) else ""
    return classify_topic(title, summary) if (title or summary) else "综合热点"
