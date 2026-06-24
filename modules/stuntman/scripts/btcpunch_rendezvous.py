#!/usr/bin/env python3
# Copyright (c) 2026-present The Bitcoin Core developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or http://www.opensource.org/licenses/mit-license.php.
"""Tiny UDP rendezvous server for btcpunch TCP experiments."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import select
import socket
import sys
import time
from typing import Any, Optional


DEFAULT_BIND = "0.0.0.0:3479"
DEFAULT_SESSION = "btcpunch"
JSON_MAGIC = "btcpunch-udp-v1"

Address = tuple[Any, ...]


def log(message: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)


def parse_host_port(value: str) -> tuple[str, int]:
    if value.startswith("["):
        end = value.find("]")
        if end == -1 or len(value) <= end + 2 or value[end + 1] != ":":
            raise argparse.ArgumentTypeError(f"invalid endpoint: {value}")
        host = value[1:end]
        port_text = value[end + 2 :]
    else:
        if ":" not in value:
            raise argparse.ArgumentTypeError(f"missing port in endpoint: {value}")
        host, port_text = value.rsplit(":", 1)

    try:
        port = int(port_text)
    except ValueError as err:
        raise argparse.ArgumentTypeError(f"invalid port in endpoint: {value}") from err

    if port < 0 or port > 65535:
        raise argparse.ArgumentTypeError(f"port out of range in endpoint: {value}")

    return host, port


def resolve_endpoint(value: str) -> Address:
    host, port = parse_host_port(value)
    infos = socket.getaddrinfo(host, port, socket.AF_UNSPEC, socket.SOCK_DGRAM)
    if not infos:
        raise OSError(f"could not resolve {value}")
    return infos[0][4]


def short_addr(addr: Address) -> str:
    host, port = addr[0], addr[1]
    if ":" in host and not host.startswith("["):
        return f"[{host}]:{port}"
    return f"{host}:{port}"


def make_udp_socket(bind_value: str) -> socket.socket:
    bind_addr = resolve_endpoint(bind_value)
    family = socket.AF_INET6 if len(bind_addr) == 4 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    sock.bind(bind_addr)
    return sock


def encode_json(message: dict[str, Any]) -> bytes:
    message = {"magic": JSON_MAGIC, **message}
    return json.dumps(message, separators=(",", ":"), sort_keys=True).encode()


def decode_json(data: bytes) -> Optional[dict[str, Any]]:
    try:
        message = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(message, dict) or message.get("magic") != JSON_MAGIC:
        return None
    return message


def send_json(sock: socket.socket, addr: Address, message: dict[str, Any]) -> None:
    sock.sendto(encode_json(message), addr)


@dataclass
class RendezvousClient:
    addr: Address
    name: str
    want: Optional[str]
    tcp_port: int
    last_seen: float


def find_target(
    name: str,
    client: RendezvousClient,
    clients: dict[str, RendezvousClient],
) -> Optional[RendezvousClient]:
    if client.want:
        return clients.get(client.want)
    for other_name, other in clients.items():
        if other_name != name:
            return other
    return None


def run(args: argparse.Namespace) -> int:
    sock = make_udp_socket(args.bind)
    sock.setblocking(False)
    log(f"rendezvous bound UDP {short_addr(sock.getsockname())}")

    sessions: dict[str, dict[str, RendezvousClient]] = {}
    end_at = time.monotonic() + args.duration if args.duration > 0 else None

    while True:
        now = time.monotonic()
        if end_at is not None and now >= end_at:
            return 0

        timeout = 0.2
        if end_at is not None:
            timeout = max(0.0, min(timeout, end_at - now))
        readable, _writable, _errored = select.select([sock], [], [], timeout)
        if not readable:
            continue

        try:
            data, addr = sock.recvfrom(2048)
        except BlockingIOError:
            continue

        message = decode_json(data)
        if message is None or message.get("type") != "register":
            continue

        session = str(message.get("session", DEFAULT_SESSION))
        name = str(message.get("name", short_addr(addr)))
        want = message.get("want")
        if want is not None:
            want = str(want)
        tcp_port = message.get("tcp_port")
        if not isinstance(tcp_port, int) or tcp_port < 0 or tcp_port > 65535:
            continue

        clients = sessions.setdefault(session, {})
        clients[name] = RendezvousClient(
            addr=addr,
            name=name,
            want=want,
            tcp_port=tcp_port,
            last_seen=now,
        )
        for client_name in list(clients):
            if now - clients[client_name].last_seen > args.expire:
                del clients[client_name]

        send_json(
            sock,
            addr,
            {
                "type": "observed",
                "session": session,
                "addr": [addr[0], addr[1]],
            },
        )

        client = clients[name]
        target = find_target(name, client, clients)
        if target is None:
            continue

        send_json(
            sock,
            client.addr,
            {
                "type": "peer",
                "session": session,
                "peer": {
                    "name": target.name,
                    "addr": [target.addr[0], target.tcp_port],
                },
                "start_delay_ms": args.start_delay_ms,
            },
        )
        send_json(
            sock,
            target.addr,
            {
                "type": "peer",
                "session": session,
                "peer": {
                    "name": client.name,
                    "addr": [client.addr[0], client.tcp_port],
                },
                "start_delay_ms": args.start_delay_ms,
            },
        )
        log(
            f"paired {session}: {client.name} {short_addr(client.addr)} "
            f"<-> {target.name} {short_addr(target.addr)}"
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bind",
        default=DEFAULT_BIND,
        help=f"local UDP bind endpoint (default: {DEFAULT_BIND})",
    )
    parser.add_argument(
        "--expire",
        type=float,
        default=30.0,
        help="seconds before an inactive peer expires",
    )
    parser.add_argument(
        "--start-delay-ms",
        type=int,
        default=500,
        help="suggested punch delay sent to peers",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=0.0,
        help="exit after this many seconds; 0 means run forever",
    )
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        log("interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main())
