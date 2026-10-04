"""正式发行凭据事务编排（credentials.py）的单元测试。

不依赖 Flask：直接对临时 SQLite 数据库验证
同一事务封存、失败不留记录、凭据不可修改、查阅时一致快照复核、
任一字段变化即待复核（仍可发布也列出阻断区间）、
原访谈删除后来源缺失但历史凭据仍可查看。
"""
import json
import os
import sqlite3
import tempfile
import unittest

from credentials import (
    STATUS_LABELS, describe_credential, issue_credential, list_credentials,
)

# 与 app.py 中 SCHEMA 保持一致（测试夹具，独立维护）
SCHEMA = """
CREATE TABLE interviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0)
);
CREATE TABLE speakers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    name TEXT NOT NULL
);
CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL REFERENCES interviews(id) ON DELETE CASCADE,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE licenses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    speaker_id INTEGER NOT NULL REFERENCES speakers(id) ON DELETE CASCADE,
    start_seconds INTEGER NOT NULL,
    end_seconds INTEGER NOT NULL,
    allow_media TEXT NOT NULL DEFAULT '[]',
    deny_media TEXT NOT NULL DEFAULT '[]',
    embargo_until TEXT,
    note TEXT NOT NULL DEFAULT '',
    CHECK (start_seconds >= 0 AND end_seconds > start_seconds)
);
CREATE TABLE credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interview_id INTEGER NOT NULL,
    interview_title TEXT NOT NULL,
    publication_date TEXT NOT NULL,
    medium TEXT NOT NULL CHECK (medium IN ('audio', 'text')),
    input_snapshot TEXT NOT NULL,
    evaluation_result TEXT NOT NULL,
    sealed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
"""

PUB_DATE = "2026-10-04"


class CredentialTest(unittest.TestCase):
    """夹具：访谈 600 秒；甲发言 0-600，乙发言 300-600；
    甲已有音频授权 0-300（未补件前 300-600 不可发布）。"""

    def setUp(self):
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = sqlite3.connect(self.db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        c = self.conn
        c.execute("INSERT INTO interviews (title, duration_seconds)"
                  " VALUES ('访谈', 600)")
        c.execute("INSERT INTO speakers (interview_id, name) VALUES (1, '甲')")
        c.execute("INSERT INTO speakers (interview_id, name) VALUES (1, '乙')")
        c.execute("INSERT INTO segments (interview_id, speaker_id,"
                  " start_seconds, end_seconds) VALUES (1, 1, 0, 600)")
        c.execute("INSERT INTO segments (interview_id, speaker_id,"
                  " start_seconds, end_seconds) VALUES (1, 2, 300, 600)")
        c.execute("INSERT INTO licenses (speaker_id, start_seconds,"
                  " end_seconds, allow_media, deny_media, embargo_until,"
                  " note) VALUES (1, 0, 300, '[\"audio\"]', '[]', NULL, '')")
        c.commit()

    def tearDown(self):
        self.conn.close()
        os.unlink(self.db_path)

    def issue(self, interview_id=1, pub_date=PUB_DATE, medium="audio"):
        return issue_credential(
            self.conn, interview_id=interview_id,
            publication_date=pub_date, medium=medium)

    def credential_count(self):
        return self.conn.execute(
            "SELECT COUNT(*) AS n FROM credentials").fetchone()["n"]

    def add_full_licenses(self):
        """补齐 300-600 甲乙音频授权，使整条时间轴可发布。"""
        c = self.conn
        for sp in (1, 2):
            c.execute("INSERT INTO licenses (speaker_id, start_seconds,"
                      " end_seconds, allow_media, deny_media, embargo_until,"
                      " note) VALUES (?, 300, 600, '[\"audio\"]', '[]',"
                      " NULL, '')", (sp,))
        c.commit()

    def get_cred(self, cred_id):
        row = self.conn.execute(
            "SELECT * FROM credentials WHERE id = ?", (cred_id,)).fetchone()
        return describe_credential(self.conn, row)

    # ---------------------------------------------------------- 封存

    def test_blocked_timeline_does_not_seal(self):
        result, status = self.issue()
        self.assertEqual(status, 409)
        self.assertIn("error", result)
        self.assertEqual(self.credential_count(), 0)  # 失败不留记录
        # 返回当前阻断区间及原因
        self.assertEqual(len(result["blocked"]), 1)
        self.assertEqual((result["blocked"][0]["start_seconds"],
                          result["blocked"][0]["end_seconds"]), (300, 600))

    def test_seal_success_stores_snapshot_and_evaluation(self):
        self.add_full_licenses()
        result, status = self.issue()
        self.assertEqual(status, 201)
        self.assertEqual(self.credential_count(), 1)
        self.assertEqual(result["interview_id"], 1)
        self.assertEqual(result["publication_date"], PUB_DATE)
        self.assertEqual(result["medium"], "audio")
        # 封存的完整输入
        sealed_in = result["sealed"]["input"]
        self.assertEqual(sealed_in["duration_seconds"], 600)
        self.assertEqual([s["name"] for s in sealed_in["speakers"]],
                         ["甲", "乙"])
        self.assertEqual(len(sealed_in["segments"]), 2)
        self.assertEqual(len(sealed_in["licenses"]), 3)
        # 封存的校验结果：整条时间轴可发布
        sealed_eval = result["sealed"]["evaluation"]
        self.assertEqual(sealed_eval["summary"]["blocked_seconds"], 0)
        self.assertTrue(all(i["status"] != "blocked"
                            for i in sealed_eval["intervals"]))
        # 即时复核为一致
        self.assertEqual(result["recheck"]["status"], "current")

    def test_interview_not_found(self):
        result, status = self.issue(interview_id=999)
        self.assertEqual(status, 404)
        self.assertEqual(self.credential_count(), 0)

    def test_sealed_rows_are_complete_json(self):
        self.add_full_licenses()
        result, status = self.issue()
        self.assertEqual(status, 201)
        row = self.conn.execute(
            "SELECT * FROM credentials WHERE id = ?",
            (result["id"],)).fetchone()
        snap = json.loads(row["input_snapshot"])
        self.assertEqual(snap["title"], "访谈")
        self.assertEqual(snap["licenses"][0]["allow_media"], ["audio"])
        evaluation = json.loads(row["evaluation_result"])
        self.assertIn("intervals", evaluation)
        self.assertIn("summary", evaluation)

    # ---------------------------------------------------------- 复核

    def test_recheck_current_when_unchanged(self):
        self.add_full_licenses()
        result, _ = self.issue()
        cred = self.get_cred(result["id"])
        self.assertEqual(cred["recheck"]["status"], "current")
        self.assertEqual(cred["recheck"]["status_label"],
                         STATUS_LABELS["current"])
        self.assertNotIn(True, cred["recheck"]["changes"].values())
        self.assertEqual(cred["recheck"]["blocked"], [])
        # 发行时结论原样保留
        self.assertEqual(
            cred["sealed"]["evaluation"]["summary"]["blocked_seconds"], 0)

    def test_duration_change_marks_pending_review_even_if_still_publishable(self):
        self.add_full_licenses()
        result, _ = self.issue()
        # 时长延长到 700 秒：延长部分无发言（无需授权，仍可发布），
        # 但时长已变化 -> 待复核
        self.conn.execute(
            "UPDATE interviews SET duration_seconds = 700 WHERE id = 1")
        self.conn.commit()
        cred = self.get_cred(result["id"])
        self.assertEqual(cred["recheck"]["status"], "pending_review")
        self.assertTrue(cred["recheck"]["changes"]["duration"])
        self.assertEqual(cred["recheck"]["blocked"], [])
        # 发行时结论保留
        self.assertEqual(
            cred["sealed"]["input"]["duration_seconds"], 600)

    def test_speaker_change_marks_pending_review(self):
        self.add_full_licenses()
        result, _ = self.issue()
        self.conn.execute("UPDATE speakers SET name = ? WHERE id = 1",
                          ("甲（改名）",))
        self.conn.commit()
        cred = self.get_cred(result["id"])
        self.assertEqual(cred["recheck"]["status"], "pending_review")
        self.assertTrue(cred["recheck"]["changes"]["speakers"])

    def test_segment_change_marks_pending_review(self):
        self.add_full_licenses()
        result, _ = self.issue()
        self.conn.execute(
            "UPDATE segments SET end_seconds = 400 WHERE id = 1")
        self.conn.commit()
        cred = self.get_cred(result["id"])
        self.assertEqual(cred["recheck"]["status"], "pending_review")
        self.assertTrue(cred["recheck"]["changes"]["segments"])

    def test_license_change_marks_pending_review_and_lists_blocked(self):
        self.add_full_licenses()
        result, _ = self.issue()
        # 删除甲 300-600 的授权 -> 300-600 再次阻断
        self.conn.execute(
            "DELETE FROM licenses WHERE speaker_id = 1 AND start_seconds = 300")
        self.conn.commit()
        cred = self.get_cred(result["id"])
        self.assertEqual(cred["recheck"]["status"], "pending_review")
        self.assertTrue(cred["recheck"]["changes"]["licenses"])
        # 即便仍可发布也列出当前阻断区间；此处确有阻断
        self.assertEqual(len(cred["recheck"]["blocked"]), 1)
        self.assertEqual(cred["recheck"]["current"]["summary"]
                         ["blocked_seconds"], 300)

    def test_title_change_alone_stays_current(self):
        # 标题不在封存的校验输入比较范围内（快照留存但不影响状态）
        self.add_full_licenses()
        result, _ = self.issue()
        self.conn.execute("UPDATE interviews SET title = ? WHERE id = 1",
                          ("新标题",))
        self.conn.commit()
        cred = self.get_cred(result["id"])
        self.assertEqual(cred["recheck"]["status"], "current")

    def test_deleted_interview_shows_source_missing_but_credential_remains(self):
        self.add_full_licenses()
        result, _ = self.issue()
        cred_id = result["id"]
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("DELETE FROM interviews WHERE id = 1")
        self.conn.commit()
        # 凭据记录仍在（不级联删除）
        self.assertEqual(self.credential_count(), 1)
        cred = self.get_cred(cred_id)
        self.assertEqual(cred["recheck"]["status"], "source_missing")
        self.assertEqual(cred["recheck"]["status_label"],
                         STATUS_LABELS["source_missing"])
        self.assertIsNone(cred["recheck"]["current"])
        self.assertEqual(cred["recheck"]["blocked"], [])
        # 历史凭据内容仍可查看
        self.assertEqual(cred["interview_title"], "访谈")
        self.assertEqual(len(cred["sealed"]["input"]["licenses"]), 3)

    def test_list_credentials_newest_first_with_status(self):
        self.add_full_licenses()
        first, s1 = self.issue()
        self.assertEqual(s1, 201)
        # 第二条：同一访谈按文字媒介封存（现有授权均允许音频 -> 阻断，失败）
        blocked_result, s2 = self.issue(medium="text")
        self.assertEqual(s2, 409)
        self.assertEqual(self.credential_count(), 1)
        # 为文字也补齐授权后再封存一条
        c = self.conn
        c.execute("UPDATE licenses SET allow_media = '[\"audio\",\"text\"]'")
        c.commit()
        second, s3 = self.issue(medium="text")
        self.assertEqual(s3, 201)
        listing = list_credentials(self.conn)
        self.assertEqual([x["id"] for x in listing],
                         [second["id"], first["id"]])
        for item in listing:
            self.assertIn(item["recheck"]["status"],
                          ("current", "pending_review", "source_missing"))

    def test_failed_seal_leaves_no_partial_data(self):
        # 阻断封存前后 licenses / segments 数量不变
        before = {
            t: self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("licenses", "segments", "speakers")}
        result, status = self.issue()
        self.assertEqual(status, 409)
        after = {
            t: self.conn.execute(
                f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in ("licenses", "segments", "speakers")}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
