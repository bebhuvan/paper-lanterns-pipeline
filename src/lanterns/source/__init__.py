"""Internet Archive (`archive.org`) discovery, metadata triage, IIIF/BookReader streaming, and verified source ingest."""

from __future__ import annotations

from lanterns.source.archive_org import (
    ARCHIVE_DOWNLOAD_BASE,
    ARCHIVE_IIIF_BASE,
    ARCHIVE_METADATA_BASE,
    ARCHIVE_SEARCH_URL,
    ArchiveSourceError,
    access_leaves,
    build_iiif_info_url,
    build_iiif_page_url,
    detect_jbig2_derivative_risk,
    download_archive_source,
    fetch_archive_metadata,
    parse_scandata_leaves,
    search_archive_correspondence,
)

__all__ = [
    "ARCHIVE_DOWNLOAD_BASE",
    "ARCHIVE_IIIF_BASE",
    "ARCHIVE_METADATA_BASE",
    "ARCHIVE_SEARCH_URL",
    "ArchiveSourceError",
    "access_leaves",
    "build_iiif_info_url",
    "build_iiif_page_url",
    "detect_jbig2_derivative_risk",
    "download_archive_source",
    "fetch_archive_metadata",
    "parse_scandata_leaves",
    "search_archive_correspondence",
]
