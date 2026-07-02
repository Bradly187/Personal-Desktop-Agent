import pytest
import asyncio
import socket
from unittest.mock import patch, MagicMock
from core.egress import EgressController, EgressError

@pytest.mark.asyncio
async def test_egress_invalid_scheme():
    with pytest.raises(EgressError, match="Scheme 'file' is not allowed"):
        await EgressController.validate_url("file:///etc/passwd")

@pytest.mark.asyncio
async def test_egress_no_hostname():
    with pytest.raises(EgressError, match="URL does not contain a hostname"):
        await EgressController.validate_url("http://")

@pytest.mark.asyncio
async def test_egress_loopback():
    with pytest.raises(EgressError, match="is a loopback address"):
        await EgressController.validate_url("http://localhost:8770/")
        
    with pytest.raises(EgressError, match="is a loopback address"):
        await EgressController.validate_url("http://127.0.0.1:8770/")

@pytest.mark.asyncio
async def test_egress_private_ip():
    with pytest.raises(EgressError, match="is a private address"):
        await EgressController.validate_url("http://192.168.1.1/")

@pytest.mark.asyncio
async def test_egress_unresolvable_host():
    with pytest.raises(EgressError, match="DNS resolution"):
        await EgressController.validate_url("http://this-domain-surely-does-not-exist.invalid/")

@pytest.mark.asyncio
async def test_egress_allowed_domain():
    # Mock DNS resolution to return a public IP so test passes offline
    with patch("asyncio.events.AbstractEventLoop.getaddrinfo") as mock_getaddrinfo:
        # getaddrinfo returns list of tuples: (family, type, proto, canonname, sockaddr)
        mock_getaddrinfo.return_value = [
            (socket.AF_INET, socket.SOCK_STREAM, 6, '', ('8.8.8.8', 80))
        ]
        # Should not raise any error
        await EgressController.validate_url("http://example.com")
