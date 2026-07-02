import socket
import ipaddress
import asyncio
from urllib.parse import urlparse

class EgressError(Exception):
    pass

class EgressController:
    """Enforces outbound network restrictions for DevAgent web verbs.
    
    Implements CG-1:
    - Blocks schemes other than http/https.
    - Resolves hostnames and blocks RFC-1918 private IPs / loopback.
    """
    
    ALLOWED_SCHEMES = {"http", "https"}

    @classmethod
    async def validate_url(cls, url: str) -> None:
        """Validate a URL against egress policies.
        Raises EgressError if the URL is not permitted.
        """
        try:
            parsed = urlparse(url)
        except Exception as e:
            raise EgressError(f"Invalid URL format: {e}")

        # Requirement 1: Scheme Allowlist
        scheme = parsed.scheme.lower()
        if scheme not in cls.ALLOWED_SCHEMES:
            raise EgressError(f"Scheme '{scheme}' is not allowed. Only http and https are permitted.")

        hostname = parsed.hostname
        if not hostname:
            raise EgressError("URL does not contain a hostname.")

        # Requirement 2: Private-IP and Loopback Blocking
        loop = asyncio.get_running_loop()
        try:
            addr_info = await asyncio.wait_for(
                loop.getaddrinfo(hostname, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
                timeout=5.0
            )
        except asyncio.TimeoutError:
            raise EgressError(f"DNS resolution for {hostname} timed out.")
        except socket.gaierror as e:
            raise EgressError(f"DNS resolution for {hostname} failed: {e}")
        except Exception as e:
            raise EgressError(f"Error resolving {hostname}: {e}")

        if not addr_info:
            raise EgressError(f"No IP addresses found for {hostname}.")

        for info in addr_info:
            ip_str = info[4][0]
            try:
                ip_obj = ipaddress.ip_address(ip_str)
            except ValueError:
                continue

            if ip_obj.is_loopback:
                raise EgressError(f"Resolved IP {ip_str} for {hostname} is a loopback address.")
            if ip_obj.is_private:
                raise EgressError(f"Resolved IP {ip_str} for {hostname} is a private address.")
            if ip_obj.is_link_local or ip_obj.is_multicast:
                raise EgressError(f"Resolved IP {ip_str} for {hostname} is an invalid address type.")
