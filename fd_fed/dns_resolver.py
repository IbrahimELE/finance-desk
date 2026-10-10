#!/usr/bin/env python3
"""One-shot DNS/SSRF validation helper for cancellable federation lookups."""

from __future__ import annotations

import json
import sys

from net_utils import URLValidationError, validate_url_safe_core


def main() -> int:
  if len(sys.argv) != 2:
    return 2
  try:
    pinned_urls, host_header, sni_host = validate_url_safe_core(sys.argv[1])
    payload = {
      "ok": True,
      "pinned_urls": pinned_urls,
      "host_header": host_header,
      "sni_host": sni_host,
    }
  except URLValidationError as exc:
    payload = {
      "ok": False,
      "status": int(exc.status_code),
      "detail": str(exc.detail),
    }
  except Exception:
    payload = {
      "ok": False,
      "status": 502,
      "detail": "Host resolution failed.",
    }
  sys.stdout.write(json.dumps(payload, separators=(",", ":")))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
