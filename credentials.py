"""正式发行凭据：同一 SQLite 事务封存不可修改的发行凭据，查阅时复核。

凭据只增不改：

- 封存（issue_credential）：在 ``BEGIN IMMEDIATE`` 事务内一次性读取访谈
  时长、说话人、发言区间与授权，按既有规则（evaluator.evaluate_timeline）
  校验整条时间轴；仅当不存在不可发布区间时，才把完整输入快照与校验结果
  写入 credentials 表并提交，否则回滚，不留任何记录。
- 查阅（describe_credential / list_credentials）：开启只读事务，在一致
  的读取快照中重新读取现有资料，按发行时的发布日期与媒介重新校验。
  时长 / 说话人 / 发言 / 授权任一变化、或当前仍存在阻断区间，状态为
  pending_review（待复核）；原访谈已删除则为 source_missing（来源缺失），
  历史凭据本身仍可查看。

事务编排与 HTTP 层分离：本模块只依赖 sqlite3 连接（行需为 sqlite3.Row），
不依赖 Flask，可独立测试。
"""
import json

from evaluator import evaluate_timeline

FETCH_INTERVIEW = "SELECT * FROM interviews WHERE id = ?"
FETCH_SPEAKERS = "SELECT * FROM speakers WHERE interview_id = ? ORDER BY id"
FETCH_SEGMENTS = (
    "SELECT * FROM segments WHERE interview_id = ? ORDER BY id")
FETCH_LICENSES = (
    "SELECT l.* FROM licenses l JOIN speakers s ON s.id = l.speaker_id"
    " WHERE s.interview_id = ? ORDER BY l.id")

# 复核状态：一致 / 待复核 / 来源缺失
STATUS_LABELS = {
    "current": "一致",
    "pending_review": "待复核",
    "source_missing": "来源缺失",
}


def _license_dict(row):
    return {
        "id": row["id"],
        "speaker_id": row["speaker_id"],
        "start_seconds": row["start_seconds"],
        "end_seconds": row["end_seconds"],
        "allow_media": json.loads(row["allow_media"]),
        "deny_media": json.loads(row["deny_media"]),
        "embargo_until": row["embargo_until"],
        "note": row["note"],
    }


def _summary(duration, intervals):
    blocked = sum(i["end_seconds"] - i["start_seconds"]
                  for i in intervals if i["status"] == "blocked")
    return {
        "duration_seconds": duration,
        "blocked_seconds": blocked,
        "publishable_seconds": duration - blocked,
    }


def _materialize(interview, speakers, segments, licenses):
    """把数据库行整理为规范化、可直接比较 / 序列化的输入快照。"""
    return {
        "interview_id": interview["id"],
        "title": interview["title"],
        "duration_seconds": interview["duration_seconds"],
        "speakers": [{"id": s["id"], "name": s["name"]} for s in speakers],
        "segments": [{"id": s["id"], "speaker_id": s["speaker_id"],
                      "start_seconds": s["start_seconds"],
                      "end_seconds": s["end_seconds"]} for s in segments],
        "licenses": [_license_dict(l) for l in licenses],
    }


def read_source(conn, interview_id):
    """在当前事务的一致快照中读取访谈全部资料，访谈不存在返回 None。"""
    interview = conn.execute(FETCH_INTERVIEW, (interview_id,)).fetchone()
    if interview is None:
        return None
    speakers = conn.execute(FETCH_SPEAKERS, (interview_id,)).fetchall()
    segments = conn.execute(FETCH_SEGMENTS, (interview_id,)).fetchall()
    licenses = conn.execute(FETCH_LICENSES, (interview_id,)).fetchall()
    return _materialize(interview, speakers, segments, licenses)


def evaluate_source(source, publication_date, medium):
    """按既有规则校验一份输入快照，返回 {intervals, summary}。"""
    intervals = evaluate_timeline(
        duration=source["duration_seconds"],
        speaker_names={s["id"]: s["name"] for s in source["speakers"]},
        segments=source["segments"],
        licenses=source["licenses"],
        publication_date=publication_date,
        medium=medium,
    )
    return {"intervals": intervals,
            "summary": _summary(source["duration_seconds"], intervals)}


# ---------------------------------------------------------------- 封存

def issue_credential(conn, interview_id, publication_date, medium):
    """在同一事务内读取资料并校验，整条时间轴可发布才封存凭据。

    返回 (result, status)：
        201 —— 凭据已封存（result 为凭据详情，含发行时结论与即时复核）；
        404 —— 访谈不存在；
        409 —— 存在不可发布区间，已回滚不留任何记录
               （result 含当前区间与 blocked 阻断列表）。
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        # 1. 同一事务内读取时长、说话人、发言与授权
        source = read_source(conn, interview_id)
        if source is None:
            conn.rollback()
            return {"error": "访谈不存在"}, 404
        # 2. 按现有规则校验整条时间轴
        evaluation = evaluate_source(source, publication_date, medium)
        blocked = [i for i in evaluation["intervals"]
                   if i["status"] == "blocked"]
        # 3. 存在阻断区间 -> 回滚，失败不留记录
        if blocked:
            conn.rollback()
            return {
                "error": (
                    f"整条时间轴存在 {len(blocked)} 个不可发布区间，"
                    "未保存发行凭据"),
                "interview_id": interview_id,
                "medium": medium,
                "publication_date": publication_date,
                "intervals": evaluation["intervals"],
                "summary": evaluation["summary"],
                "blocked": blocked,
            }, 409
        # 4. 全部可发布 -> 封存完整输入与校验结果，凭据不可修改
        cur = conn.execute(
            "INSERT INTO credentials (interview_id, interview_title,"
            " publication_date, medium, input_snapshot, evaluation_result)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (interview_id, source["title"], publication_date, medium,
             json.dumps(source), json.dumps(evaluation)))
        cred_id = cur.lastrowid
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    row = conn.execute(
        "SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
    return describe_credential(conn, row), 201


# ---------------------------------------------------------------- 查阅复核

def _recheck(conn, interview_id, sealed_input, publication_date, medium):
    """在一致的读取快照中比较现有资料并重新校验。

    返回 {"status", "status_label", "changes", "current", "blocked"}：
      - current：现有资料不存在（原访谈已删除）时为 None，状态 source_missing；
      - 时长 / 说话人 / 发言 / 授权任一变化，或当前存在阻断区间，
        状态 pending_review（即使仍全部可发布也列出阻断区间）；
      - 否则 current。
    """
    conn.execute("BEGIN")
    try:
        source = read_source(conn, interview_id)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    if source is None:
        return {
            "status": "source_missing",
            "status_label": STATUS_LABELS["source_missing"],
            "changes": {"duration": None, "speakers": None,
                        "segments": None, "licenses": None},
            "current": None,
            "blocked": [],
        }

    changes = {
        "duration":
            source["duration_seconds"] != sealed_input["duration_seconds"],
        "speakers": source["speakers"] != sealed_input["speakers"],
        "segments": source["segments"] != sealed_input["segments"],
        "licenses": source["licenses"] != sealed_input["licenses"],
    }
    evaluation = evaluate_source(source, publication_date, medium)
    blocked = [i for i in evaluation["intervals"]
               if i["status"] == "blocked"]
    status = "current" if not any(changes.values()) and not blocked \
        else "pending_review"
    return {
        "status": status,
        "status_label": STATUS_LABELS[status],
        "changes": changes,
        "current": {
            **source,
            "medium": medium,
            "publication_date": publication_date,
            "intervals": evaluation["intervals"],
            "summary": evaluation["summary"],
        },
        "blocked": blocked,
    }


def describe_credential(conn, row):
    """组装一条凭据的查阅视图：封存内容（不可变）+ 当前复核结论。"""
    sealed_input = json.loads(row["input_snapshot"])
    sealed_evaluation = json.loads(row["evaluation_result"])
    return {
        "id": row["id"],
        "interview_id": row["interview_id"],
        "interview_title": row["interview_title"],
        "publication_date": row["publication_date"],
        "medium": row["medium"],
        "sealed_at": row["sealed_at"],
        "sealed": {"input": sealed_input, "evaluation": sealed_evaluation},
        "recheck": _recheck(
            conn, row["interview_id"], sealed_input,
            row["publication_date"], row["medium"]),
    }


def list_credentials(conn):
    """列出全部凭据（新封存在前），逐条给出当前复核状态。"""
    rows = conn.execute(
        "SELECT * FROM credentials ORDER BY id DESC").fetchall()
    return [describe_credential(conn, row) for row in rows]
