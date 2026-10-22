"""Zenodo adapter — research datasets, software and associated records.

Docs: https://developers.zenodo.org/#records
Search: GET /api/records?q=&size=&page=&type=dataset&access_right=open
"""

from __future__ import annotations

from typing import Any

from .base import DATASETS, REPOSITORY, NormalizedRecord, SearchPage, SearchQuery, SourceAdapter

BASE = "https://zenodo.org/api/records"

EXTENSION_TO_FORMAT = {
    "csv": "CSV",
    "tsv": "TSV",
    "json": "JSON",
    "jsonl": "JSONL",
    "parquet": "Parquet",
    "xlsx": "XLSX",
    "zip": "ZIP archive",
    "tar": "TAR archive",
    "gz": "Gzip",
    "h5": "HDF5",
    "hdf5": "HDF5",
    "nc": "NetCDF",
    "nc4": "NetCDF",
    "dcm": "DICOM",
    "nii": "NIfTI",
    "png": "PNG image",
    "jpg": "JPEG image",
    "tif": "TIFF image",
    "wav": "WAV audio",
    "mp3": "MP3 audio",
    "mp4": "MP4 video",
    "txt": "plain text",
    "pdf": "PDF document",
    "rdf": "RDF",
    "shp": "Shapefile",
    "geojson": "GeoJSON",
    "sav": "SPSS data",
    "dta": "Stata data",
}


class ZenodoAdapter(SourceAdapter):
    name = "zenodo"
    label = "Zenodo"
    kind = "dataset"
    families = (DATASETS, REPOSITORY)
    homepage = "https://zenodo.org"
    docs_url = "https://developers.zenodo.org/#records"
    supports_datasets = True
    notes = (
        "CERN-operated general-purpose repository: datasets, software and "
        "supplementary material with DOIs, files, checksums and licenses."
    )

    def search(self, query: SearchQuery) -> SearchPage:
        size = min(max(query.limit, 1), 100)
        page = (query.offset // size) + 1
        params: dict[str, Any] = {
            "q": query.text,
            "size": size,
            "page": page,
            "sort": "bestmatch",
        }
        if query.filters.get("open_access_only"):
            params["access_right"] = "open"
        if query.intent == "datasets":
            params["type"] = "dataset"

        payload = self._fetch_json(BASE, params=params)
        hits_wrapper = (payload or {}).get("hits") or {}
        hits = hits_wrapper.get("hits") or []
        total = hits_wrapper.get("total")
        records = [r for r in (self._to_record(item) for item in hits) if r]

        next_offset = None
        if isinstance(total, int) and page * size < min(total, 10000):
            next_offset = page * size
        return SearchPage(
            records=records,
            total_available=total,
            next_offset=next_offset,
            raw_result_count=len(hits),
        )

    def fetch_detail(self, record_id: str) -> dict | None:
        try:
            return self._fetch_json(f"{BASE}/{record_id}")
        except Exception:
            return None

    def _to_record(self, item: dict) -> NormalizedRecord | None:
        metadata = item.get("metadata") or {}
        title = self._clean_text(metadata.get("title"))
        if not title:
            return None
        resource_type = (metadata.get("resource_type") or {}).get("type") or ""
        is_dataset = resource_type in {"dataset", "software", "image", "other", "publication"}
        license_info = metadata.get("license") or {}
        if isinstance(license_info, dict):
            license_text = license_info.get("id") or license_info.get("title") or ""
        else:
            license_text = str(license_info)

        files = []
        formats: set[str] = set()
        total_size = 0
        for file_entry in item.get("files") or []:
            filename = file_entry.get("key") or ""
            size = file_entry.get("size")
            if isinstance(size, int):
                total_size += size
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if extension and extension in EXTENSION_TO_FORMAT:
                formats.add(EXTENSION_TO_FORMAT[extension])
            files.append(
                {
                    "name": filename,
                    "url": (file_entry.get("links") or {}).get("self") or "",
                    "size_bytes": size,
                    "checksum": file_entry.get("checksum") or "",
                    "format": EXTENSION_TO_FORMAT.get(extension, extension.upper()),
                }
            )

        access_right = item.get("access_right") or metadata.get("access_right") or "unknown"
        access = {"open": "open", "restricted": "restricted", "closed": "restricted"}.get(
            access_right, "unknown"
        )
        related = metadata.get("related_identifiers") or []
        related_publications = [
            rel.get("identifier")
            for rel in related
            if isinstance(rel, dict) and rel.get("relation") in {"isSupplementTo", "isDocumentedBy", "cites"}
        ][:25]

        dataset_meta = {
            "publisher": (metadata.get("imprint_publisher") or "Zenodo"),
            "creators": [c.get("name") for c in (metadata.get("creators") or []) if c.get("name")][:40],
            "samples": None,
            "labels": [],
            "format": ", ".join(sorted(formats)) if formats else "",
            "files": files[:200],
            "size_bytes": total_size or None,
            "download_url": files[0]["url"] if files else "",
            "source_record_url": f"https://zenodo.org/records/{item.get('id')}",
            "version": metadata.get("version") or "",
            "updated": (metadata.get("publication_date") or item.get("updated") or ""),
            "doi": item.get("doi") or "",
            "related_publications": related_publications,
            "keywords": metadata.get("keywords") or [],
            "resource_type": resource_type,
            "access_instructions": (
                "" if access == "open" else "Request access from the depositor; download is not permitted."
            ),
        }

        return NormalizedRecord(
            source=self.name,
            source_id=str(item.get("id") or item.get("doi") or title[:60]),
            kind="dataset" if is_dataset else "paper",
            title=title,
            doi=(item.get("doi") or "").lower(),
            abstract=self._clean_text(metadata.get("description") or ""),
            authors=[c.get("name") for c in (metadata.get("creators") or []) if c.get("name")][:40],
            year=self._year_from(metadata.get("publication_date")),
            published_date=metadata.get("publication_date"),
            venue="Zenodo",
            publisher=(metadata.get("imprint_publisher") or "Zenodo"),
            url=f"https://zenodo.org/records/{item.get('id')}",
            pdf_url="",
            license=str(license_text),
            access=access,
            citation_count=None,
            concepts=[str(k) for k in (metadata.get("keywords") or [])][:10],
            modality="",
            dataset=dataset_meta,
            raw={
                "doi": item.get("doi"),
                "conceptdoi": item.get("conceptdoi"),
                "stats": item.get("stats"),
                "resource_type": resource_type,
            },
            retrieved_at="",
            source_family="datasets",
        )
