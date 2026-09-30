#!/usr/bin/env python3
"""Set the Wi-Fi network of a device over its serial port (Improv Wi-Fi, serial version 1),
without recompiling it. Browsers can do the same with ESP Web Tools.

    uv run --with pyserial tools/improv.py /dev/ttyACM0 --state
    uv run --with pyserial tools/improv.py /dev/ttyACM0 --info
    uv run --with pyserial tools/improv.py /dev/ttyACM0 --scan
    uv run --with pyserial tools/improv.py /dev/ttyACM0 --ssid MyNetwork   # asks for the password

The password comes from --password, $WIFI_PASS, or a prompt.
"""

import argparse
import getpass
import os
import sys
import time

import serial

STATES = {2: "ready (no network)", 3: "provisioning", 4: "provisioned"}
ERRORS = {1: "invalid packet", 2: "unknown command", 3: "unable to connect", 0xFF: "unknown error"}
TYPE_STATE, TYPE_ERROR, TYPE_RPC, TYPE_RESULT = 1, 2, 3, 4
WIFI_SETTINGS, GET_STATE, GET_INFO, SCAN = 1, 2, 3, 4


def packet(kind: int, data: bytes) -> bytes:
    body = b"IMPROV" + bytes([1, kind, len(data)]) + data
    return body + bytes([sum(body) & 0xFF]) + b"\n"


def rpc(command: int, data: bytes = b"") -> bytes:
    return packet(TYPE_RPC, bytes([command, len(data)]) + data)


def strings(data: bytes) -> list[str]:
    """The strings of an RPC result, after its command and length bytes."""
    out, i = [], 2
    while i < len(data):
        n = data[i]
        out.append(data[i + 1 : i + 1 + n].decode(errors="replace"))
        i += 1 + n
    return out


def packets(port: serial.Serial, timeout: float):
    """Improv packets in what the device sends; its log lines are skipped."""
    buffer = b""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        buffer += port.read(port.in_waiting or 1)
        while (start := buffer.find(b"IMPROV")) >= 0 and len(buffer) >= start + 9:
            length = buffer[start + 8]
            if len(buffer) < start + 10 + length:
                break
            frame = buffer[start : start + 10 + length]
            buffer = buffer[start + 10 + length :]
            if sum(frame[:-1]) & 0xFF == frame[-1]:
                yield frame[7], frame[9 : 9 + length]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("port")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--state", action="store_true", help="is it on a network?")
    action.add_argument("--info", action="store_true", help="firmware, version, chip, name")
    action.add_argument("--scan", action="store_true", help="the networks it sees")
    action.add_argument("--ssid", help="the network to join")
    parser.add_argument("--password", help="default: $WIFI_PASS, or asked")
    args = parser.parse_args()

    # Opening the port doesn't reset the board as long as DTR and RTS stay as they are.
    port = serial.Serial(args.port, 115200, timeout=0.2)
    if args.ssid:
        password = args.password or os.environ.get("WIFI_PASS") or getpass.getpass("password: ")
        ssid_b, pass_b = args.ssid.encode(), password.encode()
        port.write(rpc(WIFI_SETTINGS, bytes([len(ssid_b)]) + ssid_b + bytes([len(pass_b)]) + pass_b))
        timeout = 40
    else:
        port.write(rpc(GET_STATE if args.state else GET_INFO if args.info else SCAN))
        timeout = 15 if args.scan else 5

    for kind, data in packets(port, timeout):
        if kind == TYPE_STATE:
            print("state:", STATES.get(data[0], data[0]))
            if data[0] == 2 and (args.state or args.ssid):
                if args.state:
                    return 0
        elif kind == TYPE_ERROR:
            if data[0]:
                print("error:", ERRORS.get(data[0], data[0]))
                return 1
        elif kind == TYPE_RESULT:
            values = strings(data)
            if data[0] == GET_INFO:
                print("firmware: {} {}\nchip: {}\nname: {}".format(*values))
                return 0
            if data[0] == SCAN:
                if not values:
                    return 0
                print(f"{values[1]:>4} dBm  {'secured' if values[2] == 'YES' else 'open   '}  {values[0]}")
            elif data[0] in (WIFI_SETTINGS, GET_STATE):
                if values:
                    print("dashboard:", values[0])
                return 0
    print("no answer: is it running a firmware with Improv?", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
