"""Real-doc corpus run: measure rewrite coverage and token split.

Read-only — fetches real documents and measures compression metrics.
Zero mutations sent to any document.

Run with:
    direnv exec . python experiments/tree_ir_prototype/corpus_run.py
"""

from __future__ import annotations

import json
import re
import glob
import sys
from dataclasses import dataclass

# Add parent to path for imports
sys.path.insert(0, "/Users/hhartmann/Projects/Tools/src/gax")

from experiments.tree_ir_prototype.rewrite_rules import (
    compress_doc,
    coverage_report,
    extract_appendix,
)
from gax.auth import get_authenticated_credentials
from googleapiclient.discovery import build


def get_docs_service():
    """Build authenticated Docs API service."""
    creds = get_authenticated_credentials()
    return build("docs", "v1", credentials=creds)


@dataclass
class DocMetrics:
    title: str
    doc_id: str
    raw_json_chars: int
    body_chars: int
    appendix_chars: int
    markdown_chars: int
    coverage_pct: float
    total_nodes: int
    verbatim_count: int
    verbatim_types: list[str]


def fetch_and_measure(docs_service, doc_id: str, title: str) -> DocMetrics:
    """Fetch a real doc and measure compression metrics."""
    doc = docs_service.documents().get(
        documentId=doc_id, includeTabsContent=True
    ).execute()

    # Get body content from first tab
    tab = doc.get("tabs", [{}])[0]
    body_content = tab.get("documentTab", {}).get("body", {}).get("content", [])
    lists = tab.get("documentTab", {}).get("lists", {})

    # Raw JSON size
    raw_json = json.dumps(body_content, ensure_ascii=False)
    raw_json_chars = len(raw_json)

    # Compress
    result = compress_doc(body_content, lists=lists)
    report = coverage_report(result)

    # Extract appendix
    appendix_result = extract_appendix(result.body)
    body_json = json.dumps(appendix_result.body, ensure_ascii=False)
    appendix_json = json.dumps(appendix_result.appendix, ensure_ascii=False)

    # Identify verbatim node types
    verbatim_types = []
    for item in result.body:
        if isinstance(item, dict) and "_verbatim" in item:
            verbatim = item["_verbatim"]
            if isinstance(verbatim, dict):
                verbatim_types.extend(k for k in verbatim.keys()
                                      if k not in ("startIndex", "endIndex"))

    # Quick markdown approximation (text only)
    md_chars = 0
    for elem in body_content:
        if "paragraph" in elem:
            for el in elem["paragraph"].get("elements", []):
                tr = el.get("textRun", {})
                md_chars += len(tr.get("content", ""))

    return DocMetrics(
        title=title,
        doc_id=doc_id,
        raw_json_chars=raw_json_chars,
        body_chars=len(body_json),
        appendix_chars=len(appendix_json),
        markdown_chars=md_chars,
        coverage_pct=report["coverage_pct"],
        total_nodes=report["total_nodes"],
        verbatim_count=report["verbatim"],
        verbatim_types=verbatim_types,
    )


def discover_docs() -> list[tuple[str, str]]:
    """Discover real doc IDs from checked-out .gax.md files."""
    files = glob.glob("/Users/hhartmann/Projects/Tools/src/gax/**/*.doc.gax.md", recursive=True)
    docs = []
    for f in sorted(files):
        with open(f) as fh:
            content = fh.read(500)
        m = re.search(r"source: https://docs.google.com/document/d/([^/]+)/edit", content)
        title_m = re.search(r"title: (.+)", content)
        if m and title_m:
            docs.append((m.group(1), title_m.group(1)))
    return docs


def main():
    """Run corpus measurement on a sample of real documents."""
    all_docs = discover_docs()

    # Select diverse sample: largest (Chairs Guide), medium, and small docs
    # Pick 6-8 diverse documents
    target_titles = [
        "SREcon Chairs Guide",          # Largest (18K md)
        "USENIX Responsibilities",      # Medium (4K)
        "Co-chair Responsibilities",    # Medium (2.8K)
        "Panel Moderator Role",         # Medium (2.6K)
        "Room Captain How-To",          # Medium (2.4K)
        "Speaker rehearsals: Logistics & guidance",  # Medium (2.9K)
    ]

    # Find matching docs
    sample = []
    for doc_id, title in all_docs:
        if title in target_titles:
            sample.append((doc_id, title))
        if len(sample) >= 6:
            break

    # Add any extras if we didn't get enough
    if len(sample) < 5:
        for doc_id, title in all_docs:
            if (doc_id, title) not in sample:
                sample.append((doc_id, title))
            if len(sample) >= 6:
                break

    print(f"Measuring {len(sample)} documents...\n")

    docs_service = get_docs_service()
    metrics: list[DocMetrics] = []

    for doc_id, title in sample:
        try:
            m = fetch_and_measure(docs_service, doc_id, title)
            metrics.append(m)
            print(f"  ✓ {title}: {m.coverage_pct:.1f}% coverage, "
                  f"{m.raw_json_chars} raw → {m.body_chars} body + {m.appendix_chars} appendix")
        except Exception as e:
            print(f"  ✗ {title}: {e}")

    # Print summary table
    print("\n" + "=" * 90)
    print(f"{'Document':<40} {'Raw JSON':>10} {'Body':>8} {'Appx':>8} {'MD':>6} {'Cov%':>6} {'Verb':>5}")
    print("-" * 90)
    for m in metrics:
        print(f"{m.title[:39]:<40} {m.raw_json_chars:>10,} {m.body_chars:>8,} "
              f"{m.appendix_chars:>8,} {m.markdown_chars:>6,} {m.coverage_pct:>5.1f}% {m.verbatim_count:>5}")
    print("-" * 90)

    # Aggregates
    total_raw = sum(m.raw_json_chars for m in metrics)
    total_body = sum(m.body_chars for m in metrics)
    total_appx = sum(m.appendix_chars for m in metrics)
    total_md = sum(m.markdown_chars for m in metrics)
    avg_coverage = sum(m.coverage_pct for m in metrics) / len(metrics) if metrics else 0

    print(f"{'TOTAL':<40} {total_raw:>10,} {total_body:>8,} "
          f"{total_appx:>8,} {total_md:>6,} {avg_coverage:>5.1f}%")
    print(f"\nBody/Raw ratio: {total_body/total_raw:.2f}")
    print(f"(Body+Appx)/Raw ratio: {(total_body+total_appx)/total_raw:.2f}")
    print(f"MD/Raw ratio: {total_md/total_raw:.3f}")

    # Residual inventory
    all_verbatim = []
    for m in metrics:
        all_verbatim.extend(m.verbatim_types)
    if all_verbatim:
        from collections import Counter
        counts = Counter(all_verbatim)
        print(f"\nResidual (verbatim) node types:")
        for node_type, count in counts.most_common():
            print(f"  {node_type}: {count}")
    else:
        print(f"\nNo residual nodes — 100% coverage across all docs!")

    return metrics


if __name__ == "__main__":
    main()
