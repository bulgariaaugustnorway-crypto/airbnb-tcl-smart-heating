"""HTTPS transport with explicit URL and redirect-scheme validation."""
import urllib.parse
import urllib.request


def validate_https(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        raise ValueError("External requests require HTTPS without URL credentials")


class HTTPSOnlyRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_https(newurl)
        old = urllib.parse.urlsplit(req.full_url)
        new = urllib.parse.urlsplit(newurl)
        if (old.hostname, old.port or 443) != (new.hostname, new.port or 443):
            raise ValueError("Cross-origin redirects are disabled to protect request credentials")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_https(request, timeout=10):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    validate_https(url)
    opener = urllib.request.build_opener(HTTPSOnlyRedirectHandler())
    return opener.open(request, timeout=timeout)
