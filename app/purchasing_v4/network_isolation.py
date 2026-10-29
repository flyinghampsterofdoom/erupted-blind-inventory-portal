"""Process-local qualification boundary; never installed by production entrypoints."""
import ipaddress,sys

_installed=False

def install():
    global _installed
    if _installed:return
    def loopback(host):
        if host in (None,'localhost'):return True
        try:return ipaddress.ip_address(host).is_loopback
        except ValueError:return False
    def guard(event,args):
        if event=='socket.getaddrinfo' and not loopback(args[0]):raise PermissionError('Isolated qualification cannot resolve external endpoints')
        if event=='socket.connect':
            address=args[1]
            if isinstance(address,tuple) and not loopback(address[0]):raise PermissionError('Isolated qualification cannot contact external endpoints')
    sys.addaudithook(guard);_installed=True
