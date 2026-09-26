"""秋季误采处置协同的轻量 HTTP 边界。

约定：
- 身份经请求头传入：X-Actor-Id（操作人）、X-Actor-Role（public/dispatcher/
  medical/admin）；缺省按 public（匿名公众）处理，敏感字段自动脱敏；
- 写操作可带 X-Request-Key 做幂等去重；
- 错误体 {"error": ...}，状态码：400 参数 / 403 权限 / 404 不存在 / 409 冲突。
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .domain import advice_for
from .service import DomainStore, ServiceError

_STATUS = {"validation": 400, "forbidden": 403, "not_found": 404, "conflict": 409}


class Handler(BaseHTTPRequestHandler):
    store = DomainStore()
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础工具
    def _reply(self, code, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _actor(self):
        return (self.headers.get("X-Actor-Id", ""),
                self.headers.get("X-Actor-Role", "public"))

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ServiceError("请求体不是合法 JSON")

    def _segments(self):
        return [seg for seg in urlparse(self.path).path.split("/") if seg]

    def _query(self):
        return parse_qs(urlparse(self.path).query)

    def _guard(self, func):
        try:
            func()
        except ServiceError as exc:
            self._reply(_STATUS.get(exc.code, 400), {"error": str(exc)})
        except Exception as exc:  # 兜底，避免连接悬挂
            self._reply(500, {"error": "内部错误：%s" % exc})

    # ------------------------------------------------------------ 路由
    def do_GET(self):
        def handle():
            seg = self._segments()
            _, role = self._actor()
            if seg[:1] == ["incidents"] and len(seg) == 2:
                self._reply(200, self.store.incident_view(seg[1], role=role))
            elif seg[:1] == ["follow-ups"] and len(seg) == 1:
                self._reply(200, {"groups": self.store.list_due_follow_ups(role=role)})
            elif seg[:1] == ["advice"]:
                query = self._query()
                self._reply(200, {"advice": advice_for(
                    query.get("sample", ["unknown"])[0],
                    query.get("route", ["unknown"])[0],
                    query.get("source", ["unknown"])[0])})
            elif seg[:1] == ["public"] and len(seg) == 2:
                self._reply(200, self.store.public_status(seg[1]))
            elif seg[:1] == ["records"] and len(seg) == 2:
                self._reply(200, self.store.get(seg[1]).__dict__)
            else:
                self._reply(404, {"error": "路由不存在"})
        self._guard(handle)

    def do_POST(self):
        def handle():
            seg = self._segments()
            actor, role = self._actor()
            request_key = self.headers.get("X-Request-Key")
            body = self._body()
            if seg[:1] == ["incidents"] and len(seg) == 1:
                self._reply(200, self.store.report_incident(
                    body, role=role, actor=actor, request_key=request_key))
            elif seg[:1] == ["incidents"] and len(seg) == 3 and seg[2] == "symptoms":
                if isinstance(body, list):
                    items = body
                else:
                    items = body.get("symptoms") or [body]
                result = {}
                for item in items:
                    result = self.store.add_symptom(
                        seg[1], item, role, actor=actor, request_key=request_key)
                    request_key = None  # 批量时仅首条使用请求键
                self._reply(200, result)
            elif seg[:1] == ["incidents"] and len(seg) == 3 and seg[2] == "handoffs":
                self._reply(200, self.store.record_handoff(
                    seg[1], body, role, actor=actor))
            elif seg[:1] == ["incidents"] and len(seg) == 3 and seg[2] == "actions":
                self._reply(200, self.store.add_action_note(
                    seg[1], body.get("note", ""), body.get("basis", ""),
                    role, actor=actor))
            elif seg[:1] == ["incidents"] and len(seg) == 3 and seg[2] == "downgrade":
                self._reply(200, self.store.downgrade_risk(
                    seg[1], body.get("level", ""), body.get("reason", ""),
                    role, actor=actor))
            elif seg[:1] == ["merges"] and len(seg) == 1:
                self._reply(200, self.store.merge_incidents(
                    body.get("survivor_id", ""), body.get("absorbed_id", ""),
                    role, actor=actor, reason=body.get("reason", "")))
            elif seg[:1] == ["merges"] and len(seg) == 3 and seg[2] == "undo":
                self._reply(200, self.store.undo_merge(seg[1], role, actor=actor))
            elif seg[:1] == ["reports"] and len(seg) == 3 and seg[2] == "relocate":
                self._reply(200, self.store.relocate_report(
                    seg[1], body.get("target_incident_id"), role,
                    actor=actor, reason=body.get("reason", "")))
            elif seg[:1] == ["follow-ups"] and len(seg) == 3 and seg[2] == "complete":
                self._reply(200, self.store.complete_follow_up(
                    int(seg[1]), role, actor=actor, outcome=body.get("outcome", "")))
            elif seg[:1] == ["follow-ups"] and len(seg) == 3 and seg[2] == "cancel":
                self._reply(200, self.store.cancel_follow_up(
                    int(seg[1]), role, actor=actor, reason=body.get("reason", "")))
            else:
                self._reply(404, {"error": "路由不存在"})
        self._guard(handle)

    def log_message(self, *_):
        return


def make_server(store=None, host="127.0.0.1", port=8080):
    handler = type("BoundHandler", (Handler,), {})
    handler.store = store or DomainStore()
    return ThreadingHTTPServer((host, port), handler)


def serve(host="127.0.0.1", port=8080, database="autumn_poison.db"):
    make_server(DomainStore(database), host, port).serve_forever()


if __name__ == "__main__":
    serve()
