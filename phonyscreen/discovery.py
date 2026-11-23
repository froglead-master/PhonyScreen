"""Find the paired iPhone on the local network without Bonjour.

Bonjour/mDNS discovery is often blocked (routers, Windows firewall, network profiles). Instead:
1. try the last known address,
2. otherwise probe every address on the laptop's local /24 networks for the iPhone's
   lockdown port (62078), in parallel,
3. confirm each responder is *this* phone by connecting with its pair record
   (`create_using_mobdev2(host, udid)` only succeeds for the paired device with that UDID).
"""

from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import logging
from typing import Iterable, Optional

logger = logging.getLogger("phonyscreen.discovery")

LOCKDOWN_PORT = 62078
PROBE_TIMEOUT = 0.8
MAX_PARALLEL = 128


def local_subnets() -> list[ipaddress.IPv4Network]:
    """The /24 networks around each private IPv4 address of this machine."""
    import ifaddr

    nets: list[ipaddress.IPv4Network] = []
    for adapter in ifaddr.get_adapters():
        for ip in adapter.ips:
            if not isinstance(ip.ip, str):
                continue  # IPv6 entries are tuples
            addr = ipaddress.IPv4Address(ip.ip)
            if not addr.is_private or addr.is_loopback or addr.is_link_local:
                continue
            prefix = max(24, ip.network_prefix or 24)  # never scan more than 254 hosts per adapter
            net = ipaddress.IPv4Network(f"{addr}/{prefix}", strict=False)
            if net not in nets:
                nets.append(net)
    return nets


async def port_open(host: str, port: int = LOCKDOWN_PORT, timeout: float = PROBE_TIMEOUT) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


async def responders(hosts: Iterable[str]) -> list[str]:
    sem = asyncio.Semaphore(MAX_PARALLEL)

    async def probe(h: str) -> Optional[str]:
        async with sem:
            return h if await port_open(h) else None

    found = await asyncio.gather(*(probe(h) for h in hosts))
    return [h for h in found if h]


async def is_my_phone(host: str, udid: Optional[str]) -> Optional[str]:
    """Return the phone's UDID if ``host`` is a paired iPhone (and ``udid`` when one is given)."""
    from pymobiledevice3.lockdown import create_using_mobdev2

    try:
        lockdown = await asyncio.wait_for(create_using_mobdev2(hostname=host, udid=udid), 8)
    except Exception as e:
        logger.debug("%s is not the phone: %s", host, e)
        return None
    try:
        return lockdown.udid
    finally:
        with contextlib.suppress(Exception):
            await lockdown.close()


async def find_phone_async(udid: Optional[str], last_ip: Optional[str]) -> Optional[tuple[str, str]]:
    """Return ``(ip, udid)`` of the paired phone, or None."""
    if last_ip and await port_open(last_ip, timeout=1.5):
        found = await is_my_phone(last_ip, udid)
        if found:
            return last_ip, found
    hosts: list[str] = []
    for net in local_subnets():
        hosts.extend(str(h) for h in net.hosts())
    hosts = [h for h in dict.fromkeys(hosts) if h != last_ip]
    logger.info("scanning %d addresses for the iPhone", len(hosts))
    for host in await responders(hosts):
        found = await is_my_phone(host, udid)
        if found:
            return host, found
    return None


def find_phone(udid: Optional[str], last_ip: Optional[str]) -> Optional[tuple[str, str]]:
    return asyncio.run(find_phone_async(udid, last_ip))
