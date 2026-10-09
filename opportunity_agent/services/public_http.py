from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

import requests


def public_addresses(url):
    parsed = urlsplit(url or '')
    if (
        parsed.scheme not in {'http', 'https'}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.hostname.lower().endswith(('.localhost', '.local'))
    ):
        raise ValueError('URL must be a public HTTP or HTTPS URL.')
    try:
        literal = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        try:
            records = socket.getaddrinfo(
                parsed.hostname,
                parsed.port or (443 if parsed.scheme == 'https' else 80),
            )
        except OSError as exc:
            raise ValueError('URL hostname could not be resolved.') from exc
        addresses = {ipaddress.ip_address(record[4][0]) for record in records}
    else:
        addresses = {literal}
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError('URL must resolve only to public IP addresses.')
    return addresses


def validate_response_peer(response, expected_addresses):
    if not isinstance(response, requests.Response):
        return
    raw = getattr(response, 'raw', None)
    connection = getattr(raw, '_connection', None) if raw is not None else None
    peer_socket = getattr(connection, 'sock', None)
    if not isinstance(peer_socket, socket.socket):
        raise ValueError('Could not verify the public HTTP connection destination.')
    try:
        peer_address = ipaddress.ip_address(peer_socket.getpeername()[0])
    except (OSError, ValueError, IndexError, TypeError) as exc:
        raise ValueError('Could not verify the public HTTP connection destination.') from exc
    if not peer_address.is_global or peer_address not in expected_addresses:
        raise ValueError('HTTP connection resolved to an unapproved destination.')


def get_public_response(url, *, headers=None, timeout=15, params=None):
    expected_addresses = public_addresses(url)
    response = requests.get(
        url,
        headers=headers,
        timeout=timeout,
        params=params,
        allow_redirects=False,
        stream=True,
    )
    try:
        status_code = getattr(response, 'status_code', None)
        if isinstance(status_code, int) and 300 <= status_code < 400:
            raise ValueError('Public URL redirects; configure the final URL directly.')
        validate_response_peer(response, expected_addresses)
        response.raise_for_status()
    except Exception:
        response.close()
        raise
    return response
