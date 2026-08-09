"""Human-authored English speaking cards for the first voice-native release."""

from __future__ import annotations

from typing import Final

from services.tutor.domain import LessonDifficulty, LessonTask, TutorFocus

_LessonSeed = tuple[str, str, str, str, LessonDifficulty]

# These are scenario cards, not an answer-bearing question bank. Keeping them
# in source makes the first release reviewable and deterministic.
_ENGLISH_SCENARIOS: Final[tuple[_LessonSeed, ...]] = (
    ("new-classmate", "认识新同学", "自然问候并介绍姓名", "Hi, I'm Alex. What's your name?", "starter"),
    ("self-introduction", "一分钟自我介绍", "用三句话介绍自己", "Tell me your name, grade, and one thing you enjoy.", "starter"),
    ("classroom-help", "课堂求助", "礼貌请求老师帮助", "You don't understand the exercise. Ask me for help.", "beginner"),
    ("ask-repeat", "请求重复", "没听清时请求重复", "Sorry, the room is noisy. What would you say?", "starter"),
    ("ask-clarify", "请求解释", "用英语确认词义", "You heard a new word. Ask what it means.", "beginner"),
    ("cafeteria-order", "食堂点餐", "点一份餐并确认数量", "Welcome! What would you like for lunch?", "starter"),
    ("food-preference", "饮食偏好", "表达喜欢和不喜欢的食物", "What food do you like, and what don't you like?", "starter"),
    ("convenience-store", "便利店购物", "询问商品位置并结账", "Hello! Can I help you find something?", "beginner"),
    ("clothes-shopping", "购买衣服", "说明想要的衣服", "What are you looking for today?", "beginner"),
    ("size-and-price", "尺码与价格", "询问尺码、颜色和价格", "This shirt comes in three colors. What would you ask?", "beginner"),
    ("school-directions", "校园问路", "询问并复述路线", "You're near the library. Where do you need to go?", "beginner"),
    ("bus-route", "乘坐公交", "确认线路和站点", "Which bus are you trying to take?", "beginner"),
    ("subway-ticket", "购买地铁票", "说明目的地并购票", "Where are you going, and how many tickets do you need?", "beginner"),
    ("taxi-destination", "打车出行", "说明目的地和路线偏好", "Good afternoon. Where would you like to go?", "beginner"),
    ("weather-small-talk", "谈论天气", "描述天气并自然接话", "It's sunny today. What is the weather like where you are?", "starter"),
    ("weekend-plan", "周末计划", "用将来时说计划", "What are you going to do this weekend?", "beginner"),
    ("hobby-chat", "兴趣爱好", "介绍爱好并追问对方", "What do you like doing after school?", "starter"),
    ("sports-chat", "运动话题", "谈喜欢的运动和频率", "Do you play any sports?", "starter"),
    ("music-and-movies", "音乐与电影", "表达偏好并给出原因", "What kind of music or movies do you enjoy?", "beginner"),
    ("talk-about-pets", "聊聊宠物", "描述动物外貌与习惯", "Do you have a pet, or would you like one?", "beginner"),
    ("family-introduction", "介绍家人", "简短介绍家庭成员", "Tell me about one person in your family.", "beginner"),
    ("daily-routine", "日常作息", "按顺序描述一天", "What do you usually do before school?", "beginner"),
    ("morning-routine", "早晨准备", "使用先后连接词", "Walk me through your morning in three steps.", "beginner"),
    ("make-appointment", "约定时间", "询问时间并确认安排", "Are you free after school on Friday?", "beginner"),
    ("invite-friend", "邀请朋友", "发出包含时间地点的邀请", "Invite me to do something this weekend.", "beginner"),
    ("accept-decline", "接受或婉拒", "礼貌回应邀请并说明原因", "Would you like to see a movie tonight?", "beginner"),
    ("phone-call", "打电话", "开场、说明来意和结束通话", "Hello, this is Sam speaking. How can I help?", "intermediate"),
    ("voice-message", "语音留言", "留下简洁完整的信息", "I'm not available. Please leave a short message.", "intermediate"),
    ("describe-symptom", "描述不舒服", "用简单英语说明症状并求助", "You don't feel well. Tell the school nurse what happened.", "intermediate"),
    ("ask-pharmacist", "药店求助", "说明一般需求并听从专业建议", "Hello. What do you need help with today?", "intermediate"),
    ("hotel-checkin", "酒店入住", "提供预订信息并确认房间", "Welcome. Do you have a reservation?", "intermediate"),
    ("airport-checkin", "机场值机", "回答证件、行李和座位问题", "May I see your passport and ticket, please?", "intermediate"),
    ("security-check", "安全检查", "理解并回应简短指令", "Please place your bag on the belt.", "intermediate"),
    ("border-questions", "入境问答", "说明旅行目的和停留时间", "What is the purpose of your visit?", "intermediate"),
    ("restaurant-booking", "餐厅订位", "预订人数、日期和时间", "Thank you for calling. When would you like a table?", "intermediate"),
    ("read-a-menu", "看菜单点餐", "询问菜品并完成点单", "Would you like a moment to look at the menu?", "beginner"),
    ("polite-complaint", "礼貌反馈问题", "清楚描述问题并提出合理请求", "Is everything okay with your order?", "intermediate"),
    ("lost-item", "寻找失物", "描述物品和最后出现地点", "What did you lose, and where did you last see it?", "intermediate"),
    ("ask-for-help", "向陌生人求助", "礼貌开口并说明需求", "You look a little lost. Do you need help?", "beginner"),
    ("mini-presentation", "课堂小演讲", "有开场、要点和结尾地表达", "Give a short talk about your favorite place.", "intermediate"),
    ("share-opinion", "表达观点", "说出观点并给一个理由", "Should students have less homework? What do you think?", "intermediate"),
    ("agree-disagree", "同意与不同意", "尊重地回应不同观点", "I think school uniforms are useful. Do you agree?", "intermediate"),
    ("past-story", "讲述过去经历", "用过去时讲清事件顺序", "Tell me about something fun you did last week.", "intermediate"),
    ("future-goal", "未来目标", "描述目标和一个行动", "What is one thing you want to learn this year?", "intermediate"),
    ("student-interview", "校园小采访", "回答基本经历与优势问题", "Tell me about a school project you're proud of.", "intermediate"),
    ("teamwork", "团队合作", "提出分工并确认意见", "We have a group project. How should we divide the work?", "intermediate"),
    ("make-apology", "真诚道歉", "说明错误、道歉并提出补救", "You returned my book late. What would you say?", "beginner"),
    ("thanks-compliment", "感谢与赞美", "自然表达感谢并回应赞美", "Your presentation was very clear!", "beginner"),
    ("emergency-help", "紧急求助", "清楚说地点、问题和需要", "You need urgent help. Tell me where you are and what happened.", "intermediate"),
    ("weekly-recap", "本周自由回顾", "综合运用本周表达完成对话", "What was the best part of your week, and why?", "intermediate"),
)

ENGLISH_LESSONS: Final[tuple[LessonTask, ...]] = tuple(
    LessonTask(
        task_id=f"english-{task_id}",
        focus="tutor_english",
        title=title,
        objective=objective,
        opening_prompt=opening_prompt,
        difficulty=difficulty,
        skill_keys=(task_id,),
    )
    for task_id, title, objective, opening_prompt, difficulty in _ENGLISH_SCENARIOS
)
HOMEWORK_COMPANION_TASK: Final = LessonTask(
    task_id="homework-self-guided",
    focus="tutor_homework",
    title="作业陪伴督导",
    objective="自己念题、梳理条件并完成一个可执行的下一步",
    opening_prompt="先把题目和你已经想到的部分说给我听，我们一次只推进一步。",
    difficulty="starter",
    skill_keys=("homework-planning",),
)
ALL_LESSONS: Final = (*ENGLISH_LESSONS, HOMEWORK_COMPANION_TASK)
_BY_ID: Final[dict[str, LessonTask]] = {task.task_id: task for task in ALL_LESSONS}


def lesson_task(task_id: str) -> LessonTask | None:
    return _BY_ID.get(task_id)


def lessons_for(
    *,
    difficulty: LessonDifficulty | None = None,
    focus: TutorFocus = "tutor_english",
) -> tuple[LessonTask, ...]:
    lessons = tuple(task for task in ALL_LESSONS if task.focus == focus)
    if difficulty is not None:
        lessons = tuple(task for task in lessons if task.difficulty == difficulty)
    return lessons
