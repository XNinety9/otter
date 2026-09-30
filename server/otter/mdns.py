"""Advertises the server on the LAN over mDNS (#19), so devices built without a server URL
find it: service type _otter._tcp, with the URL to use in the TXT record when OTTER_PUBLIC_URL
is set (for HTTPS behind a proxy), else the host's addresses and OTTER_MDNS_PORT.

Multicast doesn't leave Docker's default bridge network: run Otter on the host network (or
directly on the host) for this to work.
"""

import logging
import socket
from urllib.parse import urlsplit

import ifaddr
from zeroconf import IPVersion
from zeroconf.asyncio import AsyncServiceInfo, AsyncZeroconf

from . import config

log = logging.getLogger("otter.mdns")

SERVICE_TYPE = "_otter._tcp.local."
# Virtual interfaces devices can't reach.
SKIPPED_INTERFACES = ("lo", "docker", "br-", "veth", "virbr", "tailscale", "wg")


def lan_addresses() -> list[str]:
    return sorted(
        ip.ip
        for adapter in ifaddr.get_adapters()
        if not adapter.nice_name.startswith(SKIPPED_INTERFACES)
        for ip in adapter.ips
        if ip.is_IPv4 and not ip.ip.startswith(("127.", "169.254."))
    )


def service_info() -> AsyncServiceInfo:
    properties = {"path": "/"}
    port = config.MDNS_PORT
    if config.PUBLIC_URL:
        properties["url"] = config.PUBLIC_URL
        url = urlsplit(config.PUBLIC_URL)
        port = url.port or (443 if url.scheme == "https" else 80)
    host = socket.gethostname().split(".")[0]
    return AsyncServiceInfo(
        SERVICE_TYPE,
        f"Otter on {host}.{SERVICE_TYPE}",
        addresses=[socket.inet_aton(a) for a in lan_addresses()],
        port=port,
        properties=properties,
        server=f"{host}.local.",
    )


class Advertiser:
    def __init__(self) -> None:
        self._zeroconf: AsyncZeroconf | None = None
        self._info: AsyncServiceInfo | None = None

    async def start(self) -> None:
        if not config.MDNS:
            return
        try:
            self._info = service_info()
            self._zeroconf = AsyncZeroconf(ip_version=IPVersion.V4Only)
            await self._zeroconf.async_register_service(self._info, allow_name_change=True)
            addresses = ", ".join(self._info.parsed_addresses()) or "no LAN address"
            log.info("advertised over mDNS as %r (%s, port %d)", self._info.name, addresses, self._info.port)
        except Exception:  # discovery is a convenience: never keep the server from starting
            log.exception("mDNS advertisement failed")
            await self.stop()

    async def stop(self) -> None:
        if self._zeroconf is None:
            return
        if self._info is not None:
            await self._zeroconf.async_unregister_service(self._info)
        await self._zeroconf.async_close()
        self._zeroconf = self._info = None


advertiser = Advertiser()
