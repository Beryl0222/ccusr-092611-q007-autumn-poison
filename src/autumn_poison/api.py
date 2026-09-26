"""秋季误采处置协同的轻量 HTTP 边界。"""
import json
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from .service import DomainStore,ServiceError
from .incident_service import IncidentService
class Handler(BaseHTTPRequestHandler):
 store=DomainStore()
 def _reply(self,code,body):
  data=json.dumps(body).encode();self.send_response(code);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
 def do_GET(self):
  try:self._reply(200,self.store.get(self.path.rsplit("/",1)[-1]).__dict__)
  except ServiceError as exc:self._reply(404,{"error":str(exc)})
 def log_message(self,*_):return
def make_incident_handler(service):
 """中毒事件接口：角色经 X-Actor-Id / X-Actor-Role 头传入。"""
 class IncidentHandler(BaseHTTPRequestHandler):
  def _reply(self,code,body):
   data=json.dumps(body,ensure_ascii=False).encode();self.send_response(code);self.send_header("Content-Type","application/json");self.send_header("Content-Length",str(len(data)));self.end_headers();self.wfile.write(data)
  def _body(self):
   length=int(self.headers.get("Content-Length") or 0)
   return json.loads(self.rfile.read(length) or b"{}")
  def _actor(self):return self.headers.get("X-Actor-Id","anonymous"),self.headers.get("X-Actor-Role","public")
  def do_POST(self):
   actor,role=self._actor();parts=[p for p in self.path.split("/") if p]
   try:
    body=self._body();key=body.get("request_key")
    if parts==["incidents"]:
     self._reply(201,service.file_report(body.get("channel","hotline"),body["sample_type"],body["exposure_route"],body.get("narrative",""),body.get("reporter_name"),body.get("reporter_phone"),actor,role,key))
    elif len(parts)==3 and parts[0]=="incidents" and parts[2]=="symptoms":
     self._reply(200,service.add_symptom(parts[1],body["observed_at"],body["description"],body.get("improving",False),actor,role,key))
    elif len(parts)==3 and parts[0]=="incidents" and parts[2]=="handoffs":
     self._reply(200,service.record_handoff(parts[1],body["from_party"],body["to_party"],body.get("note",""),actor,role,key))
    elif len(parts)==3 and parts[0]=="incidents" and parts[2]=="advice":
     self._reply(200,service.record_advice(parts[1],body["advice"],actor,role,key))
    elif len(parts)==3 and parts[0]=="incidents" and parts[2]=="merge":
     self._reply(200,service.merge_incidents(parts[1],body["secondary_no"],actor,body.get("reason",""),role,key))
    elif len(parts)==3 and parts[0]=="merges" and parts[2]=="rollback":
     self._reply(200,service.rollback_merge(parts[1],actor,body.get("reason",""),role))
    elif len(parts)==3 and parts[0]=="followups" and parts[2]=="complete":
     self._reply(200,service.complete_followup(parts[1],actor,role))
    else:self._reply(404,{"error":"未知路径"})
   except ServiceError as exc:self._reply(400,{"error":str(exc)})
   except KeyError as exc:self._reply(400,{"error":"缺少字段 %s"%exc})
  def do_GET(self):
   _,role=self._actor();parts=[p for p in self.path.split("/") if p]
   try:
    if len(parts)==2 and parts[0]=="incidents":self._reply(200,service.get_incident(parts[1],role))
    elif len(parts)==3 and parts[0]=="incidents" and parts[2]=="briefing":self._reply(200,service.briefing(parts[1]))
    else:self._reply(404,{"error":"未知路径"})
   except ServiceError as exc:self._reply(404,{"error":str(exc)})
  def log_message(self,*_):return
 return IncidentHandler
def serve(host="127.0.0.1",port=8080,database=":memory:"):ThreadingHTTPServer((host,port),make_incident_handler(IncidentService(database))).serve_forever()
