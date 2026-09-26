"""HTTP 边界冒烟测试：角色头、幂等键、错误码映射与值班台。"""
import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

from autumn_poison import domain
from autumn_poison.api import Handler
from autumn_poison.service import DomainStore


def _request(server, method, path, body=None, role=None, actor="",
             request_key=None):
    url = "http://127.0.0.1:%d%s" % (server.server_address[1], path)
    headers = {"Content-Type": "application/json"}
    if role is not None:
        headers["X-Actor-Role"] = role
        headers["X-Actor-Id"] = actor or role
    if request_key:
        headers["X-Request-Key"] = request_key
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


class HttpTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = DomainStore()
        bound = type("BoundHandler", (Handler,), {"store": cls.store})
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), bound)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.store.close()

    def test_full_flow_over_http(self):
        report = {
            "sample_type": domain.SAMPLE_WILD_MUSHROOM,
            "source": domain.SOURCE_SELF_PICKED,
            "route": domain.ROUTE_INGESTION,
            "symptoms": [{"code": "vomiting"}],
            "reporter": {"name": "王五", "contact": "13600000000"},
            "dedupe": {"date": "2026-09-26", "patient": "王五"},
        }
        # 幂等键重放：两次 POST 返回同一事件且只建一份
        status, first = _request(self.server, "POST", "/incidents", report,
                                 role="public", request_key="k-1")
        self.assertEqual(status, 200)
        status, replay = _request(self.server, "POST", "/incidents", report,
                                  role="public", request_key="k-1")
        self.assertEqual(replay["incident_id"], first["incident_id"])
        self.assertEqual(replay["report_id"], first["report_id"])

        # 匿名查询看不到身份信息
        status, pub = _request(self.server, "GET",
                               "/incidents/%s" % first["incident_id"],
                               role="public")
        self.assertNotIn("reporter_name", pub["reports"][0])

        # 医护补充高危症状 -> 自动升级
        status, esc = _request(
            self.server, "POST",
            "/incidents/%s/symptoms" % first["incident_id"],
            {"code": "jaundice"}, role="medical", actor="doc-li")
        self.assertEqual(status, 200)
        self.assertEqual(esc["risk_level"], "high")

        # 医疗交接
        status, ho = _request(
            self.server, "POST",
            "/incidents/%s/handoffs" % first["incident_id"],
            {"facility": "市一院", "staff": "李医生"}, role="medical")
        self.assertEqual(ho["status"], "handoff")

        # 值班台有待办；公众无权访问
        status, board = _request(self.server, "GET", "/follow-ups",
                                 role="dispatcher")
        self.assertEqual(status, 200)
        self.assertTrue(any(g["incident_id"] == first["incident_id"]
                            for g in board["groups"]))
        status, denied = _request(self.server, "GET", "/follow-ups",
                                  role="public")
        self.assertEqual(status, 403)

        # 建议查询按途径区分
        status, advice = _request(
            self.server, "GET",
            "/advice?sample=lycoris_bulb&route=eye_contact", role="public")
        self.assertEqual(status, 200)
        self.assertIn("冲洗", advice["advice"])

        # 合并后回滚
        status, other = _request(
            self.server, "POST", "/incidents",
            {"sample_type": domain.SAMPLE_GINKGO,
             "route": domain.ROUTE_INGESTION}, role="public")
        status, merge = _request(
            self.server, "POST", "/merges",
            {"survivor_id": first["incident_id"],
             "absorbed_id": other["incident_id"], "reason": "误判同事件"},
            role="dispatcher")
        self.assertEqual(status, 200)
        status, undo = _request(
            self.server, "POST",
            "/merges/%s/undo" % merge["merge_id"], {}, role="dispatcher")
        self.assertEqual(status, 200)
        self.assertTrue(undo["undone"])

    def test_old_records_route_still_works(self):
        status, rec = _request(self.server, "GET", "/records/missing",
                               role="dispatcher")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
