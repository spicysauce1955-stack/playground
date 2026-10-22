"""Unit tests for local-libvirt egress isolation (nwfilter) -- no libvirt needed.

``FakeLibvirt`` is an in-memory virsh that models just enough of libvirt
10 (behaviour confirmed live): ``nwfilter-define`` of an existing name
without its UUID is refused, ``update-device`` edits the live or the
persistent interface, ``nwfilter-undefine`` of a referenced filter is
refused, and ``--live`` on a shut-off domain fails.
"""

from __future__ import annotations

import subprocess
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path

import pytest

from playground.backend.local_libvirt import isolation
from playground.backend.local_libvirt.isolation import (
    execute_connect,
    execute_isolate,
    remove_isolation_filter,
)
from playground.backend.local_libvirt.isolation_xml import (
    filter_name,
    filters_equivalent,
    network_gateway_ipv4,
    render_filter_xml,
)
from playground.config.loader import load_config
from playground.config.resolver import resolve_lab
from playground.events import EventBus

REPO_ROOT = Path(__file__).resolve().parents[4]
FILTER = "playground-isolate-generic-infra"
GATEWAYS = {"edge": "10.20.10.1", "lab-private": "10.20.20.1", "routed-a": "10.20.30.1"}


def _cp(rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["virsh"], rc, out, err)


class FakeLibvirt:
    def __init__(self, resolved, *, running: set[str], networks: dict[str, str] | None = None):
        self.nets = dict(GATEWAYS if networks is None else networks)
        self.running = set(running)
        self.live: dict[str, list[ET.Element]] = {}
        self.cfg: dict[str, list[ET.Element]] = {}
        for vm in resolved.vms:
            self.cfg[vm.name] = [self._iface(vm.name, i, n) for i, n in enumerate(vm.networks)]
            if vm.name in self.running:
                self.live[vm.name] = [self._clone(e) for e in self.cfg[vm.name]]
        self.filters: dict[str, str] = {}  # name -> xml (with uuid)
        self.calls: list[list[str]] = []
        self.fail: dict[str, subprocess.CompletedProcess[str]] = {}
        self.prefixes: dict[str, str] = {}
        self.running_list_calls = 0
        self.running_list_hooks: dict[int, object] = {}

    def start(self, vm: str, *, strip_filter: bool = False) -> None:
        """Start a defined domain (as if it booted after `isolate` attached)."""
        self.running.add(vm)
        clones = [self._clone(e) for e in self.cfg[vm]]
        if strip_filter:
            for e in clones:
                for ref in e.findall("filterref"):
                    e.remove(ref)
        self.live[vm] = clones

    @staticmethod
    def _clone(e: ET.Element) -> ET.Element:
        return ET.fromstring(ET.tostring(e))

    @staticmethod
    def _iface(vm: str, i: int, net: str) -> ET.Element:
        mac = f"52:54:00:00:{abs(hash(vm)) % 256:02x}:{i:02x}"
        return ET.fromstring(
            f"<interface type='network'><mac address='{mac}'/>"
            f"<source network='{net}'/><target dev='vnet-{vm}-{i}'/>"
            "<model type='virtio'/></interface>"
        )

    def _domxml(self, vm: str, ifaces: list[ET.Element]) -> str:
        body = "".join(ET.tostring(e, encoding="unicode") for e in ifaces)
        return f"<domain><name>{vm}</name><devices>{body}</devices></domain>"

    def __call__(self, args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        for prefix, result in self.fail.items():
            if " ".join(args).startswith(prefix):
                return result
        cmd = args[0]
        if cmd == "list":
            if "--all" not in args:
                self.running_list_calls += 1
                hook = self.running_list_hooks.get(self.running_list_calls)
                if callable(hook):
                    hook()
            names = list(self.cfg) if "--all" in args else sorted(self.running & set(self.cfg))
            return _cp(out="\n".join(names) + "\n\n")
        if cmd == "net-dumpxml":
            net = args[1]
            if net not in self.nets:
                return _cp(1, err=f"error: failed to get network '{net}'\nerror: Network not found")
            return _cp(
                out=(
                    f"<network><name>{net}</name>"
                    f"<ip address='{self.nets[net]}' "
                    f"prefix='{self.prefixes.get(net, '24')}'/></network>"
                )
            )
        if cmd == "dumpxml":
            vm = args[-1]
            if vm not in self.cfg:
                return _cp(1, err="error: failed to get domain\nerror: Domain not found")
            if "--inactive" in args:
                return _cp(out=self._domxml(vm, self.cfg[vm]))
            return _cp(out=self._domxml(vm, self.live.get(vm, self.cfg[vm])))
        if cmd == "nwfilter-dumpxml":
            xml = self.filters.get(args[1])
            if xml is None:
                return _cp(1, err="error: Network filter not found: no nwfilter with matching name")
            return _cp(out=xml)
        if cmd == "nwfilter-define":
            root = ET.fromstring(Path(args[1]).read_text())
            name = root.get("name", "")
            uuid_el = root.find("uuid")
            if name in self.filters:
                old = ET.fromstring(self.filters[name]).findtext("uuid")
                if uuid_el is None or uuid_el.text != old:
                    return _cp(
                        1, err=f"operation failed: filter '{name}' already exists with uuid {old}"
                    )
            elif uuid_el is None:
                ET.SubElement(root, "uuid").text = f"uuid-{len(self.filters) + 1}"
            self.filters[name] = ET.tostring(root, encoding="unicode")
            return _cp()
        if cmd == "nwfilter-undefine":
            name = args[1]
            if name not in self.filters:
                return _cp(1, err="error: Network filter not found: no nwfilter with matching name")
            if self._referenced(name):
                return _cp(1, err="error: Requested operation is not valid: nwfilter is in use")
            del self.filters[name]
            return _cp()
        if cmd == "update-device":
            return self._update(args)
        if cmd == "nwfilter-binding-dumpxml":
            dev = args[1]
            for ifaces in self.live.values():
                for e in ifaces:
                    t = e.find("target")
                    if t is not None and t.get("dev") == dev:
                        ref = e.find("filterref")
                        if ref is not None:
                            return _cp(
                                out=(
                                    "<filterbinding><filterref filter="
                                    f"'{ref.get('filter')}'/></filterbinding>"
                                )
                            )
            return _cp(1, err="error: Network filter binding not found")
        raise AssertionError(f"unexpected virsh call {args}")

    def _referenced(self, name: str) -> bool:
        return any(
            (e.find("filterref") is not None and e.find("filterref").get("filter") == name)  # type: ignore[union-attr]
            for ifaces in self.live.values()
            for e in ifaces
        )

    def _update(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        vm, path, flag = args[1], args[2], args[3]
        new = ET.fromstring(Path(path).read_text())
        if flag == "--live":
            if vm not in self.running:
                return _cp(1, err="error: Requested operation is not valid: domain is not running")
            store = self.live[vm]
        else:
            store = self.cfg[vm]
        mac = new.find("mac").get("address")  # type: ignore[union-attr]
        for i, e in enumerate(store):
            if e.find("mac").get("address") == mac:  # type: ignore[union-attr]
                store[i] = new
                return _cp(out="Device updated successfully")
        return _cp(1, err="error: no matching interface")

    # -- assertions -------------------------------------------------------
    def filter_refs(self, vm: str, *, live: bool) -> list[str | None]:
        ifaces = self.live[vm] if live else self.cfg[vm]
        return [
            (e.find("filterref").get("filter") if e.find("filterref") is not None else None)  # type: ignore[union-attr]
            for e in ifaces
        ]

    def mutating_calls(self) -> list[list[str]]:
        return [
            c
            for c in self.calls
            if c[0] in ("update-device", "nwfilter-define", "nwfilter-undefine")
        ]


@pytest.fixture
def resolved():
    loaded, diags = load_config(REPO_ROOT / "config")
    assert diags == []
    return resolve_lab(loaded, "generic-infra")


ALL_RUNNING = {"node1", "docker1", "router1"}


def _isolate(resolved, fake, tmp_path):
    return execute_isolate(resolved=resolved, state_dir=tmp_path, bus=EventBus(), virsh=fake)  # type: ignore[call-arg]


def _connect(resolved, fake, tmp_path):
    return execute_connect(resolved=resolved, state_dir=tmp_path, bus=EventBus(), virsh=fake)  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Pure rendering
# ---------------------------------------------------------------------------


def test_filter_name_sanitizes() -> None:
    assert filter_name("fleet") == "playground-isolate-fleet"
    assert filter_name("my lab/x") == "playground-isolate-my-lab-x"


def test_render_filter_rules_and_priorities() -> None:
    xml = render_filter_xml(
        "f", cidrs=["10.0.1.0/24", "10.0.2.5/24"], gateways=["10.0.1.1", "10.0.2.1"]
    )
    root = ET.fromstring(xml)
    assert root.get("chain") == "root"
    rules = [
        (
            r.get("action"),
            r.get("direction"),
            int(r.get("priority", "0")),
            r[0].tag,
            dict(r[0].attrib),
        )
        for r in root.findall("rule")
    ]
    assert all(r[1] == "out" for r in rules)
    prios = [r[2] for r in rules]
    assert prios == sorted(prios)
    dns = [r for r in rules if r[2] == 100]
    assert {(r[4]["dstipaddr"], r[4]["protocol"]) for r in dns} == {
        (g, p) for g in ("10.0.1.1", "10.0.2.1") for p in ("udp", "tcp")
    }
    assert all(r[0] == "drop" and r[4]["dstportstart"] == "53" == r[4]["dstportend"] for r in dns)
    for gw in ("10.0.1.1", "10.0.2.1"):
        assert ("accept", 110, "udp", "67") in {
            (r[0], r[2], r[4]["protocol"], r[4]["dstportstart"])
            for r in rules
            if r[4].get("dstipaddr") == gw and r[2] == 110
        }
        assert any(
            r[0] == "accept"
            and r[2] == 120
            and r[4]["dstipaddr"] == gw
            and r[4]["protocol"] == "tcp"
            and r[4]["srcportstart"] == "22" == r[4]["srcportend"]
            for r in rules
        )
        assert any(
            r[0] == "accept" and r[2] == 130 and r[4] == {"protocol": "icmp", "dstipaddr": gw}
            for r in rules
        )
        assert any(r[0] == "drop" and r[2] == 150 and r[4] == {"dstipaddr": gw} for r in rules)
    lab = [r for r in rules if r[2] == 200]
    assert [(r[0], r[4]["dstipaddr"], r[4]["dstipmask"]) for r in lab] == [
        ("accept", "10.0.1.0", "24"),
        ("accept", "10.0.2.0", "24"),  # host bits normalised
    ]
    dhcp = [r for r in rules if r[2] == 210]
    assert dhcp[0][4] == {
        "protocol": "udp",
        "dstipaddr": "255.255.255.255",
        "dstportstart": "67",
        "dstportend": "67",
    }
    tail = [(r[0], r[3]) for r in rules if r[2] == 900]
    assert tail == [("drop", "ip"), ("drop", "ipv6")]
    assert [r[3] for r in rules if r[2] == 50] == ["arp"]


def _verdict(xml: str, *, dst: str, proto: str, sport: int = 40000, dport: int = 80) -> str:
    """First-match evaluation of the rendered rules (lowest priority first)."""
    import ipaddress

    root = ET.fromstring(xml)
    rules = sorted(root.findall("rule"), key=lambda r: int(r.get("priority", "0")))
    for r in rules:
        el = r[0]
        if el.tag != "ip":
            continue
        a = el.attrib
        if "protocol" in a and a["protocol"] != proto:
            continue
        if "dstipaddr" in a:
            mask = a.get("dstipmask", "32")
            net = ipaddress.ip_network(f"{a['dstipaddr']}/{mask}", strict=False)
            if ipaddress.ip_address(dst) not in net:
                continue
        if "dstportstart" in a and not (int(a["dstportstart"]) <= dport <= int(a["dstportend"])):
            continue
        if "srcportstart" in a and not (int(a["srcportstart"]) <= sport <= int(a["srcportend"])):
            continue
        return str(r.get("action"))
    return "accept"


def test_gateway_is_restricted_before_cidr_accept() -> None:
    xml = render_filter_xml("f", cidrs=["10.0.1.0/24"], gateways=["10.0.1.1"])
    gw = "10.0.1.1"
    # Gateway: only DHCP renew, SSH replies and ICMP.
    assert _verdict(xml, dst=gw, proto="tcp", sport=8080, dport=40000) == "drop"
    assert _verdict(xml, dst=gw, proto="tcp", sport=5672, dport=40000) == "drop"
    assert _verdict(xml, dst=gw, proto="tcp", sport=40000, dport=2377) == "drop"
    assert _verdict(xml, dst=gw, proto="tcp", sport=40000, dport=53) == "drop"
    assert _verdict(xml, dst=gw, proto="udp", sport=68, dport=53) == "drop"
    assert _verdict(xml, dst=gw, proto="tcp", sport=22, dport=51000) == "accept"
    assert _verdict(xml, dst=gw, proto="udp", sport=68, dport=67) == "accept"
    assert _verdict(xml, dst=gw, proto="icmp") == "accept"
    # VM -> VM in the same CIDR on any port is accepted.
    assert _verdict(xml, dst="10.0.1.7", proto="tcp", sport=8080, dport=5672) == "accept"
    assert _verdict(xml, dst="10.0.1.7", proto="udp", dport=53) == "accept"
    # Outside is dropped; DHCP broadcast accepted.
    assert _verdict(xml, dst="8.8.8.8", proto="tcp", dport=443) == "drop"
    assert _verdict(xml, dst="255.255.255.255", proto="udp", sport=68, dport=67) == "accept"


def test_network_and_broadcast_addresses_are_dropped_before_cidr_accept() -> None:
    xml = render_filter_xml("f", cidrs=["10.0.1.0/24", "10.0.2.0/25"], gateways=["10.0.1.1"])
    root = ET.fromstring(xml)
    drops = {r[0].get("dstipaddr") for r in root.findall("rule") if r.get("priority") == "160"}
    assert drops == {"10.0.1.0", "10.0.1.255", "10.0.2.0", "10.0.2.127"}
    assert _verdict(xml, dst="10.0.1.255", proto="udp", dport=5000) == "drop"
    assert _verdict(xml, dst="10.0.1.0", proto="tcp", dport=22) == "drop"
    assert _verdict(xml, dst="10.0.2.127", proto="udp", dport=5000) == "drop"
    assert _verdict(xml, dst="10.0.1.7", proto="udp", dport=5000) == "accept"
    assert _verdict(xml, dst="255.255.255.255", proto="udp", sport=68, dport=67) == "accept"


def test_network_cidr_ipv4_prefix_and_netmask() -> None:
    from playground.backend.local_libvirt.isolation_xml import network_cidr_ipv4

    p = network_cidr_ipv4("<network><ip address='10.1.2.1' prefix='24'/></network>")
    n = network_cidr_ipv4("<network><ip address='10.1.2.1' netmask='255.255.255.0'/></network>")
    assert str(p) == str(n) == "10.1.2.0/24"
    assert network_cidr_ipv4("<network><ip address='10.1.2.1'/></network>") is None


def test_render_rejects_ipv6_cidr() -> None:
    with pytest.raises(ValueError):
        render_filter_xml("f", cidrs=["fd00::/64"], gateways=[])


def test_equivalence_ignores_uuid_and_attr_order() -> None:
    a = render_filter_xml("f", cidrs=["10.0.1.0/24"], gateways=["10.0.1.1"])
    b = render_filter_xml("f", cidrs=["10.0.1.0/24"], gateways=["10.0.1.1"], uuid="abc")
    c = render_filter_xml("f", cidrs=["10.0.9.0/24"], gateways=["10.0.1.1"])
    assert filters_equivalent(a, b)
    assert not filters_equivalent(a, c)


def test_gateway_parse() -> None:
    assert (
        network_gateway_ipv4("<network><ip address='10.1.2.1' prefix='24'/></network>")
        == "10.1.2.1"
    )
    assert network_gateway_ipv4("<network><ip family='ipv6' address='fd00::1'/></network>") is None


# ---------------------------------------------------------------------------
# isolate
# ---------------------------------------------------------------------------


def test_isolate_running_lab_attaches_live_and_config(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded", diags
    assert diags == []
    assert [s.name for s in run.steps] == [
        "inspect-lab",
        "define-nwfilter",
        "attach-filter",
        "verify-isolation",
    ]
    assert all(s.exit_code == 0 and Path(s.log_path).is_file() for s in run.steps)
    for vm in resolved.vms:
        assert fake.filter_refs(vm.name, live=True) == [FILTER] * len(vm.networks)
        assert fake.filter_refs(vm.name, live=False) == [FILTER] * len(vm.networks)
    # Filter carries every lab gateway + cidr.
    xml = fake.filters[FILTER]
    for gw in GATEWAYS.values():
        assert f'dstipaddr="{gw}"' in xml or f"dstipaddr='{gw}'" in xml


def test_isolate_shut_off_domains_config_only(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set())
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded", diags
    assert not any("--live" in c for c in fake.calls)
    for vm in resolved.vms:
        assert fake.filter_refs(vm.name, live=False) == [FILTER] * len(vm.networks)


def test_isolate_mixed_running_and_shut_off(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running={"node1"})
    run, _ = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded"
    live_updates = [c for c in fake.calls if c[0] == "update-device" and c[3] == "--live"]
    assert {c[1] for c in live_updates} == {"node1"}


def test_isolate_twice_is_a_noop(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    fake.calls.clear()
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded" and diags == []
    assert fake.mutating_calls() == []


def test_isolate_completes_partial_state_after_crash(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    # Simulate a crash: one VM's live iface and another's persistent iface lost the ref.
    for e in fake.live["docker1"]:
        for ref in e.findall("filterref"):
            e.remove(ref)
    for ref in fake.cfg["node1"][0].findall("filterref"):
        fake.cfg["node1"][0].remove(ref)
    fake.calls.clear()
    run, _ = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded"
    updates = [(c[1], c[3]) for c in fake.calls if c[0] == "update-device"]
    assert sorted(updates) == sorted([("docker1", "--live")] * 2 + [("node1", "--config")])
    assert fake.filter_refs("docker1", live=True) == [FILTER, FILTER]
    assert fake.filter_refs("node1", live=False) == [FILTER]


def test_isolate_redefines_changed_filter_reusing_uuid(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    old_uuid = ET.fromstring(fake.filters[FILTER]).findtext("uuid")
    fake.nets["edge"] = "10.20.10.254"  # gateway moved -> rules differ
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded", diags
    assert ET.fromstring(fake.filters[FILTER]).findtext("uuid") == old_uuid
    assert "10.20.10.254" in fake.filters[FILTER]


def test_isolate_unchanged_filter_skips_define(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    fake.calls.clear()
    _isolate(resolved, fake, tmp_path)
    assert not any(c[0] == "nwfilter-define" for c in fake.calls)


def test_isolate_extra_interface_on_other_network_fails(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    extra = FakeLibvirt._iface("router1", 9, "default")
    fake.cfg["router1"].append(extra)
    fake.live["router1"].append(FakeLibvirt._clone(extra))
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.non_lab_interface"]
    assert "router1" in diags[0].message and "default" in diags[0].message
    assert fake.mutating_calls() == []  # nothing changed before the failure


@pytest.mark.parametrize("kind", ["bridge", "direct", "user"])
def test_isolate_non_network_interface_type_fails(resolved, tmp_path, kind) -> None:
    fake = FakeLibvirt(resolved, running=set())
    src = {"bridge": "<source bridge='br0'/>", "direct": "<source dev='eth0'/>", "user": ""}[kind]
    fake.cfg["node1"].append(
        ET.fromstring(
            f"<interface type='{kind}'><mac address='52:54:00:aa:bb:cc'/>{src}</interface>"
        )
    )
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.non_lab_interface"
    assert f"type={kind}" in diags[0].message
    assert fake.mutating_calls() == []


def test_isolate_undeclared_domain_on_lab_network_is_isolated_with_warning(
    resolved, tmp_path
) -> None:
    fake = FakeLibvirt(resolved, running={"stowaway"} | ALL_RUNNING)
    ghost = FakeLibvirt._iface("stowaway", 0, "edge")
    fake.cfg["stowaway"] = [ghost]
    fake.live["stowaway"] = [FakeLibvirt._clone(ghost)]
    # An unrelated domain on a non-lab network must be left alone.
    other = FakeLibvirt._iface("bystander", 0, "default")
    fake.cfg["bystander"] = [other]
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded", diags
    assert [(d.id, d.severity) for d in diags] == [("runtime.isolate.undeclared_domain", "warning")]
    assert fake.filter_refs("stowaway", live=True) == [FILTER]
    assert fake.filter_refs("stowaway", live=False) == [FILTER]
    assert fake.filter_refs("bystander", live=False) == [None]
    assert not any(c[0] == "update-device" and c[1] == "bystander" for c in fake.calls)
    log = Path(run.steps[0].log_path).read_text()
    assert "stowaway" in log and "NOT declared" in log


def test_isolate_undeclared_domain_with_foreign_interface_fails(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set())
    fake.cfg["stowaway"] = [
        FakeLibvirt._iface("stowaway", 0, "edge"),
        FakeLibvirt._iface("stowaway", 1, "default"),
    ]
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert any(
        d.id == "runtime.isolate.non_lab_interface" and "stowaway" in d.message for d in diags
    )


def test_connect_detaches_undeclared_domains_too(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running={"stowaway"})
    fake.cfg["stowaway"] = [FakeLibvirt._iface("stowaway", 0, "edge")]
    fake.live["stowaway"] = [FakeLibvirt._clone(fake.cfg["stowaway"][0])]
    assert _isolate(resolved, fake, tmp_path)[0].status == "succeeded"
    assert fake.filter_refs("stowaway", live=True) == [FILTER]
    run, diags = _connect(resolved, fake, tmp_path)
    assert run.status == "succeeded", diags
    assert fake.filter_refs("stowaway", live=True) == [None]
    assert fake.filter_refs("stowaway", live=False) == [None]
    assert FILTER not in fake.filters


def test_isolate_network_drift_fails_and_names_reapply(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    fake.prefixes["edge"] = "16"  # libvirt runs 10.20.10.0/16, config says /24
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.network_drift"]
    assert "re-apply" in (diags[0].suggestion or "")
    assert fake.mutating_calls() == []


def test_isolate_rules_use_libvirt_cidr(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    run, _ = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded"
    root = ET.fromstring(fake.filters[FILTER])
    accepts = {
        (r[0].get("dstipaddr"), r[0].get("dstipmask"))
        for r in root.findall("rule")
        if r.get("priority") == "200"
    }
    assert accepts == {("10.20.10.0", "24"), ("10.20.20.0", "24"), ("10.20.30.0", "24")}


def test_verify_relists_running_domains(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set())
    fake.running_list_hooks[2] = lambda: fake.start("node1")  # starts after attach
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded", diags
    log = Path(run.steps[-1].log_path).read_text()
    assert "node1 live" in log and f"bound to {FILTER}" in log


def test_verify_fails_when_late_started_domain_has_no_binding(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set())
    fake.running_list_hooks[2] = lambda: fake.start("node1", strip_filter=True)
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert {d.id for d in diags} == {"runtime.isolate.verify_failed"}
    assert "node1" in diags[0].message


def test_isolate_replaces_foreign_filter_with_warning(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set())
    ET.SubElement(fake.cfg["node1"][0], "filterref", {"filter": "clean-traffic"})
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "succeeded"
    assert [d.id for d in diags] == ["runtime.isolate.filter_replaced"]
    assert diags[0].severity == "warning"
    assert fake.filter_refs("node1", live=False) == [FILTER]


def test_isolate_missing_domain_is_not_provisioned(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    del fake.cfg["router1"]
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.not_provisioned"
    assert "router1" in diags[0].message
    assert fake.mutating_calls() == []


def test_isolate_missing_network_is_not_provisioned(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    del fake.nets["routed-a"]
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert [d.id for d in diags] == ["runtime.isolate.not_provisioned"]
    assert fake.mutating_calls() == []


def test_isolate_nothing_provisioned(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set(), networks={})
    fake.cfg.clear()
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert {d.id for d in diags} == {"runtime.isolate.not_provisioned"}


def test_isolate_virsh_missing(resolved, tmp_path) -> None:
    def gone(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return _cp(127, err="virsh: command not found")

    run, diags = execute_isolate(resolved=resolved, state_dir=tmp_path, bus=EventBus(), virsh=gone)  # type: ignore[call-arg]
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.virsh_missing"


def test_isolate_virsh_permission_denied(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    fake.fail["list"] = _cp(1, err="error: failed to connect: Permission denied")
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.virsh_failed"
    assert "Permission denied" in diags[0].message
    assert "libvirt group" in (diags[0].suggestion or "")


def test_isolate_define_failure_stops_before_attach(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    fake.fail["nwfilter-define"] = _cp(1, err="error: invalid XML")
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert diags[0].id == "runtime.isolate.nwfilter_define_failed"
    assert [s.name for s in run.steps] == ["inspect-lab", "define-nwfilter"]
    assert not any(c[0] == "update-device" for c in fake.calls)


def test_isolate_attach_failure_is_loud(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    fake.fail["update-device docker1"] = _cp(1, err="error: internal error")
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert any(
        d.id == "runtime.isolate.attach_failed" and "NOT isolated" in (d.suggestion or "")
        for d in diags
    )
    # Other VMs were still attempted.
    assert fake.filter_refs("node1", live=True) == [FILTER]


def test_isolate_verify_catches_missing_live_binding(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    fake.fail["nwfilter-binding-dumpxml"] = _cp(1, err="error: binding not found")
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert {d.id for d in diags} == {"runtime.isolate.verify_failed"}


def test_isolate_vm_without_lab_interface_fails(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set())
    fake.cfg["node1"] = [FakeLibvirt._iface("node1", 0, "default")]
    run, diags = _isolate(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert any(d.id == "runtime.isolate.no_lab_interface" for d in diags)


def test_isolate_writes_run_record_and_events(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    run, _ = _isolate(resolved, fake, tmp_path)
    assert run.operation == "isolate"
    assert (tmp_path / "runs" / run.run_id / "run.json").is_file()
    assert (tmp_path / "runs" / run.run_id / "events.jsonl").is_file()
    assert (tmp_path / "runs" / run.run_id / "logs" / "attach-filter.log").is_file()


def test_isolate_never_raises(resolved, tmp_path) -> None:
    def boom(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        raise RuntimeError("kaboom")

    run, diags = execute_isolate(resolved=resolved, state_dir=tmp_path, bus=EventBus(), virsh=boom)  # type: ignore[call-arg]
    assert run.status == "failed"
    assert any(d.id == "runtime.isolate.internal_error" for d in diags)


# ---------------------------------------------------------------------------
# connect
# ---------------------------------------------------------------------------


def test_connect_reverses_isolate(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    run, diags = _connect(resolved, fake, tmp_path)
    assert run.status == "succeeded", diags
    assert [s.name for s in run.steps] == ["inspect-lab", "detach-filter", "undefine-nwfilter"]
    assert FILTER not in fake.filters
    for vm in resolved.vms:
        assert set(fake.filter_refs(vm.name, live=True)) == {None}
        assert set(fake.filter_refs(vm.name, live=False)) == {None}


def test_connect_on_never_isolated_lab_succeeds(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    run, diags = _connect(resolved, fake, tmp_path)
    assert run.status == "succeeded" and diags == []
    assert fake.mutating_calls() == [["nwfilter-undefine", FILTER]]


def test_connect_twice_is_idempotent(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    _connect(resolved, fake, tmp_path)
    run, diags = _connect(resolved, fake, tmp_path)
    assert run.status == "succeeded" and diags == []


def test_connect_shut_off_domains_config_only(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set())
    _isolate(resolved, fake, tmp_path)
    fake.calls.clear()
    run, _ = _connect(resolved, fake, tmp_path)
    assert run.status == "succeeded"
    assert not any("--live" in c for c in fake.calls)


def test_connect_tolerates_missing_domains(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    for ifaces in (fake.live, fake.cfg):
        ifaces.pop("router1", None)
    run, diags = _connect(resolved, fake, tmp_path)
    assert run.status == "succeeded", diags


def test_connect_leaves_foreign_filters_alone(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=set())
    ET.SubElement(fake.cfg["node1"][0], "filterref", {"filter": "clean-traffic"})
    run, _ = _connect(resolved, fake, tmp_path)
    assert run.status == "succeeded"
    assert fake.filter_refs("node1", live=False) == ["clean-traffic"]


def test_connect_filter_in_use_by_other_domain_fails(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    # A non-lab domain also references it.
    other = ET.fromstring(f"<interface type='network'><filterref filter='{FILTER}'/></interface>")
    fake.live["stranger"] = [other]
    run, diags = _connect(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert diags[0].id == "runtime.connect.filter_in_use"


def test_connect_detach_failure(resolved, tmp_path) -> None:
    fake = FakeLibvirt(resolved, running=ALL_RUNNING)
    _isolate(resolved, fake, tmp_path)
    fake.fail["update-device node1"] = _cp(1, err="error: boom")
    run, diags = _connect(resolved, fake, tmp_path)
    assert run.status == "failed"
    assert any(d.id == "runtime.connect.detach_failed" for d in diags)
    assert FILTER in fake.filters  # not undefined while still referenced


def test_connect_diagnostic_prefix(resolved, tmp_path) -> None:
    def gone(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return _cp(127, err="virsh: command not found")

    run, diags = execute_connect(resolved=resolved, state_dir=tmp_path, bus=EventBus(), virsh=gone)  # type: ignore[call-arg]
    assert run.status == "failed"
    assert diags[0].id == "runtime.connect.virsh_missing"


# ---------------------------------------------------------------------------
# remove_isolation_filter
# ---------------------------------------------------------------------------


def test_remove_filter_absent_is_noop(resolved) -> None:
    fake = FakeLibvirt(resolved, running=set())
    assert remove_isolation_filter("generic-infra", virsh=fake) == []


def test_remove_filter_present(resolved) -> None:
    fake = FakeLibvirt(resolved, running=set())
    fake.filters[FILTER] = "<filter/>"
    assert remove_isolation_filter("generic-infra", virsh=fake) == []
    assert FILTER not in fake.filters


def test_remove_filter_failure_is_warning_only(resolved) -> None:
    fake = FakeLibvirt(resolved, running=set())
    fake.filters[FILTER] = "<filter/>"
    fake.fail["nwfilter-undefine"] = _cp(1, err="error: nwfilter is in use")
    diags = remove_isolation_filter("generic-infra", virsh=fake)
    assert [(d.id, d.severity) for d in diags] == [
        ("runtime.isolate.filter_cleanup_failed", "warning")
    ]


# ---------------------------------------------------------------------------
# default runner
# ---------------------------------------------------------------------------


def test_default_virsh_binds_uri_and_handles_missing_binary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(isolation.shutil, "which", lambda _n: None)
    assert isolation.default_virsh(["list"]).returncode == 127

    monkeypatch.setattr(isolation.shutil, "which", lambda _n: "/usr/bin/virsh")
    seen: list[list[str]] = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return _cp()

    monkeypatch.setattr(isolation.subprocess, "run", fake_run)
    isolation.default_virsh(["list", "--all"])
    assert seen == [["virsh", "--quiet", "-c", "qemu:///system", "list", "--all"]]

    def timeout(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(isolation.subprocess, "run", timeout)
    assert isolation.default_virsh(["list"]).returncode == 124
