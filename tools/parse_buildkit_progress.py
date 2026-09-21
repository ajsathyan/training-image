#!/usr/bin/env python3
"""Extract bounded phase evidence from BuildKit plain-progress output."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


HEADER = re.compile(r"^(#\d+) (?:\[[^]]+\] )?(.+)$")
DURATION = re.compile(r"^(#\d+) DONE ([0-9]+(?:\.[0-9]+)?)s$")
INLINE_DURATION = re.compile(
    r"^(#\d+) preparing layers for inline cache ([0-9]+(?:\.[0-9]+)?)s done$"
)


def parse_progress(text: str, *, cache_export_expected: bool = False) -> dict[str, Any]:
    vertices: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    inline_cache_seen = False
    inline_substeps: list[dict[str, Any]] = []
    for line in text.splitlines():
        if "preparing layers for inline cache" in line:
            inline_cache_seen = True
        header = HEADER.match(line)
        if header and not line.startswith(header.group(1) + " DONE"):
            vertex, description = header.groups()
            if vertex not in vertices:
                order.append(vertex)
                vertices[vertex] = {"vertex": vertex, "description": description, "seconds": None}
        duration = DURATION.match(line)
        if duration:
            vertex, seconds = duration.groups()
            vertices.setdefault(vertex, {"vertex": vertex, "description": "unknown", "seconds": None})
            vertices[vertex]["seconds"] = float(seconds)
        inline_duration = INLINE_DURATION.match(line)
        if inline_duration:
            vertex, seconds = inline_duration.groups()
            inline_substeps.append(
                {"vertex": vertex, "description": "preparing layers for inline cache", "seconds": float(seconds)}
            )

    categories = {
        "baseDownloadSeconds": 0.0,
        "cacheImportSeconds": 0.0,
        "dockerfileExecutionSeconds": 0.0,
        "outputExportSeconds": 0.0,
        "cacheExportSeconds": 0.0,
        "unclassifiedSeconds": 0.0,
    }
    records: list[dict[str, Any]] = []
    for vertex in order:
        record = vertices[vertex]
        seconds = record["seconds"]
        description = record["description"]
        if seconds is None:
            category = "unmeasured"
        elif "importing cache manifest" in description:
            category = "cacheImportSeconds"
        elif "exporting cache" in description or "inline cache" in description:
            category = "cacheExportSeconds"
        elif "load metadata for" in description or description.startswith("FROM "):
            category = "baseDownloadSeconds"
        elif "exporting" in description or "pushing layers" in description:
            category = "outputExportSeconds"
        elif re.match(r"(?:RUN|COPY|ADD) ", description):
            category = "dockerfileExecutionSeconds"
        else:
            category = "unclassifiedSeconds"
        record["category"] = category
        records.append(record)
        if seconds is not None and category in categories:
            categories[category] += seconds
    cache_records = [record for record in records if record["category"] == "cacheExportSeconds"]
    if inline_substeps:
        categories["cacheExportSeconds"] += sum(item["seconds"] for item in inline_substeps)
        cache_export_availability = "measured"
    elif cache_records and all(record["seconds"] is not None for record in cache_records):
        cache_export_availability = "measured"
    elif cache_export_expected or inline_cache_seen:
        cache_export_availability = "not_separately_observable"
        categories["cacheExportSeconds"] = None
    else:
        cache_export_availability = "not_applicable"
        categories["cacheExportSeconds"] = None
    return {
        "metricSemantics": "sum of BuildKit vertex DONE durations; concurrent vertices may overlap",
        "categories": {
            key: round(value, 3) if value is not None else None
            for key, value in categories.items()
        },
        "cacheExportAvailability": cache_export_availability,
        "inlineCacheSubsteps": inline_substeps,
        "vertices": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--metadata", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--exit-status", type=int, required=True)
    parser.add_argument("--wall-seconds", type=float, required=True)
    parser.add_argument(
        "--inline-cache-export",
        choices=("expected", "not-applicable"),
        required=True,
    )
    arguments = parser.parse_args()
    raw_log = arguments.log.read_text(encoding="utf-8", errors="replace")
    metadata: Any = None
    if arguments.metadata.exists():
        raw_metadata = arguments.metadata.read_text(encoding="utf-8", errors="replace")
        try:
            metadata = json.loads(raw_metadata)
        except json.JSONDecodeError as error:
            metadata = {"parseError": str(error), "rawPrefix": raw_metadata[:2000]}
    value = parse_progress(
        raw_log,
        cache_export_expected=arguments.inline_cache_export == "expected",
    )
    value.update(
        {
            "schemaVersion": "agora.buildkit-phase-evidence.v1",
            "exitStatus": arguments.exit_status,
            "wallSeconds": arguments.wall_seconds,
            "metadata": metadata,
        }
    )
    temporary = arguments.output.with_suffix(arguments.output.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, arguments.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
