# -*- test-name: twisted.test.test_sip -*-

# Copyright (c) 2001-2009 Twisted Matrix Laboratories.
# See LICENSE for details.


"""Session Initialization Protocol.

Documented in RFC 2543.
[Superceded by 3261]


This module contains a deprecated implementation of HTTP Digest authentication.
See L{twisted.cred.credentials} and L{twisted.cred._digest} for its new home.
"""

# system imports
import socket, time, sys, random, warnings
from zope.interface import implements, Interface, Attribute

# twisted imports
from twisted.python import log, util
from twisted.python.deprecate import deprecated
from twisted.python.versions import Version
from twisted.python.hashlib import md5
from twisted.internet import protocol, defer, reactor

from twisted import cred
import twisted.cred.error
from twisted.cred.credentials import UsernameHashedPassword, UsernamePassword


# sibling imports
from twisted.protocols import basic

PORT = 5060

# SIP headers have short forms
shortHeaders = {"call-id": "i",
                "contact": "m",
                "content-encoding": "e",
                "content-length": "l",
                "content-type": "c",
                "from": "f",
                "subject": "s",
                "to": "t",
                "via": "v",
                }

longHeaders = {}
for k, v in shortHeaders.items():
    longHeaders[v] = k
del k, v

statusCodes = {
    100: "Trying",
    180: "Ringing",
    181: "Call Is Being Forwarded",
    182: "Queued",
    183: "Session Progress",

    200: "OK",

    300: "Multiple Choices",
    301: "Moved Permanently",
    302: "Moved Temporarily",
    303: "See Other",
    305: "Use Proxy",
    380: "Alternative Service",

    400: "Bad Request",
    401: "Unauthorized",
    402: "Payment Required",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    406: "Not Acceptable",
    407: "Proxy Authentication Required",
    408: "Request Timeout",
    409: "Conflict", # Not in RFC3261
    410: "Gone",
    411: "Length Required", # Not in RFC3261
    413: "Request Entity Too Large",
    414: "Request-URI Too Large",
    415: "Unsupported Media Type",
    416: "Unsupported URI Scheme",
    420: "Bad Extension",
    421: "Extension Required",
    423: "Interval Too Brief",
    480: "Temporarily Unavailable",
    481: "Call/Transaction Does Not Exist",
    482: "Loop Detected",
    483: "Too Many Hops",
    484: "Address Incomplete",
    485: "Ambiguous",
    486: "Busy Here",
    487: "Request Terminated",
    488: "Not Acceptable Here",
    491: "Request Pending",
    493: "Undecipherable",
    
    500: "Internal Server Error",
    501: "Not Implemented",
    502: "Bad Gateway", # no donut
    503: "Service Unavailable",
    504: "Server Time-out",
    505: "SIP Version not supported",
    513: "Message Too Large",
    
    600: "Busy Everywhere",
    603: "Decline",
    604: "Does not exist anywhere",
    606: "Not Acceptable",
}

TRYING = 100
OK = 200
RINGING = 180
BAD_REQUEST = 400
UNAUTHORIZED = 401
NOT_FOUND = 404
TIMEOUT = 408
REQUEST_TERMINATED = 487
INTERNAL_ERROR = 500

specialCases = {
    'cseq': 'CSeq',
    'call-id': 'Call-ID',
    'www-authenticate': 'WWW-Authenticate',
}

VIA_COOKIE = "z9hG4bK"

def dashCapitalize(s):
    ''' Capitalize a string, making sure to treat - as a word seperator '''
    return '-'.join([ x.capitalize() for x in s.split('-')])

def unq(s):
    if s[0] == s[-1] == '"':
        return s[1:-1]
    return s

def DigestCalcHA1(
    pszAlg,
    pszUserName,
    pszRealm,
    pszPassword,
    pszNonce,
    pszCNonce,
):
    m = md5()
    m.update(pszUserName)
    m.update(":")
    m.update(pszRealm)
    m.update(":")
    m.update(pszPassword)
    HA1 = m.digest()
    if pszAlg == "md5-sess":
        m = md5()
        m.update(HA1)
        m.update(":")
        m.update(pszNonce)
        m.update(":")
        m.update(pszCNonce)
        HA1 = m.digest()
    return HA1.encode('hex')


DigestCalcHA1 = deprecated(Version("Twisted", 9, 0, 0))(DigestCalcHA1)

def DigestCalcResponse(
    HA1,
    pszNonce,
    pszNonceCount,
    pszCNonce,
    pszQop,
    pszMethod,
    pszDigestUri,
    pszHEntity,
):
    m = md5()
    m.update(pszMethod)
    m.update(":")
    m.update(pszDigestUri)
    if pszQop == "auth-int":
        m.update(":")
        m.update(pszHEntity)
    HA2 = m.digest().encode('hex')
    
    m = md5()
    m.update(HA1)
    m.update(":")
    m.update(pszNonce)
    m.update(":")
    if pszNonceCount and pszCNonce: # pszQop:
        m.update(pszNonceCount)
        m.update(":")
        m.update(pszCNonce)
        m.update(":")
        m.update(pszQop)
        m.update(":")
    m.update(HA2)
    hash = m.digest().encode('hex')
    return hash


DigestCalcResponse = deprecated(Version("Twisted", 9, 0, 0))(DigestCalcResponse)

_absent = object()

class Via(object):
    """
    A L{Via} is a SIP Via header, representing a segment of the path taken by
    the request.

    See RFC 3261, sections 8.1.1.7, 18.2.2, and 20.42.

    @ivar transport: Network protocol used for this leg. (Probably either "TCP"
    or "UDP".)
    @type transport: C{str}
    @ivar branch: Unique identifier for this request.
    @type branch: C{str}
    @ivar host: Hostname or IP for this leg.
    @type host: C{str}
    @ivar port: Port used for this leg.
    @type port C{int}, or None.
    @ivar rportRequested: Whether to request RFC 3581 client processing or not.
    @type rportRequested: C{bool}
    @ivar rportValue: Servers wishing to honor requests for RFC 3581 processing
    should set this parameter to the source port the request was received
    from.
    @type rportValue: C{int}, or None.

    @ivar ttl: Time-to-live for requests on multicast paths.
    @type ttl: C{int}, or None.
    @ivar maddr: The destination multicast address, if any.
    @type maddr: C{str}, or None.
    @ivar hidden: Obsolete in SIP 2.0.
    @type hidden: C{bool}
    @ivar otherParams: Any other parameters in the header.
    @type otherParams: C{dict}
    """

    def __init__(self, host, port=PORT, transport="UDP", ttl=None,
                 hidden=False, received=None, rport=_absent, branch=None,
                 maddr=None, **kw):
        """
        Set parameters of this Via header. All arguments correspond to
        attributes of the same name.

        To maintain compatibility with old SIP
        code, the 'rport' argument is used to determine the values of
        C{rportRequested} and C{rportValue}. If None, C{rportRequested} is set
        to True. (The deprecated method for doing this is to pass True.) If an
        integer, C{rportValue} is set to the given value.

        Any arguments not explicitly named here are collected into the
        C{otherParams} dict.
        """
        self.transport = transport
        self.host = host
        self.port = port
        self.ttl = ttl
        self.hidden = hidden
        self.received = received
        if rport is True:
            warnings.warn(
                "rport=True is deprecated since Twisted 9.0.",
                DeprecationWarning,
                stacklevel=2)
            self.rportValue = None
            self.rportRequested = True
        elif rport is None:
            self.rportValue = None
            self.rportRequested = True
        elif rport is _absent:
            self.rportValue = None
            self.rportRequested = False
        else:
            self.rportValue = rport
            self.rportRequested = False

        self.branch = branch
        self.maddr = maddr
        self.otherParams = kw


    def _getrport(self):
        """
        Returns the rport value expected by the old SIP code.
        """
        if self.rportRequested == True:
            return True
        elif self.rportValue is not None:
            return self.rportValue
        else:
            return None


    def _setrport(self, newRPort):
        """
        L{Base._fixupNAT} sets C{rport} directly, so this method sets
        C{rportValue} based on that.

        @param newRPort: The new rport value.
        @type newRPort: C{int}
        """
        self.rportValue = newRPort
        self.rportRequested = False


    rport = property(_getrport, _setrport)

    def toString(self):
        """
        Serialize this header for use in a request or response.
        """
        s = "SIP/2.0/%s %s:%s" % (self.transport, self.host, self.port)
        if self.hidden:
            s += ";hidden"
        for n in "ttl", "branch", "maddr", "received":
            value = getattr(self, n)
            if value is not None:
                s += ";%s=%s" % (n, value)
        if self.rportRequested:
            s += ";rport"
        elif self.rportValue is not None:
            s += ";rport=%s" % (self.rport,)

        etc = self.otherParams.items()
        etc.sort()
        for k, v in etc:
            if v is None:
                s += ";" + k
            else:
                s += ";%s=%s" % (k, v)
        return s


def parseViaHeader(value):
    """
    Parse a Via header.

    @return: The parsed version of this header.
    @rtype: L{Via}
    """
    parts = value.split(";")
    sent, params = parts[0], parts[1:]
    protocolinfo, by = sent.split(" ", 1)
    by = by.strip()
    result = {}
    pname, pversion, transport = protocolinfo.split("/")
    if pname != "SIP" or pversion != "2.0":
        raise ValueError, "wrong protocol or version: %r" % value
    result["transport"] = transport
    if ":" in by:
        host, port = by.split(":")
        result["port"] = int(port)
        result["host"] = host
    else:
        result["host"] = by
    for p in params:
        # it's the comment-striping dance!
        p = p.strip().split(" ", 1)
        if len(p) == 1:
            p, comment = p[0], ""
        else:
            p, comment = p
        if p == "hidden":
            result["hidden"] = True
            continue
        parts = p.split("=", 1)
        if len(parts) == 1:
            name, value = parts[0], None
        else:
            name, value = parts
            if name in ("rport", "ttl"):
                value = int(value)
        result[name] = value
    return Via(**result)


class URL:
    """A SIP URL."""

    def __init__(self, host, username=None, password=None, port=None,
                 transport=None, usertype=None, method=None,
                 ttl=None, maddr=None, tag=None, other=None, headers=None):
        self.username = username
        self.host = host
        self.password = password
        self.port = port
        self.transport = transport
        self.usertype = usertype
        self.method = method
        self.tag = tag
        self.ttl = ttl
        self.maddr = maddr
        if other == None:
            self.other = []
        else:
            self.other = other
        if headers == None:
            self.headers = {}
        else:
            self.headers = headers

    def toString(self):
        l = []; w = l.append
        w("sip:")
        if self.username != None:
            w(self.username)
            if self.password != None:
                w(":%s" % self.password)
            w("@")
        w(self.host)
        if self.port != None:
            w(":%d" % self.port)
        if self.usertype != None:
            w(";user=%s" % self.usertype)
        for n in ("transport", "ttl", "maddr", "method", "tag"):
            v = getattr(self, n)
            if v != None:
                w(";%s=%s" % (n, v))
        for v in self.other:
            w(";%s" % v)
        if self.headers:
            w("?")
            w("&".join([("%s=%s" % (specialCases.get(h) or dashCapitalize(h), v)) for (h, v) in self.headers.items()]))
        return "".join(l)

    def __str__(self):
        return self.toString()
    
    def __repr__(self):
        return '<URL %s:%s@%s:%r/%s>' % (self.username, self.password, self.host, self.port, self.transport)


def parseURL(url, host=None, port=None):
    """Return string into URL object.

    URIs are of of form 'sip:user@example.com'.
    """
    d = {}
    if not url.startswith("sip:"):
        raise ValueError("unsupported scheme: " + url[:4])
    parts = url[4:].split(";")
    userdomain, params = parts[0], parts[1:]
    udparts = userdomain.split("@", 1)
    if len(udparts) == 2:
        userpass, hostport = udparts
        upparts = userpass.split(":", 1)
        if len(upparts) == 1:
            d["username"] = upparts[0]
        else:
            d["username"] = upparts[0]
            d["password"] = upparts[1]
    else:
        hostport = udparts[0]
    hpparts = hostport.split(":", 1)
    if len(hpparts) == 1:
        d["host"] = hpparts[0]
    else:
        d["host"] = hpparts[0]
        d["port"] = int(hpparts[1])
    if host != None:
        d["host"] = host
    if port != None:
        d["port"] = port
    for p in params:
        if p == params[-1] and "?" in p:
            d["headers"] = h = {}
            p, headers = p.split("?", 1)
            for header in headers.split("&"):
                k, v = header.split("=")
                h[k] = v
        nv = p.split("=", 1)
        if len(nv) == 1:
            d.setdefault("other", []).append(p)
            continue
        name, value = nv
        if name == "user":
            d["usertype"] = value
        elif name in ("transport", "ttl", "maddr", "method", "tag"):
            if name == "ttl":
                value = int(value)
            d[name] = value
        else:
            d.setdefault("other", []).append(p)
    return URL(**d)


def cleanRequestURL(url):
    """Clean a URL from a Request line."""
    url.transport = None
    url.maddr = None
    url.ttl = None
    url.headers = {}


def parseAddress(address, host=None, port=None, clean=0):
    """Return (name, uri, params) for From/To/Contact header.

    @param clean: remove unnecessary info, usually for From and To headers.
    """
    address = address.strip()
    # simple 'sip:foo' case
    if address.startswith("sip:"):
        return "", parseURL(address, host=host, port=port), {}
    params = {}
    name, url = address.split("<", 1)
    name = name.strip()
    if name.startswith('"'):
        name = name[1:]
    if name.endswith('"'):
        name = name[:-1]
    url, paramstring = url.split(">", 1)
    url = parseURL(url, host=host, port=port)
    paramstring = paramstring.strip()
    if paramstring:
        for l in paramstring.split(";"):
            if not l:
                continue
            k, v = l.split("=")
            params[k] = v
    if clean:
        # rfc 2543 6.21
        url.ttl = None
        url.headers = {}
        url.transport = None
        url.maddr = None
    return name, url, params


class SIPError(Exception):
    def __init__(self, code, phrase=None):
        if phrase is None:
            phrase = statusCodes[code]
        Exception.__init__(self, "SIP error (%d): %s" % (code, phrase))
        self.code = code
        self.phrase = phrase


class RegistrationError(SIPError):
    """Registration was not possible."""


class Message:
    """A SIP message."""

    length = None
    
    def __init__(self):
        self.headers = util.OrderedDict() # map name to list of values
        self.body = ""
        self.finished = 0
    
    def addHeader(self, name, value):
        name = name.lower()
        name = longHeaders.get(name, name)
        if name == "content-length":
            self.length = int(value)
        self.headers.setdefault(name,[]).append(value)

    def bodyDataReceived(self, data):
        self.body += data
    
    def creationFinished(self):
        if (self.length != None) and (self.length != len(self.body)):
            raise ValueError, "wrong body length"
        self.finished = 1

    def toString(self):
        s = "%s\r\n" % self._getHeaderLine()
        for n, vs in self.headers.items():
            for v in vs:
                s += "%s: %s\r\n" % (specialCases.get(n) or dashCapitalize(n), v)
        s += "\r\n"
        s += self.body
        return s

    def _getHeaderLine(self):
        raise NotImplementedError

    def computeBranch(self):
        """
        Create a branch tag to uniquely identify this message.  See RFC3261
        section 8.1.1.7.  Proxies that want to support loop detection need to
        do more than this: see section 16.6.8.
        """
        return VIA_COOKIE + ''.join(["%02x" % (random.randrange(256),)
                                     for _ in range(8)])



class Request(Message):
    """A Request for a URI"""


    def __init__(self, method, uri, version="SIP/2.0"):
        Message.__init__(self)
        self.method = method
        if isinstance(uri, URL):
            self.uri = uri
        else:
            self.uri = parseURL(uri)
            cleanRequestURL(self.uri)


    def __repr__(self):
        return "<SIP Request %s:%s %s>" % (hex(util.unsignedID(self)),
                                           self.method,
                                           self.uri.toString())


    def _getHeaderLine(self):
        return "%s %s SIP/2.0" % (self.method, self.uri.toString())



class Response(Message):
    """
    A SIP response message. See RFC 3261, section 7.2.

    @param code: The response code.
    @param phrase: A description of the response status.
    """

    def fromRequest(cls, code, request):
        """
        Create a response to a request, copying the essential headers from the
        given request as per the rules in RFC 3261, section 8.2.6.2.

        @param code: The SIP response code to use.
        @param request: The request to respond to.
        """
        response = cls(code)
        for name in ("via", "to", "from", "call-id", "cseq"):
            response.headers[name] = request.headers[name][:]
        return response


    fromRequest = classmethod(fromRequest)

    def __init__(self, code, phrase=None):
        Message.__init__(self)
        self.code = code
        if phrase == None:
            phrase = statusCodes[code]
        self.phrase = phrase


    def __repr__(self):
        """
        Compact printable representation of a response.
        """
        return "<SIP Response %s:%s>" % (hex(util.unsignedID(self)), self.code)


    def _getHeaderLine(self):
        """
        Returns the first line of the response message.
        """
        return "SIP/2.0 %s %s" % (self.code, self.phrase)



class MessagesParser(basic.LineReceiver):
    """A SIP messages parser.

    Expects dataReceived, dataDone repeatedly,
    in that order. Shouldn't be connected to actual transport.
    """

    version = "SIP/2.0"
    acceptResponses = 1
    acceptRequests = 1
    state = "firstline" # or "headers", "body" or "invalid"
    
    debug = 0
    
    def __init__(self, messageReceivedCallback):
        self.messageReceived = messageReceivedCallback
        self.reset()

    def reset(self, remainingData=""):
        self.state = "firstline"
        self.length = None # body length
        self.bodyReceived = 0 # how much of the body we received
        self.message = None
        self.setLineMode(remainingData)
    
    def invalidMessage(self):
        self.state = "invalid"
        self.setRawMode()
    
    def dataDone(self):
        # clear out any buffered data that may be hanging around
        self.clearLineBuffer()
        if self.state == "firstline":
            return
        if self.state != "body":
            self.reset()
            return
        if self.length == None:
            # no content-length header, so end of data signals message done
            self.messageDone()
        elif self.length < self.bodyReceived:
            # aborted in the middle
            self.reset()
        else:
            # we have enough data and message wasn't finished? something is wrong
            raise RuntimeError, "this should never happen"
    
    def dataReceived(self, data):
        try:
            basic.LineReceiver.dataReceived(self, data)
        except:
            log.err()
            self.invalidMessage()
    
    def handleFirstLine(self, line):
        """Expected to create self.message."""
        raise NotImplementedError

    def lineLengthExceeded(self, line):
        self.invalidMessage()
    
    def lineReceived(self, line):
        if self.state == "firstline":
            while line.startswith("\n") or line.startswith("\r"):
                line = line[1:]
            if not line:
                return
            try:
                a, b, c = line.split(" ", 2)
            except ValueError:
                self.invalidMessage()
                return
            if a == "SIP/2.0" and self.acceptResponses:
                # response
                try:
                    code = int(b)
                except ValueError:
                    self.invalidMessage()
                    return
                self.message = Response(code, c)
            elif c == "SIP/2.0" and self.acceptRequests:
                self.message = Request(a, b)
            else:
                self.invalidMessage()
                return
            self.state = "headers"
            return
        else:
            assert self.state == "headers"
        if line:
            # XXX support multi-line headers
            try:
                name, value = line.split(":", 1)
            except ValueError:
                self.invalidMessage()
                return
            self.message.addHeader(name, value.lstrip())
            if name.lower() == "content-length":
                try:
                    self.length = int(value.lstrip())
                except ValueError:
                    self.invalidMessage()
                    return
        else:
            # CRLF, we now have message body until self.length bytes,
            # or if no length was given, until there is no more data
            # from the connection sending us data.
            self.state = "body"
            if self.length == 0:
                self.messageDone()
                return
            self.setRawMode()

    def messageDone(self, remainingData=""):
        assert self.state == "body"
        self.message.creationFinished()
        self.messageReceived(self.message)
        self.reset(remainingData)
    
    def rawDataReceived(self, data):
        assert self.state in ("body", "invalid")
        if self.state == "invalid":
            return
        if self.length == None:
            self.message.bodyDataReceived(data)
        else:
            dataLen = len(data)
            expectedLen = self.length - self.bodyReceived
            if dataLen > expectedLen:
                self.message.bodyDataReceived(data[:expectedLen])
                self.messageDone(data[expectedLen:])
                return
            else:
                self.bodyReceived += dataLen
                self.message.bodyDataReceived(data)
                if self.bodyReceived == self.length:
                    self.messageDone()


class Base(protocol.DatagramProtocol):
    """Base class for SIP clients and servers."""
    
    PORT = PORT
    debug = False
    
    def __init__(self):
        self.messages = []
        self.parser = MessagesParser(self.addMessage)

    def addMessage(self, msg):
        self.messages.append(msg)

    def datagramReceived(self, data, addr):
        self.parser.dataReceived(data)
        self.parser.dataDone()
        for m in self.messages:
            self._fixupNAT(m, addr)
            if self.debug:
                log.msg("Received %r from %r" % (m.toString(), addr))
            if isinstance(m, Request):
                self.handle_request(m, addr)
            else:
                self.handle_response(m, addr)
        self.messages[:] = []

    def _fixupNAT(self, message, (srcHost, srcPort)):
        # RFC 2543 6.40.2,
        senderVia = parseViaHeader(message.headers["via"][0])
        if senderVia.host != srcHost:            
            senderVia.received = srcHost
            if senderVia.port != srcPort:
                senderVia.rport = srcPort
            message.headers["via"][0] = senderVia.toString()
        elif senderVia.rport == True:
            senderVia.received = srcHost
            senderVia.rport = srcPort
            message.headers["via"][0] = senderVia.toString()

    def deliverResponse(self, responseMessage):
        """Deliver response.

        Destination is based on topmost Via header."""
        destVia = parseViaHeader(responseMessage.headers["via"][0])
        # XXX we don't do multicast yet
        host = destVia.received or destVia.host
        port = destVia.rport or destVia.port or self.PORT
        destAddr = URL(host=host, port=port)
        self.sendMessage(destAddr, responseMessage)

    def responseFromRequest(self, code, request):
        """Create a response to a request message."""
        response = Response(code)
        for name in ("via", "to", "from", "call-id", "cseq"):
            response.headers[name] = request.headers.get(name, [])[:]

        return response

    def sendMessage(self, destURL, message):
        """Send a message.

        @param destURL: C{URL}. This should be a *physical* URL, not a logical one.
        @param message: The message to send.
        """
        if destURL.transport not in ("udp", None):
            raise RuntimeError, "only UDP currently supported"
        if self.debug:
            log.msg("Sending %r to %r" % (message.toString(), destURL))
        self.transport.write(message.toString(), (destURL.host, destURL.port or self.PORT))

    def handle_request(self, message, addr):
        """Override to define behavior for requests received

        @type message: C{Message}
        @type addr: C{tuple}
        """
        raise NotImplementedError

    def handle_response(self, message, addr):
        """Override to define behavior for responses received.
        
        @type message: C{Message}
        @type addr: C{tuple}
        """
        raise NotImplementedError


class IContact(Interface):
    """A user of a registrar or proxy"""


class Registration:
    def __init__(self, secondsToExpiry, contactURL):
        self.secondsToExpiry = secondsToExpiry
        self.contactURL = contactURL

class IRegistry(Interface):
    """Allows registration of logical->physical URL mapping."""

    def registerAddress(domainURL, logicalURL, physicalURL):
        """Register the physical address of a logical URL.

        @return: Deferred of C{Registration} or failure with RegistrationError.
        """

    def unregisterAddress(domainURL, logicalURL, physicalURL):
        """Unregister the physical address of a logical URL.

        @return: Deferred of C{Registration} or failure with RegistrationError.
        """

    def getRegistrationInfo(logicalURL):
        """Get registration info for logical URL.

        @return: Deferred of C{Registration} object or failure of LookupError.
        """


class ILocator(Interface):
    """Allow looking up physical address for logical URL."""

    def getAddress(logicalURL):
        """Return physical URL of server for logical URL of user.

        @param logicalURL: a logical C{URL}.
        @return: Deferred which becomes URL or fails with LookupError.
        """


class Proxy(Base):
    """SIP proxy."""
    
    PORT = PORT

    locator = None # object implementing ILocator
    
    def __init__(self, host=None, port=PORT):
        """Create new instance.

        @param host: our hostname/IP as set in Via headers.
        @param port: our port as set in Via headers.
        """
        self.host = host or socket.getfqdn()
        self.port = port
        Base.__init__(self)
        
    def getVia(self):
        """Return value of Via header for this proxy."""
        return Via(host=self.host, port=self.port)

    def handle_request(self, message, addr):
        # send immediate 100/trying message before processing
        #self.deliverResponse(self.responseFromRequest(100, message))
        f = getattr(self, "handle_%s_request" % message.method, None)
        if f is None:
            f = self.handle_request_default
        try:
            d = f(message, addr)
        except SIPError, e:
            self.deliverResponse(self.responseFromRequest(e.code, message))
        except:
            log.err()
            self.deliverResponse(self.responseFromRequest(500, message))
        else:
            if d is not None:
                d.addErrback(lambda e:
                    self.deliverResponse(self.responseFromRequest(e.code, message))
                )
        
    def handle_request_default(self, message, (srcHost, srcPort)):
        """Default request handler.
        
        Default behaviour for OPTIONS and unknown methods for proxies
        is to forward message on to the client.

        Since at the moment we are stateless proxy, thats basically
        everything.
        """
        def _mungContactHeader(uri, message):
            message.headers['contact'][0] = uri.toString()            
            return self.sendMessage(uri, message)
        
        viaHeader = self.getVia()
        if viaHeader.toString() in message.headers["via"]:
            # must be a loop, so drop message
            log.msg("Dropping looped message.")
            return

        message.headers["via"].insert(0, viaHeader.toString())
        name, uri, tags = parseAddress(message.headers["to"][0], clean=1)

        # this is broken and needs refactoring to use cred
        d = self.locator.getAddress(uri)
        d.addCallback(self.sendMessage, message)
        d.addErrback(self._cantForwardRequest, message)
    
    def _cantForwardRequest(self, error, message):
        error.trap(LookupError)
        del message.headers["via"][0] # this'll be us
        self.deliverResponse(self.responseFromRequest(404, message))
    
    def deliverResponse(self, responseMessage):
        """Deliver response.

        Destination is based on topmost Via header."""
        destVia = parseViaHeader(responseMessage.headers["via"][0])
        # XXX we don't do multicast yet
        host = destVia.received or destVia.host
        port = destVia.rport or destVia.port or self.PORT
        
        destAddr = URL(host=host, port=port)
        self.sendMessage(destAddr, responseMessage)

    def responseFromRequest(self, code, request):
        """Create a response to a request message."""
        response = Response(code)
        for name in ("via", "to", "from", "call-id", "cseq"):
            response.headers[name] = request.headers.get(name, [])[:]
        return response
    
    def handle_response(self, message, addr):
        """Default response handler."""
        v = parseViaHeader(message.headers["via"][0])
        if (v.host, v.port) != (self.host, self.port):
            # we got a message not intended for us?
            # XXX note this check breaks if we have multiple external IPs
            # yay for suck protocols
            log.msg("Dropping incorrectly addressed message")
            return
        del message.headers["via"][0]
        if not message.headers["via"]:
            # this message is addressed to us
            self.gotResponse(message, addr)
            return
        self.deliverResponse(message)
    
    def gotResponse(self, message, addr):
        """Called with responses that are addressed at this server."""
        pass

class IAuthorizer(Interface):
    def getChallenge(peer):
        """Generate a challenge the client may respond to.
        
        @type peer: C{tuple}
        @param peer: The client's address
        
        @rtype: C{str}
        @return: The challenge string
        """
    
    def decode(response):
        """Create a credentials object from the given response.
        
        @type response: C{str}
        """

class BasicAuthorizer:
    """Authorizer for insecure Basic (base64-encoded plaintext) authentication.
    
    This form of authentication is broken and insecure.  Do not use it.
    """

    implements(IAuthorizer)

    def __init__(self):
        """
        This method exists solely to issue a deprecation warning.
        """
        warnings.warn(
            "twisted.protocols.sip.BasicAuthorizer was deprecated "
            "in Twisted 9.0.0",
            category=DeprecationWarning,
            stacklevel=2)


    def getChallenge(self, peer):
        return None
    
    def decode(self, response):
        # At least one SIP client improperly pads its Base64 encoded messages
        for i in range(3):
            try:
                creds = (response + ('=' * i)).decode('base64')
            except:
                pass
            else:
                break
        else:
            # Totally bogus
            raise SIPError(400)
        p = creds.split(':', 1)
        if len(p) == 2:
            return UsernamePassword(*p)
        raise SIPError(400)



class DigestedCredentials(UsernameHashedPassword):
    """Yet Another Simple Digest-MD5 authentication scheme"""
    
    def __init__(self, username, fields, challenges):
        warnings.warn(
            "twisted.protocols.sip.DigestedCredentials was deprecated "
            "in Twisted 9.0.0",
            category=DeprecationWarning,
            stacklevel=2)
        self.username = username
        self.fields = fields
        self.challenges = challenges
    
    def checkPassword(self, password):
        method = 'REGISTER'
        response = self.fields.get('response')
        uri = self.fields.get('uri')
        nonce = self.fields.get('nonce')
        cnonce = self.fields.get('cnonce')
        nc = self.fields.get('nc')
        algo = self.fields.get('algorithm', 'MD5')
        qop = self.fields.get('qop-options', 'auth')
        opaque = self.fields.get('opaque')

        if opaque not in self.challenges:
            return False
        del self.challenges[opaque]
        
        user, domain = self.username.split('@', 1)
        if uri is None:
            uri = 'sip:' + domain

        expected = DigestCalcResponse(
            DigestCalcHA1(algo, user, domain, password, nonce, cnonce),
            nonce, nc, cnonce, qop, method, uri, None,
        )
        
        return expected == response

class DigestAuthorizer:
    CHALLENGE_LIFETIME = 15
    
    implements(IAuthorizer)
    
    def __init__(self):
        warnings.warn(
            "twisted.protocols.sip.DigestAuthorizer was deprecated "
            "in Twisted 9.0.0",
            category=DeprecationWarning,
            stacklevel=2)

        self.outstanding = {}



    def generateNonce(self):
        c = tuple([random.randrange(sys.maxint) for _ in range(3)])
        c = '%d%d%d' % c
        return c

    def generateOpaque(self):
        return str(random.randrange(sys.maxint))

    def getChallenge(self, peer):
        c = self.generateNonce()
        o = self.generateOpaque()
        self.outstanding[o] = c
        return ','.join((
            'nonce="%s"' % c,
            'opaque="%s"' % o,
            'qop-options="auth"',
            'algorithm="MD5"',
        ))
        
    def decode(self, response):
        response = ' '.join(response.splitlines())
        parts = response.split(',')
        auth = dict([(k.strip(), unq(v.strip())) for (k, v) in [p.split('=', 1) for p in parts]])
        try:
            username = auth['username']
        except KeyError:
            raise SIPError(401)
        try:
            return DigestedCredentials(username, auth, self.outstanding)
        except:
            raise SIPError(400)


class RegisterProxy(Proxy):
    """A proxy that allows registration for a specific domain.

    Unregistered users won't be handled.
    """

    portal = None

    registry = None # should implement IRegistry

    authorizers = {
        'digest': DigestAuthorizer(),
    }

    def __init__(self, *args, **kw):
        Proxy.__init__(self, *args, **kw)
        self.liveChallenges = {}
        
    def handle_ACK_request(self, message, (host, port)):
        # XXX
        # ACKs are a client's way of indicating they got the last message
        # Responding to them is not a good idea.
        # However, we should keep track of terminal messages and re-transmit
        # if no ACK is received.
        pass

    def handle_REGISTER_request(self, message, (host, port)):
        """Handle a registration request.

        Currently registration is not proxied.
        """
        if self.portal is None:
            # There is no portal.  Let anyone in.
            self.register(message, host, port)
        else:
            # There is a portal.  Check for credentials.
            if not message.headers.has_key("authorization"):
                return self.unauthorized(message, host, port)
            else:
                return self.login(message, host, port)

    def unauthorized(self, message, host, port):
        m = self.responseFromRequest(401, message)
        for (scheme, auth) in self.authorizers.iteritems():
            chal = auth.getChallenge((host, port))
            if chal is None:
                value = '%s realm="%s"' % (scheme.title(), self.host)
            else:
                value = '%s %s,realm="%s"' % (scheme.title(), chal, self.host)
            m.headers.setdefault('www-authenticate', []).append(value)
        self.deliverResponse(m)


    def login(self, message, host, port):
        parts = message.headers['authorization'][0].split(None, 1)
        a = self.authorizers.get(parts[0].lower())
        if a:
            try:
                c = a.decode(parts[1])
            except SIPError:
                raise
            except:
                log.err()
                self.deliverResponse(self.responseFromRequest(500, message))
            else:
                c.username += '@' + self.host
                self.portal.login(c, None, IContact
                    ).addCallback(self._cbLogin, message, host, port
                    ).addErrback(self._ebLogin, message, host, port
                    ).addErrback(log.err
                    )
        else:
            self.deliverResponse(self.responseFromRequest(501, message))

    def _cbLogin(self, (i, a, l), message, host, port):
        # It's stateless, matey.  What a joke.
        self.register(message, host, port)

    def _ebLogin(self, failure, message, host, port):
        failure.trap(cred.error.UnauthorizedLogin)
        self.unauthorized(message, host, port)

    def register(self, message, host, port):
        """Allow all users to register"""
        name, toURL, params = parseAddress(message.headers["to"][0], clean=1)
        contact = None
        if message.headers.has_key("contact"):
            contact = message.headers["contact"][0]

        if message.headers.get("expires", [None])[0] == "0":
            self.unregister(message, toURL, contact)
        else:
            # XXX Check expires on appropriate URL, and pass it to registry
            # instead of having registry hardcode it.
            if contact is not None:
                name, contactURL, params = parseAddress(contact, host=host, port=port)
                d = self.registry.registerAddress(message.uri, toURL, contactURL)
            else:
                d = self.registry.getRegistrationInfo(toURL)
            d.addCallbacks(self._cbRegister, self._ebRegister,
                callbackArgs=(message,),
                errbackArgs=(message,)
            )

    def _cbRegister(self, registration, message):
        response = self.responseFromRequest(200, message)
        if registration.contactURL != None:
            response.addHeader("contact", registration.contactURL.toString())
            response.addHeader("expires", "%d" % registration.secondsToExpiry)
        response.addHeader("content-length", "0")
        self.deliverResponse(response)

    def _ebRegister(self, error, message):
        error.trap(RegistrationError, LookupError)
        # XXX return error message, and alter tests to deal with
        # this, currently tests assume no message sent on failure

    def unregister(self, message, toURL, contact):
        try:
            expires = int(message.headers["expires"][0])
        except ValueError:
            self.deliverResponse(self.responseFromRequest(400, message))
        else:
            if expires == 0:
                if contact == "*":
                    contactURL = "*"
                else:
                    name, contactURL, params = parseAddress(contact)
                d = self.registry.unregisterAddress(message.uri, toURL, contactURL)
                d.addCallback(self._cbUnregister, message
                    ).addErrback(self._ebUnregister, message
                    )

    def _cbUnregister(self, registration, message):
        msg = self.responseFromRequest(200, message)
        msg.headers.setdefault('contact', []).append(registration.contactURL.toString())
        msg.addHeader("expires", "0")
        self.deliverResponse(msg)

    def _ebUnregister(self, registration, message):
        pass


class InMemoryRegistry:
    """A simplistic registry for a specific domain."""

    implements(IRegistry, ILocator)
    
    def __init__(self, domain):
        self.domain = domain # the domain we handle registration for
        self.users = {} # map username to (IDelayedCall for expiry, address URI)

    def getAddress(self, userURI):
        if userURI.host != self.domain:
            return defer.fail(LookupError("unknown domain"))
        if self.users.has_key(userURI.username):
            dc, url = self.users[userURI.username]
            return defer.succeed(url)
        else:
            return defer.fail(LookupError("no such user"))
            
    def getRegistrationInfo(self, userURI):
        if userURI.host != self.domain:
            return defer.fail(LookupError("unknown domain"))
        if self.users.has_key(userURI.username):
            dc, url = self.users[userURI.username]
            return defer.succeed(Registration(int(dc.getTime() - time.time()), url))
        else:
            return defer.fail(LookupError("no such user"))
        
    def _expireRegistration(self, username):
        try:
            dc, url = self.users[username]
        except KeyError:
            return defer.fail(LookupError("no such user"))
        else:
            dc.cancel()
            del self.users[username]
        return defer.succeed(Registration(0, url))
    
    def registerAddress(self, domainURL, logicalURL, physicalURL):
        if domainURL.host != self.domain:
            log.msg("Registration for domain we don't handle.")
            return defer.fail(RegistrationError(404))
        if logicalURL.host != self.domain:
            log.msg("Registration for domain we don't handle.")
            return defer.fail(RegistrationError(404))
        if self.users.has_key(logicalURL.username):
            dc, old = self.users[logicalURL.username]
            dc.reset(3600)
        else:
            dc = reactor.callLater(3600, self._expireRegistration, logicalURL.username)
        log.msg("Registered %s at %s" % (logicalURL.toString(), physicalURL.toString()))
        self.users[logicalURL.username] = (dc, physicalURL)
        return defer.succeed(Registration(int(dc.getTime() - time.time()), physicalURL))

    def unregisterAddress(self, domainURL, logicalURL, physicalURL):
        return self._expireRegistration(logicalURL.username)



class ISIPTransport(Interface):
    """
    The transport layer of the SIP protocol. See RFC 3261
    section 18.
    """
    hosts = Attribute("""
                      A sequence of hostnames this transport is
                      authoritative for.
                      """)

    port = Attribute("""
                     The port number this transport listens on.
                     """)

    def isReliable():
        """
        Returns whether this transport uses a reliable protocol or not.

        @rtype: C{bool}
        """


    def defaultHostname():
        """
        Returns the hostname used on outgoing messages to identify this
        transport. Must be an item from C{hosts}.

        @rtype: C{str}
        """


    def sendRequest(msg, target):
        """
        Send a request to a remote host. The top Via header is modified to
        include sent-by information.

        @param msg: The SIP request to be sent.
        @type msg: L{Request}

        @param target: A (host, port) tuple.
        @type target: Tuple of (C{str}, C{int}).
        """


    def sendResponse(msg):
        """
        Send a response to a request, delivering it to the host the request was
        received from.

        @param msg: The SIP response to be sent.
        @type msg: L{Response}
        """



class SIPTransport(protocol.DatagramProtocol):
    """
    The UDP version of the transport layer of the SIP protocol. See RFC 3261
    section 18.

    @ivar _transactionUser: A transaction user. Its `requestReceived` and
        `responseReceived` methods will be called when (non-retransmitted)
        messages are received, and its `clientTransactionTerminated` method
        will be called when client transactions it has created terminate.
    @type _transactionUser: an provider of L{ITransactionUser}.
    @ivar hosts: A sequence of hostnames this transport is authoritative for.
    @ivar port: The port number this transport listens on.
    @ivar _parser: A L{MessagesParser}.
    @ivar _messages: A list of L{Message}s not yet processed.
    @ivar _serverTransactions: A mapping of (branch, host, port, method) to
    L{ServerTransaction} or L{ServerInviteTransaction} instances.
    @ivar _oldStyleServerTransactions: A list of pairs of Requests from
    non-RFC3261-compliant user agents, and L{ServerTransaction} or
    L{ServerInviteTransaction} instances.

    @ivar _clientTransactions: A mapping of branch strings (from Via headers) to
    L{ClientTransaction} or L{ClientInviteTransaction} instances.
    @ivar _clock: A provider of L{twisted.internet.interfaces.IReactorTime}.
    """

    implements(ISIPTransport)

    def __init__(self, tu, hosts, port, clock):
        """
        Set initial values.
        """
        self.hosts = hosts
        self.port = port
        self._messages = []
        self._parser = MessagesParser(self._messages.append)
        self._transactionUser = tu
        self._serverTransactions = {}
        self._oldStyleServerTransactions = []
        self._clientTransactions = {}
        self._clock = clock

    def defaultHostname(self):
        """
        @see: L{ISIPTransport.defaultHostname}
        """
        return self.hosts[0]


    def isReliable(self):
        """
        UDP transports are unreliable.

        @return: False
        """
        return False


    def datagramReceived(self, data, addr):
        """
        Feed received datagrams to the SIP parser.
        """
        self._parser.dataReceived(data)
        self._parser.dataDone()
        for m in self._messages:
            if isinstance(m, Request):
                self._handleRequest(m, addr)
            else:
                self._handleResponse(m, addr)
        del self._messages[:]


    def _newServerTransaction(self, st, msg, via):
        """
        Store a server transaction created by the TU so it can be matched up
        with retransmissions of requests later.
        """
        if (via.branch and via.branch.startswith(VIA_COOKIE)):
            self._serverTransactions[(via.branch, via.host,
                                     via.port, msg.method)] = st
        else:
            self._oldStyleServerTransactions.append((st, msg))

    def _matchOldStyleRequest(self, msg, via):
        """
        Requests sent by RFC 2543-compliant implementations do not have unique
        branch parameters, so various elements of the message must be compared
        to match it to a transaction.

        @return: The matched server transaction, or None.
        """

        for (st, original) in self._oldStyleServerTransactions:
            if original.method == "INVITE":
                if msg.method in  ("INVITE", "CANCEL"):
                    originalToTag = parseAddress(
                        original.headers['to'][0])[2].get('tag','')
                elif msg.method == "ACK":
                    originalToTag = parseAddress(
                        st._lastResponse.headers['to'][0])[2].get('tag','')
                else:
                    continue
            else:
                if original.method == msg.method:
                    originalToTag = parseAddress(
                        original.headers['to'][0])[2].get('tag','')
                else:
                    continue
            #XXX URI comparison: #3582
            if (original.uri.toString() == msg.uri.toString() and
                (parseAddress(msg.headers['to'][0])[2].get('tag','') ==
                 originalToTag) and
                (parseAddress(msg.headers['from'][0])[2].get('tag','') ==
                 parseAddress(original.headers['from'][0])[2].get('tag','')) and
                original.headers['call-id'][0] == msg.headers['call-id'][0] and
                (original.headers['cseq'][0].split(' ',1)[0] ==
                 msg.headers['cseq'][0].split(' ',1)[0]) and
                original.headers['via'][0] == msg.headers['via'][0]):
                return st

        return None


    def _handleRequest(self, msg, addr):
        """
        Make sure a received request is valid, then match it up with a server
        transaction for processing. If there is none, deliver it to the
        transaction user, and if it returns a new server transaction, register
        it. See RFC 3261 sections 18.2.1 and 17.2.3.
        """
        def _badRequest(err):
            if err.check(SIPError):
                code = err.value.code
            else:
                code = INTERNAL_ERROR
            response = Response.fromRequest(code, msg)
            st = ServerTransaction(self, self._clock)
            st.messageReceivedFromTU(response)
            return st

        invalidRequest = False
        for header in ('to', 'from', 'call-id', 'via', 'max-forwards',
                       'cseq'):
            if header not in msg.headers:
                invalidRequest = True
                if header == 'via':
                    msg.addHeader('via',
                                  Via(host=addr[0], port=addr[1],
                                      branch=msg.computeBranch()).toString())
                else:
                    msg.addHeader(header, '')
        if invalidRequest:
            return defer.fail(SIPError(BAD_REQUEST)).addErrback(_badRequest)

        via = parseViaHeader(msg.headers['via'][0])

        if via.host != addr[0]:
            via.received = addr[0]
        if via.rport is True:
            via.rport = addr[1]
        msg.headers['via'][0] = via.toString()

        if not (via.branch and via.branch.startswith(VIA_COOKIE)):
            st = self._matchOldStyleRequest(msg, via)
        else:
            method = msg.method
            if method in ("ACK", "CANCEL"):
                method = "INVITE"
            st = self._serverTransactions.get((via.branch, via.host,
                                               via.port, method))

        def addNewServerTransaction(st):
            if st:
                if msg.method == 'INVITE':
                    st.messageReceivedFromTU(Response.fromRequest(100, msg))
                self._newServerTransaction(st, msg, via)

        if st:
            st.messageReceived(msg)
        else:
            return defer.maybeDeferred(self._transactionUser.requestReceived, msg, addr
                                       ).addErrback(_badRequest
                                       ).addCallback(addNewServerTransaction
                                       ).addErrback(log.err)

    def sendRequest(self, msg, target):
        """
        @see: L{ISIPTransport.sendRequest}
        """

        via = parseViaHeader(msg.headers['via'][0])
        via.host = self.defaultHostname()
        via.port = self.port
        msg.headers['via'][0] = via.toString()
        txt = msg.toString()
        if len(txt) > 1300:
            #XXX add support for TCP: #3626
            raise NotImplementedError, "Message too big for UDP."
        self.transport.write(txt, target)


    def _handleResponse(self, msg, target):
        """
        Deliver responses to client transactions, if any match. Otherwise
        deliver directly to the transaction user.
        """
        via = parseViaHeader(msg.headers['via'][0])
        if not (via.host in self.hosts and via.port == self.port):
            #drop silently
            return
        ct = self._clientTransactions.get(via.branch)
        if (ct and msg.headers['cseq'][0].split(' ')[1] ==
            ct.request.headers['cseq'][0].split(' ')[1]):
            ct.messageReceived(msg)
        else:
            self._transactionUser.responseReceived(msg, None)


    def sendResponse(self, msg):
        """
        @see: L{ISIPTransport.sendResponse}
        """
        via = parseViaHeader(msg.headers['via'][0])
        host = via.received or via.host
        if via.rport is not None:
            port = via.rport
        else:
            port = via.port or PORT
        txt = msg.toString()
        if len(txt) > 1300:
            #XXX add support for TCP: #3626
            raise NotImplementedError, "Message too big for UDP."
        self.transport.write(txt, (host, port))



class ITransactionUser(Interface):
    """
    Providers of this interface fill the 'Transaction User' role, as described
    in RFC3261, section 5.
    """

    def start(transport):
        """Connects the transport to the TU.

        @param transport: a L{SIPTransport} instance.
        """


    def requestReceived(msg, addr):
        """
        Processes a message, after the transport and transaction layer are
        finished with it. May return a L{ServerTransaction} (or
        L{ServerInviteTransaction}), which will handle subsequent messages from
        that SIP transaction.

        @param msg: a L{Message} instance
        @param addr: a C{(host, port)} tuple
        """


    def responseReceived(msg, ct=None):
        """
        Processes a response received from the transport, along with the client
        transaction it is a part of, if any.

        @param msg: a L{Message} instance.

        @param ct: a L{ClientTransaction} or L{ClientInviteTransaction}
        instance that represents the SIP transaction the given message is a
        part of.
        """


    def clientTransactionTerminated(ct):
        """
        Called when a client transaction created by this TU transitions to the
        'terminated' state.

        @param ct: a L{ClientTransaction} or L{ClientInviteTransaction}
        instance that has been terminated, either by a timeout or by a message
        separately sent to C{responseReceived}.
        """



class ServerTransaction(object):
    """
    Non-INVITE server transactions, as defined in RFC 3261, section 17.2.2.

    @ivar _transport: The L{SIPTransport} this transaction sends responses to
    and receives requests from.
    @ivar _clock: A provider of L{twisted.internet.interfaces.IReactorTime}.
    @ivar _mode: One of 'trying', 'proceeding', 'completed', or 'terminated'.
    @ivar _lastResponse: The most recent response sent by the TU. None if none
    have been sent.
    """

    def __init__(self, transport, clock):
        self._clock = clock
        self._transport = transport
        self._mode = 'trying'
        self._lastResponse = None


    def _respond(self, msg):
        """
        Send a response to the transport.

        @type msg: L{Response}
        """
        self._transport.sendResponse(msg)
        self._lastResponse = msg


    def messageReceived(self, msg):
        """
        Deal with requests received from the transport.

        @param msg: A L{Request}.
        """
        if self._mode == 'trying':
            pass
        elif self._mode in ['proceeding', 'completed']:
            self._transport.sendResponse(self._lastResponse)
        elif self._mode == 'terminated':
            raise RuntimeError('No further requests should be directed to'
                               ' this transaction.')


    def messageReceivedFromTU(self, msg):
        """
        Deal with responses sent by the transaction user.

        @param msg: A L{Response}.
        """
        if self._mode == 'trying':
            self._respond(msg)
            if 100 <= msg.code < 200:
                self._mode = 'proceeding'
            else:
                self._complete()
        elif self._mode == 'proceeding':
            self._respond(msg)
            if msg.code >= 200:
                self._complete()
        elif self._mode == 'terminated':
            raise RuntimeError('No further responses can be sent in this '
                               'transaction.')


    def _complete(self):
        """
        Change the transaction's state to 'completed', if on an unreliable
        transport, and set the timer for termination. Otherwise change it to
        'terminated'.
        """
        if self._transport.isReliable():
            self._terminate()
        else:
            self._mode = 'completed'
            self._clock.callLater(64*_T1, self._terminate)


    def _terminate(self):
        """
        Switch this transaction to the 'terminated' state and inform the TU.
        """
        self._mode = 'terminated'
        self._transport.serverTransactionTerminated(self)



class ServerInviteTransaction(object):
    """
    Implementation of INVITE server transactions.  See RFC 3261, section
    17.2.1.

    @ivar _transport: The SIP transport protocol.
    @ivar clock: A provider of L{twisted.internet.interfaces.IReactorTime}.
    @ivar _mode: One of 'proceeding', 'completed', or 'terminated'.
    @ivar _lastResponse: The most recent response sent by the TU. None if none
    have been sent.
    @ivar _timerG: A L{twisted.internet.base.DelayedCall}, or None.
    @ivar _timerH: A L{twisted.internet.base.DelayedCall}, or None.
    @ivar _timerGTries: Number of retransmission attempts triggered by timer G.
    """

    def __init__(self, transport, clock):
        """
        @param transport: A L{SIPTransport}.
        """
        self._clock = clock
        self._transport = transport
        self._mode = 'proceeding'
        self._timerG = None
        self._timerH = None
        self._timerGTries = 1
        self._lastResponse = None


    def messageReceivedFromTU(self, msg):
        """
        Deal with responses sent by the transaction user.

        @param msg: A L{Response}.
        """
        if self._mode == 'terminated':
            raise RuntimeError('No further responses can be sent in this '
                               'transaction.')
        self._lastResponse = msg
        self._transport.sendResponse(msg)
        if 200 <= msg.code < 300:
            self._terminate()
        elif msg.code >= 300:
            self._mode = 'completed'
            def timerGRetry():
                self._timerGTries +=1
                self._transport.sendResponse(self._lastResponse)
                self._timerG = self._clock.callLater(
                    min((2**self._timerGTries)*_T1, _T2), timerGRetry)
            if not self._transport.isReliable():
                self._timerG = self._clock.callLater(_T1, timerGRetry)
            def _doTimerH():
                if self._timerG is not None:
                    self._timerG.cancel()
                    self._timerG = None
                self._terminate()
            self._timerH = self._clock.callLater(64*_T1, _doTimerH)


    def messageReceived(self, msg):
        """
        Deal with requests received from the transport.

        @param msg: A L{Request}.
        """
        if self._mode == 'terminated':
            raise RuntimeError('No further requests should be directed to'
                               ' this transaction.')
        if self._mode == 'confirmed':
            return
        if msg.method == 'ACK':
            self._mode = 'confirmed'
            if self._timerG is not None:
                self._timerG.cancel()
                self._timerG = None
            if not self._transport.isReliable():
                self._timerI = self._clock.callLater(_T4, self._terminate)
            else:
                self._terminate()
        else:
            self._transport.sendResponse(self._lastResponse)


    def _terminate(self):
        """
        Switch this transaction to the 'terminated' state and inform the TU.
        """
        self._mode = 'terminated'
        self._transport.serverTransactionTerminated(self)



class ClientTransaction(object):
    """
    Implementation of non-INVITE client transactions. See RFC 3261, section
    17.1.2.

    @ivar _transport: The SIP transport protocol.
    @ivar _transactionUser: The transaction user.
    @type _transactionUser: A provider of L{ITransactionUser}
    @ivar clock: A provider of L{twisted.internet.interfaces.IReactorTime}.
    @ivar branch: A string to use as the 'branch' parameter in the Via header
    this transaction will insert when sending the request.
    @ivar _mode: One of 'proceeding', 'completed', or 'terminated'.
    @ivar _timerETries: Number of retransmission attempts triggered by timer E.
    @ivar _timerE: A L{twisted.internet.base.DelayedCall}, or None.
    @ivar _timerF: A L{twisted.internet.base.DelayedCall}, or None.
    @ivar _timerK: A L{twisted.internet.base.DelayedCall}, or None.
    """

    def __init__(self, transport, tu, request, target, clock):
        """
        Set up initial values and add a Via header to the request.
        """
        self._clock = clock
        self._transport = transport
        self._transactionUser = tu
        self.request = request
        self.target = target
        self._mode = 'trying'
        self._timerETries = 1
        self._timerE = None
        def timerERetry():
            self._transport.sendRequest(self.request, self.target)
            if self._mode == 'proceeding':
                later = _T2
            elif self._mode == 'trying':
                self._timerETries +=1
                later = min((2**self._timerETries)*_T1, _T2)
            self._timerE = self._clock.callLater(
                later, timerERetry)
        if not self._transport.isReliable():
            self._timerE = self._clock.callLater(_T1, timerERetry)
        self._timerF = self._clock.callLater(64*_T1, self._doTimeout)

        if self.request.method in ('ACK', 'CANCEL'):
            # code constructing ACKs and CANCELs are responsible for inserting
            # the Via header from the request they are associated with.
            self.branch = None
        else:
            self.branch = request.computeBranch()
            self.request.headers['via'].insert(0, Via(None, branch=self.branch
                                                      ).toString())
        self._transport._clientTransactions[self.branch] = self
        self._transport.sendRequest(self.request, self.target)


    def _doTimeout(self):
        """
        If timer F fires, terminate the transaction and send the
        appropriate timeout/cancel error code.
        """
        self._stopTimers()
        self._transactionUser.responseReceived(Response.fromRequest(408,
                                                                  self.request),
                                        self)
        self._terminate()


    def _stopTimers(self):
        """
        Stop timers E and F, if they are running.
        """
        if self._timerE is not None:
            if self._timerE.active():
                self._timerE.cancel()
            self._timerE = None
        if self._timerF is not None:
            if self._timerF.active():
                self._timerF.cancel()
            self._timerF = None


    def _terminate(self):
        """
        Switch this transaction to the 'terminated' state, unregister from the
        transport and inform the TU.
        """
        self._mode = 'terminated'
        for k, v in self._transport._clientTransactions.iteritems():
            if v is self:
                del self._transport._clientTransactions[k]
                break

        self._transactionUser.clientTransactionTerminated(self)


    def messageReceived(self, msg):
        """
        Deal with responses received from the transport.

        @param msg: A L{Response}.
        """
        if self._mode == 'terminated':
            raise RuntimeError('No further responses should be directed to'
                               ' this transaction.')
        if self._mode == 'completed':
            return
        if msg.code < 200:
            if self._mode == 'trying':
                self._mode = 'proceeding'
        else:
            self._mode = 'completed'
            self._stopTimers()
            if self._transport.isReliable():
                self._terminate()
            else:
                self._timerK = self._clock.callLater(_T4, self._terminate)
        self._transactionUser.responseReceived(msg, self)



class ClientInviteTransaction(object):
    """
    Implementation of INVITE client transactions. See RFC 3261, section 17.1.1.

    @ivar _transport: The SIP transport protocol.
    @ivar _transactionUser: The transaction user.
    @type _transactionUser: A provider of L{ITransactionUser}.
    @ivar clock: A provider of L{twisted.internet.interfaces.IReactorTime}.
    @ivar branch: A string to use as the 'branch' parameter in the Via header
    this transaction will insert when sending the request.
    @ivar _mode: One of 'proceeding', 'completed', or 'terminated'.
    @ivar _timerA: A L{twisted.internet.base.DelayedCall}, or None.
    @ivar _timerB: A L{twisted.internet.base.DelayedCall}, or None.
    @ivar _timerD: A L{twisted.internet.base.DelayedCall}, or None.
    @ivar _timerATries: Number of retransmission attempts triggered by timer A.
    @ivar _cancelDeferred: When a cancellation is pending, a Deferred to be
    fired when the CANCEL message is sent, otherwise None.
    """

    def __init__(self, transport, tu, request, target, clock):
        self._clock = clock
        self._transport = transport
        self._transactionUser = tu
        self.request = request
        self.target = target
        self._mode = 'calling'
        self._timerATries = 0
        self._timerA = None
        self.branch = request.computeBranch()
        self._transport._clientTransactions[self.branch] = self
        self.request.headers['via'].insert(0, Via(None, branch=self.branch))

        def timerARetry():
            self._transport.sendRequest(self.request, self.target)
            if not transport.isReliable():
                self._timerA = self._clock.callLater((2**self._timerATries)*_T1,
                                                    timerARetry)
                self._timerATries +=1

        timerARetry()
        self._timerB = self._clock.callLater(64*_T1, self._doTimeout)
        self._timerD = None
        self._cancelDeferred = None


    def _doTimeout(self):
        """
        If timer B fires, terminate the transaction and send the
        appropriate timeout/cancel error code.
        """
        self._stopTimers()
        self._transactionUser.responseReceived(Response.fromRequest(408,
                                                                  self.request),
                                        self)
        self._terminate()


    def _stopTimers(self):
        """
        Stop timers A and B, if they are running.
        """
        if self._timerB is not None:
            if self._timerB.active():
                self._timerB.cancel()
            self._timerB = None
        if self._timerA is not None:
            if self._timerA.active():
                self._timerA.cancel()
            self._timerA = None


    def _terminate(self):
        """
        Switch this transaction to the 'terminated' state, unregister from the
        transport and inform the TU.
        """
        self._mode = 'terminated'
        for k, v in self._transport._clientTransactions.iteritems():
            if v is self:
                del self._transport._clientTransactions[k]
                break

        self._transactionUser.clientTransactionTerminated(self)


    def _ack(self, msg):
        """
        Send an ACK message to the response received.  See RFC3261, section
        17.1.1.3.
        """
        ack = Request('ACK',self.request.uri)
        for name in ("from", "call-id", 'route'):
            ack.headers[name] = self.request.headers.get(name, [])[:]
        cseq = self.request.headers['cseq'][0].split(' ',1)[0]
        ack.addHeader('cseq', cseq + " ACK")
        ack.headers['to'] = msg.headers['to']
        ack.addHeader('via', self.request.headers['via'][0])
        self._transport.sendRequest(ack, self.target)


    def messageReceived(self, msg):
        """
        Deal with responses received from the transport.

        @param msg: A L{Response}.
        """
        if self._mode == 'terminated':
            raise RuntimeError('No further responses should be directed to'
                               ' this transaction.')
        if self._mode == 'completed':
            return
        if msg.code < 200:
            if self._mode == 'calling':
                self._mode = 'proceeding'
        elif 200 <= msg.code < 300:
            self._terminate()
        else:
            self._mode = 'completed'
            self._ack(msg)
            if not self._transport.isReliable():
                self.timerD = self._clock.callLater(32, self._terminate)
            else:
                self._terminate()
        self._stopTimers()
        self._transactionUser.responseReceived(msg, self)


    def cancel(self):
        """
        Send a CANCEL message to the target of this INVITE, in its own
        transaction.

        @return: A Deferred which fires with the new L{ClientTransaction} for
        the CANCEL, after it has been sent.
        """
        self._cancelDeferred = defer.Deferred()
        cancel = Request("CANCEL", self.request.uri)
        for hdr in ('from','to','call-id'):
            cancel.addHeader(hdr, self.request.headers[hdr][0])
        cseq = self.request.headers['cseq'][0].split(' ',1)[0]
        cancel.addHeader('cseq', cseq + " CANCEL")
        cancel.addHeader('via', Via(self._transport.host,
                                    self._transport.port,
                                    branch=self.branch).toString())

        cancelCT = ClientTransaction(self._transport, self._transactionUser,
                                     cancel, self.target, self._clock)
        self._cancelDeferred.callback(cancelCT)
        return self._cancelDeferred



#Timer values defined in RFC 3261, section 30.
_T1 = 0.5 # An estimate of round-trip time.

_T2 = 4 # Maximum interval between retransmission attempts.

_T4 = 5 # The amount of time the network will take to clear messages between
        # client and server transactions.


