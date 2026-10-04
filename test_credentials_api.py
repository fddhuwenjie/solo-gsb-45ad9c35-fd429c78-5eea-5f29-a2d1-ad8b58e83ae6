"""HTTP 层测试：发行凭据接口。

POST   /api/interviews/<id>/credentials  封存凭据
GET    /api/credentials                  凭据列表（含复核状态）
GET    /api/credentials/<id>             凭据详情

需要 Flask（本地无依赖时可跳过；Docker 镜像内可直接运行）：
    python -m unittest test_credentials_api.py -v
"""
import os
import sqlite3
import tempfile
import unittest

_fd, _DB = tempfile.mkstemp(suffix=".db")
os.close(_fd)
os.environ["DB_PATH"] = _DB

from app import app  # noqa: E402

PUB_DATE = "2026-10-04"


class CredentialsApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def setUp(self):
        conn = sqlite3.connect(_DB)
        conn.execute("PRAGMA foreign_keys = ON")
        for table in ("credentials", "licenses", "segments",
                      "speakers", "interviews"):
            conn.execute(f"DELETE FROM {table}")
        conn.commit()
        conn.close()
        c = self.client
        self.iid = c.post("/api/interviews", json={
            "title": "访谈", "duration_seconds": 600}).get_json()["id"]
        self.sp1 = c.post(f"/api/interviews/{self.iid}/speakers",
                          json={"name": "甲"}).get_json()["id"]
        self.sp2 = c.post(f"/api/interviews/{self.iid}/speakers",
                          json={"name": "乙"}).get_json()["id"]
        c.post(f"/api/interviews/{self.iid}/segments", json={
            "speaker_id": self.sp1, "start_seconds": 0,
            "end_seconds": 600})
        c.post(f"/api/interviews/{self.iid}/segments", json={
            "speaker_id": self.sp2, "start_seconds": 300,
            "end_seconds": 600})
        c.post(f"/api/speakers/{self.sp1}/licenses", json={
            "start_seconds": 0, "end_seconds": 300,
            "allow_media": ["audio"], "deny_media": [],
            "embargo_until": None, "note": ""})

    def add_license(self, speaker, start, end, allow=("audio",)):
        return self.client.post(f"/api/speakers/{speaker}/licenses", json={
            "start_seconds": start, "end_seconds": end,
            "allow_media": list(allow), "deny_media": [],
            "embargo_until": None, "note": ""})

    def seal(self, iid=None, medium="audio", pub_date=PUB_DATE):
        return self.client.post(
            f"/api/interviews/{iid or self.iid}/credentials",
            json={"publication_date": pub_date, "medium": medium})

    def credential_count(self):
        return len(self.client.get("/api/credentials").get_json())

    # ---------------------------------------------------------- 封存

    def test_blocked_returns_409_and_saves_nothing(self):
        resp = self.seal()
        self.assertEqual(resp.status_code, 409)
        data = resp.get_json()
        self.assertIn("error", data)
        self.assertEqual(len(data["blocked"]), 1)
        self.assertEqual(self.credential_count(), 0)

    def test_seal_success(self):
        self.add_license(self.sp1, 300, 600)
        self.add_license(self.sp2, 300, 600)
        resp = self.seal()
        self.assertEqual(resp.status_code, 201)
        data = resp.get_json()
        self.assertEqual(data["interview_id"], self.iid)
        self.assertEqual(data["medium"], "audio")
        self.assertEqual(data["publication_date"], PUB_DATE)
        self.assertEqual(data["recheck"]["status"], "current")
        # 封存完整输入与校验结果
        self.assertEqual(
            data["sealed"]["input"]["duration_seconds"], 600)
        self.assertEqual(len(data["sealed"]["input"]["licenses"]), 3)
        self.assertEqual(
            data["sealed"]["evaluation"]["summary"]["blocked_seconds"], 0)
        self.assertIn("sealed_at", data)
        self.assertEqual(self.credential_count(), 1)

    def test_seal_validation_errors(self):
        resp = self.seal(medium="video")
        self.assertEqual(resp.status_code, 400)
        resp = self.seal(pub_date="")
        self.assertEqual(resp.status_code, 400)
        resp = self.seal(pub_date="2026/10/04")
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(self.credential_count(), 0)

    def test_seal_interview_not_found(self):
        resp = self.seal(iid=9999)
        self.assertEqual(resp.status_code, 404)

    # ---------------------------------------------------------- 查阅复核

    def test_list_and_detail_keep_sealed_conclusion(self):
        self.add_license(self.sp1, 300, 600)
        self.add_license(self.sp2, 300, 600)
        cred_id = self.seal().get_json()["id"]
        resp = self.client.get("/api/credentials")
        self.assertEqual(resp.status_code, 200)
        listing = resp.get_json()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["id"], cred_id)
        self.assertEqual(listing[0]["recheck"]["status"], "current")

        detail = self.client.get(f"/api/credentials/{cred_id}").get_json()
        # 发行时结论保留
        self.assertEqual(
            detail["sealed"]["evaluation"]["summary"]["blocked_seconds"], 0)

    def test_detail_not_found(self):
        resp = self.client.get("/api/credentials/9999")
        self.assertEqual(resp.status_code, 404)

    def test_change_marks_pending_review_and_lists_current_blocked(self):
        self.add_license(self.sp1, 300, 600)
        self.add_license(self.sp2, 300, 600)
        cred_id = self.seal().get_json()["id"]
        # 时长变化（延长段无发言，仍全部可发布）-> 待复核，无阻断区间
        self.client.put(f"/api/interviews/{self.iid}", json={
            "title": "访谈", "duration_seconds": 700})
        detail = self.client.get(
            f"/api/credentials/{cred_id}").get_json()
        self.assertEqual(detail["recheck"]["status"], "pending_review")
        self.assertTrue(detail["recheck"]["changes"]["duration"])
        self.assertEqual(detail["recheck"]["blocked"], [])
        # 发行时结论保持为 600 秒全可发布
        self.assertEqual(
            detail["sealed"]["input"]["duration_seconds"], 600)

        # 删除一条授权 -> 待复核且列出当前阻断区间
        licenses = self.client.get(
            f"/api/interviews/{self.iid}").get_json()["licenses"]
        target = [l for l in licenses
                  if l["speaker_id"] == self.sp1
                  and l["start_seconds"] == 300][0]
        self.client.delete(f"/api/licenses/{target['id']}")
        detail = self.client.get(
            f"/api/credentials/{cred_id}").get_json()
        self.assertEqual(detail["recheck"]["status"], "pending_review")
        self.assertTrue(detail["recheck"]["changes"]["licenses"])
        self.assertEqual(len(detail["recheck"]["blocked"]), 1)
        self.assertEqual(detail["recheck"]["current"]["summary"]
                         ["blocked_seconds"], 300)

    def test_deleted_interview_shows_source_missing(self):
        self.add_license(self.sp1, 300, 600)
        self.add_license(self.sp2, 300, 600)
        cred_id = self.seal().get_json()["id"]
        self.client.delete(f"/api/interviews/{self.iid}")
        # 列表仍含历史凭据
        listing = self.client.get("/api/credentials").get_json()
        self.assertEqual(len(listing), 1)
        self.assertEqual(listing[0]["recheck"]["status"], "source_missing")
        # 详情仍可查看
        detail = self.client.get(f"/api/credentials/{cred_id}")
        self.assertEqual(detail.status_code, 200)
        data = detail.get_json()
        self.assertEqual(data["recheck"]["status"], "source_missing")
        self.assertIsNone(data["recheck"]["current"])
        self.assertEqual(data["interview_title"], "访谈")
        self.assertEqual(
            len(data["sealed"]["input"]["licenses"]), 3)

    def test_speaker_change_marks_pending_review(self):
        self.add_license(self.sp1, 300, 600)
        self.add_license(self.sp2, 300, 600)
        cred_id = self.seal().get_json()["id"]
        self.client.put(f"/api/speakers/{self.sp2}", json={"name": "丙"})
        detail = self.client.get(
            f"/api/credentials/{cred_id}").get_json()
        self.assertEqual(detail["recheck"]["status"], "pending_review")
        self.assertTrue(detail["recheck"]["changes"]["speakers"])

    def test_recheck_uses_sealed_publication_parameters(self):
        # 音频与文字均允许，2026-10-04 封存
        self.add_license(self.sp1, 300, 600, allow=("audio", "text"))
        self.add_license(self.sp2, 300, 600, allow=("audio", "text"))
        cred_id = self.seal().get_json()["id"]
        # 即便当前授权含未来解禁的新授权，复核仍按封存参数进行，
        # 这里直接验证复核媒介取自封存记录
        detail = self.client.get(
            f"/api/credentials/{cred_id}").get_json()
        self.assertEqual(detail["recheck"]["current"]["medium"], "audio")
        self.assertEqual(
            detail["recheck"]["current"]["publication_date"], PUB_DATE)


if __name__ == "__main__":
    unittest.main()
