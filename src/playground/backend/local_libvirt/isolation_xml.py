"""Pure XML helpers for local-libvirt egress isolation.

Everything here is side-effect free: render the lab's nwfilter, compare
two filter definitions, and read / rewrite ``<interface>`` elements of a
domain XML. The orchestration (virsh calls, steps, diagnostics) lives in
:mod:`playground.backend.local_libvirt.isolation`.

The filter works at the ebtables layer only (``<ip>`` / ``<ipv6>`` /
``<arp>`` rules in the ``root`` chain), because iptables-level nwfilter
rules need ``br_netfilter`` which is not guaranteed on the host. The
price is that libvirt can only *drop* at this layer, never reject.
Direction ``out`` is traffic sent BY the guest.

Consequence for host->VM traffic: the host is reachable at each lab
gateway IP and runs services of its own, so a guest may only talk to its
gateway for DHCP renewal, ICMP, and SSH replies (tcp source port 22).
Connections the host opens to a VM on any other port get no reply
(the guest's SYN-ACK is dropped), so e.g. curling a VM port directly from
the host does not work while isolated; use an SSH tunnel instead.
"""

from __future__ import annotations

import ipaddress
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

FILTER_PREFIX = "playground-isolate-"

_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")

# Rule priorities (lower runs first). Gaps are deliberate so a future
# rule can slot in without renumbering.
PRIO_ARP = 50
PRIO_DNS_DROP = 100
PRIO_GW_DHCP = 110
PRIO_GW_SSH_REPLY = 120
PRIO_GW_ICMP = 130
PRIO_GW_DROP = 150
PRIO_NET_BCAST_DROP = 160
PRIO_LAB_ACCEPT = 200
PRIO_DHCP_ACCEPT = 210
PRIO_DROP_REST = 900


def filter_name(lab_name: str) -> str:
    """``playground-isolate-<lab>`` with characters libvirt may dislike replaced."""
    return FILTER_PREFIX + _UNSAFE.sub("-", lab_name)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def parse_ipv4_network(cidr: str) -> ipaddress.IPv4Network:
    """Parse *cidr* as an IPv4 network (host bits tolerated); ValueError otherwise."""
    net = ipaddress.ip_network(cidr, strict=False)
    if not isinstance(net, ipaddress.IPv4Network):
        raise ValueError(f"{cidr!r} is not an IPv4 network")
    return net


def render_filter_xml(
    name: str,
    *,
    cidrs: list[str],
    gateways: list[str],
    uuid: str | None = None,
) -> str:
    """Render the isolation nwfilter.

    * ARP accepted (prio 50) so address resolution never depends on the
      chain's default policy;
    * the host answers on every lab gateway IP, so guest->gateway traffic
      is restricted statelessly. All of these sort BEFORE the CIDR accept
      because the gateway lies inside the CIDR:
      prio 100 drop tcp+udp port 53 (dnsmasq forwards upstream; redundant
      with the gateway drop, kept for readability), prio 110 accept udp
      dport 67 (unicast DHCP renew), prio 120 accept tcp sport 22 (replies
      to host-initiated SSH), prio 130 accept icmp, prio 150 drop all else;
    * prio 160: drop the network and broadcast address of each lab CIDR;
    * prio 200: accept IPv4 to each lab CIDR;
    * prio 210: accept the DHCP broadcast (discover / rebind);
    * prio 900: drop every other IPv4 and all IPv6.
    """
    filt = ET.Element("filter", {"name": name, "chain": "root"})
    if uuid:
        ET.SubElement(filt, "uuid").text = uuid

    def rule(action: str, priority: int, proto: str, attrs: dict[str, str]) -> None:
        r = ET.SubElement(
            filt,
            "rule",
            {"action": action, "direction": "out", "priority": str(priority)},
        )
        ET.SubElement(r, proto, attrs)

    rule("accept", PRIO_ARP, "arp", {})
    gws = _dedupe(gateways)
    for gw in gws:
        for l4 in ("udp", "tcp"):
            rule(
                "drop",
                PRIO_DNS_DROP,
                "ip",
                {
                    "protocol": l4,
                    "dstipaddr": gw,
                    "dstportstart": "53",
                    "dstportend": "53",
                },
            )
    for gw in gws:
        rule(
            "accept",
            PRIO_GW_DHCP,
            "ip",
            {"protocol": "udp", "dstipaddr": gw, "dstportstart": "67", "dstportend": "67"},
        )
    for gw in gws:
        rule(
            "accept",
            PRIO_GW_SSH_REPLY,
            "ip",
            {"protocol": "tcp", "dstipaddr": gw, "srcportstart": "22", "srcportend": "22"},
        )
    for gw in gws:
        rule("accept", PRIO_GW_ICMP, "ip", {"protocol": "icmp", "dstipaddr": gw})
    for gw in gws:
        rule("drop", PRIO_GW_DROP, "ip", {"dstipaddr": gw})
    nets = [ipaddress.IPv4Network(c) for c in _dedupe([str(parse_ipv4_network(c)) for c in cidrs])]
    # Subnet-directed broadcast / network address would otherwise be
    # accepted by the CIDR rule below and reach host UDP sockets bound to
    # 0.0.0.0, skipping the gateway drop. (/31 and /32 have no such pair.)
    for net in nets:
        if net.prefixlen < 31:
            for addr in (net.network_address, net.broadcast_address):
                rule("drop", PRIO_NET_BCAST_DROP, "ip", {"dstipaddr": str(addr)})
    for net in nets:
        rule(
            "accept",
            PRIO_LAB_ACCEPT,
            "ip",
            {"dstipaddr": str(net.network_address), "dstipmask": str(net.prefixlen)},
        )
    rule(
        "accept",
        PRIO_DHCP_ACCEPT,
        "ip",
        {
            "protocol": "udp",
            "dstipaddr": "255.255.255.255",
            "dstportstart": "67",
            "dstportend": "67",
        },
    )
    rule("drop", PRIO_DROP_REST, "ip", {})
    rule("drop", PRIO_DROP_REST, "ipv6", {})
    ET.indent(filt)
    return ET.tostring(filt, encoding="unicode") + "\n"


def filter_uuid(filter_xml: str) -> str | None:
    """The ``<uuid>`` of a filter dump, or ``None``."""
    root = ET.fromstring(filter_xml)
    node = root.find("uuid")
    return node.text.strip() if node is not None and node.text else None


def _canonical_filter(filter_xml: str) -> tuple[tuple[tuple[str, str], ...], list[str]]:
    root = ET.fromstring(filter_xml)
    head = tuple(sorted(root.attrib.items()))
    rules = sorted(
        repr(
            (
                tuple(sorted(rule.attrib.items())),
                [(c.tag, tuple(sorted(c.attrib.items()))) for c in rule],
            )
        )
        for rule in root.findall("rule")
    )
    return head, rules


def filters_equivalent(a: str, b: str) -> bool:
    """True when two filter XMLs define the same rules (UUID and order ignored)."""
    return _canonical_filter(a) == _canonical_filter(b)


def network_cidr_ipv4(network_xml: str) -> ipaddress.IPv4Network | None:
    """The IPv4 subnet libvirt actually runs (``<ip address= prefix=|netmask=>``)."""
    root = ET.fromstring(network_xml)
    for ip in root.findall("ip"):
        addr = ip.get("address")
        if ip.get("family", "ipv4") != "ipv4" or not addr:
            continue
        mask = ip.get("prefix") or ip.get("netmask")
        if not mask:
            return None
        try:
            return ipaddress.IPv4Network(f"{addr}/{mask}", strict=False)
        except ValueError:
            return None
    return None


def network_gateway_ipv4(network_xml: str) -> str | None:
    """The IPv4 ``<ip address=...>`` of a libvirt network dump (its gateway)."""
    root = ET.fromstring(network_xml)
    for ip in root.findall("ip"):
        family = ip.get("family", "ipv4")
        addr = ip.get("address")
        if family == "ipv4" and addr:
            return addr
    return None


@dataclass(frozen=True)
class DomainInterface:
    """One ``<interface type='network'>`` of a domain XML."""

    mac: str | None
    network: str
    target_dev: str | None
    filter: str | None
    element: ET.Element


def network_interfaces(domain_xml: str) -> list[DomainInterface]:
    """Every ``type='network'`` interface of *domain_xml*."""
    root = ET.fromstring(domain_xml)
    out: list[DomainInterface] = []
    for iface in root.findall("./devices/interface"):
        if iface.get("type") != "network":
            continue
        source = iface.find("source")
        if source is None or not source.get("network"):
            continue
        mac = iface.find("mac")
        target = iface.find("target")
        ref = iface.find("filterref")
        out.append(
            DomainInterface(
                mac=mac.get("address") if mac is not None else None,
                network=source.get("network", ""),
                target_dev=target.get("dev") if target is not None else None,
                filter=ref.get("filter") if ref is not None else None,
                element=iface,
            )
        )
    return out


def interfaces_on(domain_xml: str, networks: set[str]) -> list[DomainInterface]:
    """Network interfaces attached to one of *networks*."""
    return [i for i in network_interfaces(domain_xml) if i.network in networks]


def interface_xml_with_filter(iface: DomainInterface, filter_: str) -> str:
    """Interface XML carrying exactly one ``<filterref filter=filter_>``."""
    el = ET.fromstring(ET.tostring(iface.element, encoding="unicode"))
    for old in el.findall("filterref"):
        el.remove(old)
    ET.SubElement(el, "filterref", {"filter": filter_})
    ET.indent(el)
    return ET.tostring(el, encoding="unicode") + "\n"


def interface_xml_without_filter(iface: DomainInterface) -> str:
    """Interface XML with every ``<filterref>`` removed."""
    el = ET.fromstring(ET.tostring(iface.element, encoding="unicode"))
    for old in el.findall("filterref"):
        el.remove(old)
    ET.indent(el)
    return ET.tostring(el, encoding="unicode") + "\n"


def binding_filter(binding_xml: str) -> str | None:
    """The filter name in an ``nwfilter-binding-dumpxml`` result."""
    ref = ET.fromstring(binding_xml).find("filterref")
    return ref.get("filter") if ref is not None else None


def foreign_attachments(domain_xml: str, networks: set[str]) -> list[str]:
    """Interfaces NOT attached to one of *networks* (each is an unfiltered path).

    Covers other libvirt networks and every non-``network`` type
    (bridge, direct, user, ethernet, ...), as short human labels.
    """
    root = ET.fromstring(domain_xml)
    out: list[str] = []
    for iface in root.findall("./devices/interface"):
        kind = iface.get("type", "?")
        source = iface.find("source")
        if kind == "network" and source is not None and source.get("network") in networks:
            continue
        where = ""
        if source is not None:
            attrs = source.attrib
            where = next((attrs[k] for k in ("network", "bridge", "dev", "path") if k in attrs), "")
        mac = iface.find("mac")
        mac_s = mac.get("address", "?") if mac is not None else "?"
        out.append(f"{mac_s} (type={kind}{', source=' + where if where else ''})")
    return out
