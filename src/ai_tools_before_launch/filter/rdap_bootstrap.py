"""Offline snapshot of IANA's RDAP bootstrap (``dns.json``).

The age gate is only as good as its TLD coverage. A hand-written table of
thirty TLDs silently dropped every domain on ``.tech``, ``.tools``,
``.studio``, ``.design``, ``.website``, ``.store``, ``.blog`` and friends —
exactly where small AI products live — because a missing endpoint is
classified ``rdap_unsupported``, which is terminal and never retried.

So the endpoint table is data now, not code: it is generated from
https://data.iana.org/rdap/dns.json and shipped with the package, so a
fresh install works offline and in networks that cannot reach iana.org.

Regenerate with::

    curl -s https://data.iana.org/rdap/dns.json -o /tmp/rdap.json
    python - <<'EOF'
    import json
    services = json.load(open("/tmp/rdap.json"))["services"]
    table = {
        tld: url[0].rstrip("/") + "/domain/"
        for group, url in services
        for tld in group
    }
    json.dump(dict(sorted(table.items())),
              open("src/ai_tools_before_launch/filter/rdap_bootstrap.json", "w"),
              sort_keys=True, separators=(",", ":"))
    EOF

Snapshots are additive in practice: TLDs are delegated far more often than
RDAP endpoints are withdrawn, so a stale snapshot costs coverage, never
correctness. ``rdap_age._TLD_RDAP`` still wins over this table for the
registries whose published URL needs a manual correction (``.io`` is not in
the IANA bootstrap at all).
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Mapping

SNAPSHOT_SOURCE = "https://data.iana.org/rdap/dns.json"
SNAPSHOT_DATE = "2026-11-01"
"""Date the bundled snapshot was generated. Used by tests and ``status``."""


@lru_cache(maxsize=1)
def bootstrap_map() -> Mapping[str, str]:
    """Return ``{tld: rdap_base_url}``, each base ending in ``/domain/``.

    Loaded once per process: ~1,200 entries, parsed on first use so importing
    the filter package stays free for readers that never touch RDAP.
    """
    raw = (
        resources.files("ai_tools_before_launch.filter")
        .joinpath("rdap_bootstrap.json")
        .read_text(encoding="utf-8")
    )
    parsed = json.loads(raw)
    return {str(tld): str(base) for tld, base in parsed.items()}
