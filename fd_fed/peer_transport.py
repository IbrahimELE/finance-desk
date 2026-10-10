"""SSRF-safe transport for Common federation's outbound requests.

Common accepts peer locations from signed and unsigned protocol data.  Every
outbound Common request therefore crosses this one boundary: resolve and
validate with the platform's canonical policy, connect to that exact address,
preserve the original Host/SNI identity, reject redirects, ignore ambient
proxies, and cap the response before buffering it.
"""

from __future__ import annotations

import asyncio
import json as json_module
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

import httpx


class HTTPException(Exception):
  """Stand-in for fastapi.HTTPException (this package has no FastAPI): a refused peer address."""

  def __init__(self, status_code: int, detail: str):
    super().__init__(detail)
    self.status_code, self.detail = status_code, detail

DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_RESOLVER_MAX_BYTES = 64 * 1024
_RESOLVER_HELPER = Path(__file__).with_name("dns_resolver.py")


class FederationTransportError(Exception):
  """A peer response violated the bounded federation transport contract."""


def _resolver_command(url: str) -> tuple[str, ...]:
  return (sys.executable, str(_RESOLVER_HELPER), url)


async def _resolve_url_safe(url: str) -> tuple[list[str], str, str]:
  """Resolve in a killable child so the request deadline owns DNS too."""
  process = await asyncio.create_subprocess_exec(
    *_resolver_command(url),
    stdin=asyncio.subprocess.DEVNULL,
    stdout=asyncio.subprocess.PIPE,
    stderr=asyncio.subprocess.DEVNULL,
  )
  try:
    stdout, _stderr = await process.communicate()
  except BaseException:
    if process.returncode is None:
      try:
        process.kill()
      except ProcessLookupError:
        pass
    await process.wait()
    raise
  if process.returncode != 0 or len(stdout) > _RESOLVER_MAX_BYTES:
    raise FederationTransportError("Peer host resolution failed.")
  try:
    payload = json_module.loads(stdout)
  except (UnicodeDecodeError, json_module.JSONDecodeError) as exc:
    raise FederationTransportError("Peer host resolution failed.") from exc
  if not isinstance(payload, dict):
    raise FederationTransportError("Peer host resolution failed.")
  if payload.get("ok") is not True:
    status = payload.get("status")
    detail = payload.get("detail")
    if not isinstance(status, int) or not isinstance(detail, str):
      raise FederationTransportError("Peer host resolution failed.")
    raise HTTPException(status, detail)
  pinned_urls = payload.get("pinned_urls")
  host_header = payload.get("host_header")
  sni_host = payload.get("sni_host")
  if (
    not isinstance(pinned_urls, list)
    or not pinned_urls
    or any(not isinstance(item, str) for item in pinned_urls)
    or not isinstance(host_header, str)
    or not isinstance(sni_host, str)
  ):
    raise FederationTransportError("Peer host resolution failed.")
  return pinned_urls, host_header, sni_host


def _json_object(body: bytes, content_type: str) -> None:
  media_type = content_type.split(";", 1)[0].strip().lower()
  if media_type != "application/json" and not media_type.endswith("+json"):
    raise FederationTransportError("Peer response is not JSON.")
  try:
    value = json_module.loads(body)
  except (UnicodeDecodeError, json_module.JSONDecodeError) as exc:
    raise FederationTransportError("Peer response contains invalid JSON.") from exc
  if not isinstance(value, dict):
    raise FederationTransportError("Peer response must be a JSON object.")


async def federation_request(
  method: str,
  url: str,
  *,
  json: Any = None,
  params: Mapping[str, Any] | None = None,
  max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
  response_format: Literal["json", "binary"] = "json",
  timeout_seconds: float = 10.0,
) -> httpx.Response:
  """Send one bounded request to a validated, DNS-pinned public URL.

  Redirect responses are failures rather than a second implicit request.  The
  caller may intentionally make another call, but it must pass through this
  function and validation again.  JSON responses are syntax-, media-type-,
  and top-level-shape checked before they leave the transport boundary.
  """
  if max_response_bytes < 1:
    raise ValueError("max_response_bytes must be positive")
  if response_format not in ("json", "binary"):
    raise ValueError("response_format must be 'json' or 'binary'")
  if timeout_seconds <= 0:
    raise ValueError("timeout_seconds must be positive")

  original_url = str(httpx.URL(url, params=params)) if params else url
  # This request object is only the safe, unpinned URL attached to the returned
  # response and any deadline error for status reporting. Do not serialize a
  # potentially large envelope twice; the actual request below owns its body.
  public_request = httpx.Request(method, original_url)
  try:
    # HTTPX timeouts bound individual network phases. This outer deadline also
    # covers DNS validation, retries across pinned addresses, the complete
    # response stream, and response validation as one operation.
    async with asyncio.timeout(timeout_seconds):
      return await _request_within_deadline(
        method,
        original_url,
        public_request,
        json=json,
        max_response_bytes=max_response_bytes,
        response_format=response_format,
        timeout_seconds=timeout_seconds,
      )
  except TimeoutError as exc:
    raise httpx.TimeoutException(
      "Federation request exceeded its deadline.", request=public_request,
    ) from exc


async def _request_within_deadline(
  method: str,
  original_url: str,
  public_request: httpx.Request,
  *,
  json: Any,
  max_response_bytes: int,
  response_format: Literal["json", "binary"],
  timeout_seconds: float,
) -> httpx.Response:
  # getaddrinfo is blocking and a cancelled default-executor thread delays
  # asyncio.run() shutdown. The killable helper makes the outer deadline bound
  # both the coroutine and this short-lived service process.
  pinned_urls, host_header, sni_host = await _resolve_url_safe(original_url)

  async with httpx.AsyncClient(
    follow_redirects=False,
    timeout=timeout_seconds,
    trust_env=False,
  ) as client:
    # Each pinned URL targets one validated address (IPv4 first, IPv6 fallback).
    # A connection failure means no response was received, so it is safe to try
    # the next validated address; only when every candidate fails to connect do
    # we surface the error. This is what lets a peer stay reachable when DNS
    # hands back an address on a family this container cannot egress.
    upstream = None
    connect_error: Exception | None = None
    for pinned_url in pinned_urls:
      request = client.build_request(method, pinned_url, json=json)
      request.headers["host"] = host_header
      request.extensions["sni_hostname"] = sni_host
      try:
        upstream = await client.send(request, stream=True)
        break
      except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
        connect_error = exc
        continue
    if upstream is None:
      raise connect_error if connect_error is not None else (
        FederationTransportError("Peer resolved to no reachable address.")
      )
    try:
      if 300 <= upstream.status_code < 400:
        raise FederationTransportError("Federation redirects are not allowed.")

      declared_length = upstream.headers.get("content-length")
      if declared_length is not None:
        try:
          if int(declared_length) > max_response_bytes:
            raise FederationTransportError("Peer response exceeds the allowed size.")
        except ValueError:
          pass

      body = bytearray()
      async for chunk in upstream.aiter_bytes():
        room = max_response_bytes + 1 - len(body)
        if room <= 0:
          break
        body.extend(chunk[:room])
        if len(body) > max_response_bytes:
          raise FederationTransportError("Peer response exceeds the allowed size.")
    finally:
      await upstream.aclose()

  # aiter_bytes already decoded content encodings; retain representation
  # metadata without asking HTTPX to decode the buffered body a second time.
  headers = upstream.headers.copy()
  for name in ("content-encoding", "content-length", "transfer-encoding"):
    headers.pop(name, None)
  response = httpx.Response(
    upstream.status_code,
    headers=headers,
    content=bytes(body),
    request=public_request,
  )
  if 200 <= response.status_code < 300 and response_format == "json":
    _json_object(response.content, response.headers.get("content-type", ""))
  return response
