"""mDNS advertisement (#19): what devices built without a server URL find."""

from types import SimpleNamespace

from otter import config, mdns


def adapter(name, *ips):
    return SimpleNamespace(nice_name=name, ips=[SimpleNamespace(ip=ip, is_IPv4=isinstance(ip, str)) for ip in ips])


def test_only_lan_addresses_are_advertised(monkeypatch):
    monkeypatch.setattr(mdns.ifaddr, "get_adapters", lambda: [
        adapter("lo", "127.0.0.1"),
        adapter("wlan0", "192.168.1.20", ("fe80::1", 0, 2)),
        adapter("docker0", "172.17.0.1"),
        adapter("veth12ab", "169.254.3.4"),
        adapter("eth0", "10.0.0.5"),
    ])
    assert mdns.lan_addresses() == ["10.0.0.5", "192.168.1.20"]


def test_service_points_at_the_port_or_the_public_url(monkeypatch):
    monkeypatch.setattr(mdns, "lan_addresses", lambda: ["192.168.1.20"])
    monkeypatch.setattr(config, "MDNS_PORT", 8000)
    monkeypatch.setattr(config, "PUBLIC_URL", "")
    info = mdns.service_info()
    assert info.type == "_otter._tcp.local."
    assert (info.parsed_addresses(), info.port, info.properties) == (["192.168.1.20"], 8000, {b"path": b"/"})

    monkeypatch.setattr(config, "PUBLIC_URL", "https://otter.lan")
    info = mdns.service_info()
    assert info.port == 443
    assert info.properties[b"url"] == b"https://otter.lan"
