"""Resend wire contract via injected clients; no ambient credentials or activation."""
from urllib.parse import urlsplit
from uuid import UUID
import ipaddress,socket
from app.services.transactional_email_service import purchasing_resend_request

class Rejected(Exception):pass

class ResendProvider:
 isolated_simulator=False
 def __init__(self,client,download_client,*,controlled_recipients,download_hosts):
  if not controlled_recipients:raise PermissionError('Explicit controlled test recipients required; live vendor email is unauthorized')
  self.client=client;self.download_client=download_client;self.recipients=set(controlled_recipients);self.download_hosts=set(download_hosts)
 def send(self,did,payload):
  if set(payload['to'])-self.recipients:raise PermissionError('Recipient is outside explicit isolated provider-test allowlist')
  result=purchasing_resend_request(self.client,'POST','/emails',payload,idempotency_key='purchasing/'+did)
  if result['status'] in (400,401,403,404,413,422):raise Rejected('Resend explicitly rejected submission; no provider acceptance recorded')
  if result['status']>=300:raise ConnectionError('Resend submission outcome requires reconciliation')
  pid=result['data'].get('id');UUID(pid);return pid
 def sent(self,pid):
  UUID(pid);r=purchasing_resend_request(self.client,'GET','/emails/'+pid)
  if r['status']!=200:raise ConnectionError('Sent-message evidence unavailable')
  return r['data']
 def received(self,pid):
  UUID(pid);r=purchasing_resend_request(self.client,'GET','/emails/receiving/'+pid)
  if r['status']!=200:raise ConnectionError('Original received message unavailable')
  return r['data']
 def attachment(self,pid,aid):
  UUID(pid);UUID(aid);r=purchasing_resend_request(self.client,'GET','/emails/receiving/'+pid+'/attachments/'+aid)
  if r['status']!=200:raise ConnectionError('Original attachment unavailable')
  return r['data']
 def download(self,url,limit):
  u=urlsplit(url)
  if u.scheme!='https' or u.hostname not in self.download_hosts or u.port not in (None,443) or u.username or u.password:raise PermissionError('Unqualified Resend download host; no redirect or arbitrary URL fetch')
  for info in socket.getaddrinfo(u.hostname,443,type=socket.SOCK_STREAM):
   if not ipaddress.ip_address(info[4][0]).is_global:raise PermissionError('Private download endpoint is prohibited')
  with self.download_client.stream('GET',url,follow_redirects=False) as r:
   if r.status_code!=200:raise ConnectionError('Private provider original unavailable')
   chunks=[];size=0
   for chunk in r.iter_bytes():
    size+=len(chunk)
    if size>limit:raise ValueError('Provider original exceeds bounded retention capacity; exception retained')
    chunks.append(chunk)
  return b''.join(chunks)
