import os, json, sqlite3, fitz, re, asyncio, hashlib, tempfile, math
from collections import defaultdict
from flask import Flask, request, jsonify, render_template, Response, stream_with_context, send_file
from openai import OpenAI
from curriculum import CURRICULUM

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, template_folder=os.path.join(BASE_DIR, "templates"),
            static_folder=os.path.join(BASE_DIR, "static"))

# ─── Doubao API 配置 ───────────────────────────────────────────────
DOUBAO_API_KEY = "9fb81ccb-ed98-496e-8819-7f6ee7c54abb"
DOUBAO_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
DOUBAO_MODEL = "ep-m-20260302154635-vgxz5"

# ─── PDF文本缓存 ───────────────────────────────────────────────────
PDF_FILES = {
    "yuwen":   "2026春人教版五年级下册语文电子课本.pdf",
    "shuxue":  "2026春人教版五年级下册数学电子课本.pdf",
    "yingyu":  "2026春人教版PEP五年级英语下册电子课本.pdf",
}
_pdf_text_cache = {}

def get_pdf_text(subject, max_pages=None):
    if subject in _pdf_text_cache:
        return _pdf_text_cache[subject]
    path = os.path.join(os.path.dirname(__file__), PDF_FILES[subject])
    if not os.path.exists(path):
        return ""
    doc = fitz.open(path)
    pages = list(doc)[:max_pages] if max_pages else list(doc)
    text = "\n".join(p.get_text() for p in pages)
    _pdf_text_cache[subject] = text
    return text

# ─── SQLite 学习记录 ───────────────────────────────────────────────
DB_PATH = os.path.join(BASE_DIR, "data", "study.db")

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        CREATE TABLE IF NOT EXISTS progress (
            lesson_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'not_started',
            stars INTEGER DEFAULT 0,
            last_study TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS wrong_answers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            lesson_id TEXT,
            question TEXT,
            my_answer TEXT,
            correct_answer TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS mastery (
            lesson_id TEXT PRIMARY KEY,
            stars INTEGER DEFAULT 0,
            best_score INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            last_practiced TEXT,
            next_review TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS user_stats (
            id INTEGER PRIMARY KEY DEFAULT 1,
            total_xp INTEGER DEFAULT 0,
            today_xp INTEGER DEFAULT 0,
            streak_days INTEGER DEFAULT 0,
            last_study_date TEXT
        )
    """)
    con.execute("INSERT OR IGNORE INTO user_stats(id) VALUES(1)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS kp_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT,
            lesson_id TEXT,
            kp TEXT,
            is_correct INTEGER,
            created_at TEXT DEFAULT (datetime('now','localtime'))
        )
    """)
    con.commit()
    con.close()

def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con

# ─── 路由 ─────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/curriculum")
def api_curriculum():
    # 返回去掉大型数据的课程结构（仅供前端渲染菜单）
    return jsonify(CURRICULUM)

@app.route("/api/progress", methods=["GET"])
def api_get_progress():
    con = get_db()
    rows = con.execute("SELECT * FROM progress").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/progress", methods=["POST"])
def api_save_progress():
    data = request.json
    lesson_id = data.get("lesson_id")
    status = data.get("status", "in_progress")
    stars = data.get("stars", 0)
    con = get_db()
    con.execute("""
        INSERT INTO progress(lesson_id, status, stars, last_study)
        VALUES(?, ?, ?, datetime('now','localtime'))
        ON CONFLICT(lesson_id) DO UPDATE SET status=excluded.status,
        stars=MAX(stars, excluded.stars), last_study=excluded.last_study
    """, (lesson_id, status, stars))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/wrong_answers", methods=["GET"])
def api_get_wrong():
    subject = request.args.get("subject")
    con = get_db()
    if subject:
        rows = con.execute("SELECT * FROM wrong_answers WHERE subject=? ORDER BY created_at DESC LIMIT 50", (subject,)).fetchall()
    else:
        rows = con.execute("SELECT * FROM wrong_answers ORDER BY created_at DESC LIMIT 100").fetchall()
    con.close()
    return jsonify([dict(r) for r in rows])

@app.route("/api/wrong_answers", methods=["POST"])
def api_save_wrong():
    data = request.json
    con = get_db()
    con.execute("""
        INSERT INTO wrong_answers(subject, lesson_id, question, my_answer, correct_answer)
        VALUES(?, ?, ?, ?, ?)
    """, (data["subject"], data["lesson_id"], data["question"], data["my_answer"], data["correct_answer"]))
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/wrong_answers/<int:wid>", methods=["DELETE"])
def api_delete_wrong(wid):
    con = get_db()
    con.execute("DELETE FROM wrong_answers WHERE id=?", (wid,))
    con.commit()
    con.close()
    return jsonify({"ok": True})

def _get_client():
    key = DOUBAO_API_KEY or request.headers.get("X-API-Key", "")
    if not key:
        return None, "未配置Doubao API Key，请在启动时设置环境变量 DOUBAO_API_KEY"
    client = OpenAI(api_key=key, base_url=DOUBAO_BASE_URL)
    return client, None

def _build_teacher_system(subject_key, lesson_info):
    subject = CURRICULUM[subject_key]
    base = f"""你是一位专业、亲切、有耐心的小学五年级AI老师，专门教{subject['name']}。
当前学习的课程是：{lesson_info}

你的教学风格：
- 用简单易懂的语言解释，避免过于学术化
- 多用生动的例子和比喻帮助理解
- 鼓励孩子，增强自信心
- 及时表扬正确回答，温和纠正错误
- 提问时循序渐进，由易到难
- 语气亲切活泼，可以用"太棒了！""你真聪明！"等鼓励语

教学目标：帮助学生在期中期末考试中取得优异成绩。
请始终保持中文回答。"""
    return base

@app.route("/api/chat", methods=["POST"])
def api_chat():
    """流式AI对话"""
    data = request.json
    subject_key = data.get("subject", "yuwen")
    lesson_id = data.get("lesson_id", "")
    lesson_name = data.get("lesson_name", "")
    messages = data.get("messages", [])

    client, err = _get_client()
    if err:
        return jsonify({"error": err}), 400

    system_prompt = _build_teacher_system(subject_key, lesson_name)

    full_messages = [{"role": "system", "content": system_prompt}] + messages

    def generate():
        try:
            stream = client.chat.completions.create(
                model=DOUBAO_MODEL,
                messages=full_messages,
                stream=True,
                max_tokens=2000,
                temperature=0.7,
            )
            for chunk in stream:
                delta = chunk.choices[0].delta
                if delta.content:
                    yield f"data: {json.dumps({'content': delta.content}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()), content_type="text/event-stream")

def _fix_json_quotes(s):
    """修复JSON字符串值里未转义的双引号"""
    result = []
    in_string = False
    escape_next = False
    for i, c in enumerate(s):
        if escape_next:
            result.append(c)
            escape_next = False
            continue
        if c == '\\':
            result.append(c)
            escape_next = True
            continue
        if c == '"':
            if not in_string:
                in_string = True
                result.append(c)
            else:
                # 判断是结束引号还是字符串内的裸引号
                # 向后查找：如果后面跟着:,}]空格等则是结束引号
                rest = s[i+1:].lstrip()
                if rest and rest[0] in ':,}]':
                    in_string = False
                    result.append(c)
                else:
                    result.append('\\"')
        else:
            result.append(c)
    return ''.join(result)

@app.route("/api/quiz", methods=["POST"])
def api_quiz():
    """生成练习题（非流式，返回JSON）"""
    data = request.json
    subject_key = data.get("subject", "yuwen")
    lesson_name = data.get("lesson_name", "")
    lesson_key_points = data.get("key_points", [])
    quiz_type = data.get("type", "mixed")  # mixed/choice/fill/qa
    count = min(data.get("count", 5), 10)

    client, err = _get_client()
    if err:
        return jsonify({"error": err}), 400

    subject = CURRICULUM[subject_key]
    kp_str = "、".join(lesson_key_points) if lesson_key_points else lesson_name

    prompt = f"""出{count}道小学五年级{subject['name']}选择题。
课程：{lesson_name}，考查：{kp_str}
重要规则：
1. 只出纯文字题——选项必须是完整的文字描述，绝对不能为空、不能用图形符号代替
2. 不出"看到的图形是（）"之类需要展示图形才能作答的题；改出考查概念/定义/规则/数量/特征的文字题
3. 每题4个不同的文字选项A/B/C/D，answer只写1个大写字母，给出explanation
4. 只输出JSON：{{"questions":[{{"id":1,"type":"choice","question":"题目","options":{{"A":"文字选项","B":"文字选项","C":"文字选项","D":"文字选项"}},"answer":"A","explanation":"解析"}}]}}"""

    try:
        resp = client.chat.completions.create(
            model=DOUBAO_MODEL,
            messages=[
                {"role": "system", "content": "你是小学出题老师。只输出合法JSON对象，禁止输出任何其他文字。所有选项必须是非空的文字内容。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=3000,
            temperature=0.5,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)
        raw = raw.strip()
        raw = raw.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'")
        start = raw.find('{')
        end = raw.rfind('}')
        if start != -1 and end != -1 and end > start:
            json_str = raw[start:end+1]
            try:
                quiz_data = json.loads(json_str)
            except json.JSONDecodeError:
                fixed = _fix_json_quotes(json_str)
                try:
                    quiz_data = json.loads(fixed)
                except json.JSONDecodeError as je2:
                    print(f"[quiz] 解析失败: {je2}\nRAW: {raw[:500]}")
                    return jsonify({"error": "AI返回格式异常，请重试"}), 500

            # 过滤掉选项为空的题目
            valid_qs = []
            for q in quiz_data.get("questions", []):
                opts = q.get("options") or {}
                if opts and all(str(v).strip() for v in opts.values()):
                    valid_qs.append(q)
            if not valid_qs:
                return jsonify({"error": "AI生成的题目选项为空，请重试（提示：该知识点含图形题，点击重试可获取文字题）"}), 500
            quiz_data["questions"] = valid_qs
            return jsonify(quiz_data)
        return jsonify({"error": "AI返回格式异常，请重试", "raw": raw[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/exam", methods=["POST"])
def api_exam():
    """生成模拟考试卷"""
    data = request.json
    subject_key = data.get("subject", "yuwen")
    exam_type = data.get("exam_type", "midterm")  # midterm / final

    client, err = _get_client()
    if err:
        return jsonify({"error": err}), 400

    subject = CURRICULUM[subject_key]
    exam_label = "期中" if exam_type == "midterm" else "期末"

    # 根据科目和考试类型确定范围
    if exam_type == "midterm":
        scope_map = {
            "yuwen": "第一单元至第四单元（童年时光、古典名著、汉字王国、家国情怀）",
            "shuxue": "观察物体、因数和倍数、长方体和正方体（表面积部分）",
            "yingyu": "Unit 1 My Day, Unit 2 My Favourite Season, Unit 3 My School Calendar"
        }
    else:
        scope_map = {
            "yuwen": "全册八个单元",
            "shuxue": "全册所有单元（含分数的意义与性质、加减法、折线统计图、找次品）",
            "yingyu": "全册 Unit 1-6 及 Recycle 1&2"
        }

    scope = scope_map.get(subject_key, "全册内容")

    prompt = f"""请出一份小学五年级{subject['name']}{exam_label}模拟试卷。
考查范围：{scope}
要求：
1. 题型丰富，覆盖核心知识点
2. 难度分布：基础题60%，中等题30%，提高题10%
3. 每道题给出答案和简短解析
4. 严格按JSON格式返回：

{{
  "title": "五年级{subject['name']}{exam_label}模拟试卷",
  "total_score": 100,
  "sections": [
    {{
      "name": "一、选择题",
      "score_each": 2,
      "questions": [
        {{
          "id": 1,
          "type": "choice",
          "question": "...",
          "options": {{"A":"...","B":"...","C":"...","D":"..."}},
          "answer": "A",
          "explanation": "..."
        }}
      ]
    }},
    {{
      "name": "二、填空题",
      "score_each": 2,
      "questions": [...]
    }},
    {{
      "name": "三、判断题",
      "score_each": 1,
      "questions": [
        {{
          "id": 11,
          "type": "judge",
          "question": "...",
          "options": null,
          "answer": "正确",
          "explanation": "..."
        }}
      ]
    }},
    {{
      "name": "四、解答/作文题",
      "score_each": 10,
      "questions": [...]
    }}
  ]
}}"""

    try:
        resp = client.chat.completions.create(
            model=DOUBAO_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=4000,
            temperature=0.7,
        )
        raw = resp.choices[0].message.content.strip()
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            return jsonify(json.loads(match.group()))
        return jsonify({"error": "AI返回格式异常", "raw": raw[:500]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/explain", methods=["POST"])
def api_explain():
    """一键讲解某个知识点（非流式）"""
    data = request.json
    subject_key = data.get("subject", "yuwen")
    topic = data.get("topic", "")
    level = data.get("level", "basic")  # basic/detail

    client, err = _get_client()
    if err:
        return jsonify({"error": err}), 400

    subject = CURRICULUM[subject_key]
    detail = "详细深入" if level == "detail" else "简洁易懂"

    prompt = f"""请用{detail}的方式为小学五年级学生讲解以下{subject['name']}知识点：

【{topic}】

要求：
1. 语言生动活泼，贴近小学生的理解能力
2. 先简单介绍是什么，再用例子说明
3. 给出2-3个记忆小技巧或口诀
4. 最后提示容易犯的错误

请直接开始讲解，不需要开场白。"""

    try:
        resp = client.chat.completions.create(
            model=DOUBAO_MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=1500,
            temperature=0.7,
        )
        return jsonify({"content": resp.choices[0].message.content})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/stats", methods=["GET"])
def api_stats():
    con = get_db()
    stats = dict(con.execute("SELECT * FROM user_stats WHERE id=1").fetchone())
    today = __import__('datetime').date.today().isoformat()
    # 检查streak
    if stats.get("last_study_date") != today:
        yesterday = (__import__('datetime').date.today() - __import__('datetime').timedelta(days=1)).isoformat()
        if stats.get("last_study_date") == yesterday:
            pass  # streak continues
        elif stats.get("last_study_date") and stats.get("last_study_date") != today:
            con.execute("UPDATE user_stats SET streak_days=0, today_xp=0 WHERE id=1")
            con.commit()
            stats["streak_days"] = 0
            stats["today_xp"] = 0
    mastery_rows = con.execute("SELECT * FROM mastery").fetchall()
    con.close()
    mastery = {r["lesson_id"]: dict(r) for r in mastery_rows}
    return jsonify({"stats": stats, "mastery": mastery, "today": today})

@app.route("/api/stats/xp", methods=["POST"])
def api_add_xp():
    data = request.json
    xp = data.get("xp", 0)
    lesson_id = data.get("lesson_id", "")
    stars = data.get("stars", 0)
    score = data.get("score", 0)
    today = __import__('datetime').date.today().isoformat()
    con = get_db()
    row = dict(con.execute("SELECT * FROM user_stats WHERE id=1").fetchone())
    yesterday = (__import__('datetime').date.today() - __import__('datetime').timedelta(days=1)).isoformat()
    if row["last_study_date"] == today:
        new_streak = row["streak_days"]
        new_today_xp = row["today_xp"] + xp
    elif row["last_study_date"] == yesterday:
        new_streak = row["streak_days"] + 1
        new_today_xp = xp
    else:
        new_streak = 1
        new_today_xp = xp
    new_total = row["total_xp"] + xp
    con.execute("""UPDATE user_stats SET total_xp=?, today_xp=?, streak_days=?, last_study_date=? WHERE id=1""",
                (new_total, new_today_xp, new_streak, today))
    if lesson_id:
        now = __import__('datetime').datetime.now().isoformat()
        import datetime
        next_review_days = {1: 1, 2: 3, 3: 7}.get(stars, 1)
        next_review = (datetime.date.today() + datetime.timedelta(days=next_review_days)).isoformat()
        con.execute("""
            INSERT INTO mastery(lesson_id, stars, best_score, attempts, last_practiced, next_review)
            VALUES(?,?,?,1,?,?)
            ON CONFLICT(lesson_id) DO UPDATE SET
              stars=MAX(stars, excluded.stars),
              best_score=MAX(best_score, excluded.best_score),
              attempts=attempts+1,
              last_practiced=excluded.last_practiced,
              next_review=CASE WHEN excluded.stars >= stars THEN excluded.next_review ELSE next_review END
        """, (lesson_id, stars, score, now, next_review))
    con.commit()
    con.close()
    return jsonify({"ok": True, "total_xp": new_total, "streak": new_streak})

@app.route("/api/daily_plan", methods=["GET"])
def api_daily_plan():
    """每日学习计划：推荐新课+需要复习的课"""
    import datetime
    today = datetime.date.today().isoformat()
    con = get_db()
    mastery_rows = {r["lesson_id"]: dict(r) for r in con.execute("SELECT * FROM mastery").fetchall()}
    con.close()
    new_lessons, review_lessons = [], []
    for subj_key, subj in CURRICULUM.items():
        for unit in subj["units"]:
            for lesson in unit["lessons"]:
                lid = lesson["id"]
                m = mastery_rows.get(lid)
                if not m:
                    new_lessons.append({"lesson_id": lid, "name": lesson["name"],
                                        "subject": subj_key, "subject_name": subj["name"], "unit": unit["name"]})
                elif m.get("next_review") and m["next_review"] <= today and m["stars"] < 3:
                    review_lessons.append({"lesson_id": lid, "name": lesson["name"],
                                           "subject": subj_key, "subject_name": subj["name"],
                                           "stars": m["stars"], "unit": unit["name"]})
    return jsonify({
        "new": new_lessons[:5],
        "review": review_lessons[:5],
        "total_new": len(new_lessons),
        "total_review": len(review_lessons)
    })

@app.route("/api/config", methods=["GET"])
def api_config():
    key = DOUBAO_API_KEY or request.headers.get("X-API-Key", "")
    return jsonify({
        "api_configured": bool(key),
        "model": DOUBAO_MODEL,
    })

@app.route("/api/config", methods=["POST"])
def api_set_config():
    # API Key和模型已硬编码，前端无需覆盖
    return jsonify({"ok": True})

@app.route("/api/tts", methods=["POST"])
def api_tts():
    """用edge-tts生成云溪语音，返回mp3音频流"""
    import edge_tts
    data = request.json
    text = (data.get("text") or "").strip()
    if not text:
        return jsonify({"error": "text为空"}), 400
    if len(text) > 3000:
        text = text[:3000]

    # 接收前端指定voice，或自动检测
    voice_param = (data.get("voice") or "").strip()

    # 去掉markdown符号、书名号和下划线
    text = re.sub(r'###?\s*', '', text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*(.+?)\*', r'\1', text)
    text = re.sub(r'`(.+?)`', r'\1', text)
    text = re.sub(r'_+', '，', text)   # 下划线（填空横线）替换为停顿
    text = text.replace('《', '').replace('》', '')  # 书名号TTS会念"书名号左/右"

    # 自动检测：英文字符占比超50%则用英文voice
    en_chars = len(re.findall(r'[a-zA-Z]', text))
    total_chars = max(len(re.sub(r'\s', '', text)), 1)
    if voice_param:
        voice = voice_param
    elif en_chars / total_chars > 0.5:
        voice = "en-US-GuyNeural"
    else:
        voice = "zh-CN-YunxiNeural"

    # 缓存：相同文本+voice不重复生成
    cache_key = hashlib.md5(f"{voice}:{text}".encode()).hexdigest()
    cache_path = os.path.join(BASE_DIR, "data", f"tts_{cache_key}.mp3")

    if not os.path.exists(cache_path):
        async def _gen():
            c = edge_tts.Communicate(text, voice=voice, rate="-5%")
            await c.save(cache_path)
        asyncio.run(_gen())

    return send_file(cache_path, mimetype="audio/mpeg", as_attachment=False)


# ─── 知识点追踪 & 隐藏分数系统 ───────────────────────────────────

def get_all_kps(subject_key):
    """返回该科目所有知识点列表"""
    kps = []
    for unit in CURRICULUM[subject_key]["units"]:
        for lesson in unit["lessons"]:
            for kp in lesson.get("key_points", []):
                kps.append({
                    "kp": kp,
                    "lesson_id": lesson["id"],
                    "lesson_name": lesson["name"],
                    "unit": unit["name"]
                })
    return kps

@app.route("/api/kp_log", methods=["POST"])
def api_kp_log():
    """记录每道题的知识点答题情况"""
    data = request.json
    entries = data.get("entries", [])
    con = get_db()
    for e in entries:
        con.execute(
            "INSERT INTO kp_log(subject, lesson_id, kp, is_correct) VALUES(?,?,?,?)",
            (e["subject"], e["lesson_id"], e["kp"], 1 if e["is_correct"] else 0)
        )
    con.commit()
    con.close()
    return jsonify({"ok": True})

@app.route("/api/exam_readiness")
def api_exam_readiness():
    """计算三科考试预测分 + 薄弱知识点排名（遗忘曲线加权）"""
    con = get_db()
    logs = con.execute("""
        SELECT subject, kp, is_correct,
               CAST((julianday('now','localtime') - julianday(created_at)) AS REAL) as days_ago
        FROM kp_log
    """).fetchall()
    con.close()

    # 按科目+知识点分组
    kp_data = defaultdict(lambda: defaultdict(list))
    for row in logs:
        kp_data[row["subject"]][row["kp"]].append((row["is_correct"], max(0.0, row["days_ago"])))

    result = {}
    for subj_key in ["yuwen", "shuxue", "yingyu"]:
        all_kps = get_all_kps(subj_key)
        kp_scores = []
        weak_kps = []

        for kp_info in all_kps:
            kp = kp_info["kp"]
            rows = kp_data[subj_key].get(kp, [])
            if not rows:
                mastery_score = -1  # 从未练习
                attempts = 0
            else:
                total_w = 0.0
                correct_w = 0.0
                for is_correct, days_ago in rows:
                    w = math.exp(-days_ago / 14.0)  # 14天半衰期遗忘曲线
                    total_w += w
                    correct_w += w * is_correct
                mastery_score = round(correct_w / total_w * 100, 1) if total_w > 0 else 0.0
                attempts = len(rows)

            kp_scores.append(mastery_score)
            weak_kps.append({
                "kp": kp,
                "lesson_id": kp_info["lesson_id"],
                "lesson_name": kp_info["lesson_name"],
                "unit": kp_info["unit"],
                "mastery": mastery_score,
                "attempts": attempts
            })

        # 预测分：0%掌握→50分，100%掌握→100分
        numeric_scores = [s if s >= 0 else 0 for s in kp_scores]
        avg_mastery = sum(numeric_scores) / len(numeric_scores) if numeric_scores else 0
        predicted = round(50 + avg_mastery * 0.5)

        # 弱点排序：从未练过的优先，再按掌握度升序
        weak_kps_sorted = sorted(
            weak_kps,
            key=lambda x: (1 if x["attempts"] > 0 else 0, x["mastery"] if x["mastery"] >= 0 else -1)
        )

        result[subj_key] = {
            "predicted_score": predicted,
            "avg_mastery": round(avg_mastery, 1),
            "total_kps": len(all_kps),
            "practiced_kps": sum(1 for s in kp_scores if s >= 0),
            "weak_kps": weak_kps_sorted[:12]
        }

    return jsonify(result)

@app.route("/api/targeted_quiz", methods=["POST"])
def api_targeted_quiz():
    """针对薄弱知识点生成专项练习题"""
    data = request.json
    subject_key = data.get("subject", "yuwen")
    kps = data.get("kps", [])
    count = min(data.get("count", 6), 8)

    client, err = _get_client()
    if err:
        return jsonify({"error": err}), 400

    subject = CURRICULUM[subject_key]
    kp_str = "\n".join(f"- {kp}" for kp in kps[:6])

    prompt = f"""出{count}道小学五年级{subject['name']}选择题，专项练习以下薄弱知识点（每点至少1题）：
{kp_str}
重要规则：
1. 只出纯文字题——选项必须是完整的文字描述，绝对不能为空
2. 不出需要展示图形才能作答的题；改出考查概念/定义/规则/计算/特征的文字题
3. 每题4个不同的文字选项A/B/C/D，answer只写1个大写字母，给出explanation，加kp字段注明对应知识点原文
4. JSON格式：{{"questions":[{{"id":1,"type":"choice","kp":"知识点原文","question":"题目","options":{{"A":"文字选项","B":"文字选项","C":"文字选项","D":"文字选项"}},"answer":"A","explanation":"解析"}}]}}"""

    try:
        resp = client.chat.completions.create(
            model=DOUBAO_MODEL,
            messages=[
                {"role": "system", "content": "你是小学出题老师。只输出合法JSON对象，禁止输出任何其他文字。"},
                {"role": "user", "content": prompt}
            ],
            max_tokens=3000,
            temperature=0.5,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content.strip()
        raw = re.sub(r'^```(?:json)?\s*', '', raw, flags=re.MULTILINE)
        raw = re.sub(r'```\s*$', '', raw, flags=re.MULTILINE)
        raw = raw.strip()
        raw = raw.replace('\u201c', '"').replace('\u201d', '"').replace('\u2018', "'").replace('\u2019', "'")
        start, end = raw.find('{'), raw.rfind('}')
        if start != -1 and end > start:
            json_str = raw[start:end+1]
            quiz_data = None
            try:
                quiz_data = json.loads(json_str)
            except json.JSONDecodeError:
                fixed = _fix_json_quotes(json_str)
                try:
                    quiz_data = json.loads(fixed)
                except:
                    pass
            if quiz_data:
                # 过滤空选项题
                valid_qs = [q for q in quiz_data.get("questions", [])
                            if q.get("options") and all(str(v).strip() for v in q["options"].values())]
                if not valid_qs:
                    return jsonify({"error": "AI生成的题目选项为空，请重试"}), 500
                quiz_data["questions"] = valid_qs
                return jsonify(quiz_data)
        return jsonify({"error": "AI返回格式异常，请重试", "raw": raw[:300]}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == "__main__":
    os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
    init_db()
    print("LinkAI 智能学习系统启动中...")
    print("已加载三科教材：语文 / 数学 / 英语")
    print("访问地址: http://localhost:5055")
    port = int(os.environ.get("PORT", 5055))
    app.run(host="0.0.0.0", port=port, debug=False)
