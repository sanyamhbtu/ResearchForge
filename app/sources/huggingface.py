"""Hugging Face Hub dataset adapter.

Docs: https://huggingface.co/docs/hub/api
Search:  GET /api/datasets?search=&limit=&full=true
Detail:  GET /api/datasets/{repo_id}?full=true
Splits:  https://datasets-server.huggingface.co/info?dataset={repo_id}

Verified sample counts come from the datasets-server splits endpoint; when that
call fails the count stays ``None`` and the UI reports it as unknown rather than
guessing.
"""

from __future__ import annotations

from typing import Any

from ..config import get_settings
from ..http_client import HttpError
from .base import (
    DATASETS,
    REPOSITORY,
    AdapterError,
    NormalizedRecord,
    SearchPage,
    SearchQuery,
    SourceAdapter,
)

SEARCH_URL = "https://huggingface.co/api/datasets"
DETAIL_URL = "https://huggingface.co/api/datasets/{repo_id}"
SERVER_INFO_URL = "https://datasets-server.huggingface.co/info"
SIZE_URL = "https://datasets-server.huggingface.co/size"
RESOLVE_URL = "https://huggingface.co/datasets/{repo_id}/resolve/{revision}/{filename}"

TASK_TO_MODALITY = {
    "text-classification": "text",
    "token-classification": "text",
    "question-answering": "text",
    "summarization": "text",
    "translation": "text",
    "text-generation": "text",
    "fill-mask": "text",
    "sentence-similarity": "text",
    "image-classification": "image",
    "object-detection": "image",
    "image-segmentation": "image",
    "image-to-text": "image",
    "text-to-image": "image",
    "depth-estimation": "image",
    "zero-shot-image-classification": "image",
    "automatic-speech-recognition": "audio",
    "audio-classification": "audio",
    "text-to-speech": "audio",
    "audio-to-audio": "audio",
    "voice-activity-detection": "audio",
    "tabular-classification": "tabular",
    "tabular-regression": "tabular",
    "time-series-forecasting": "timeseries",
    "video-classification": "video",
    "reinforcement-learning": "other",
    "graph-ml": "graph",
    "robotics": "other",
}


class HuggingFaceAdapter(SourceAdapter):
    name = "huggingface"
    label = "Hugging Face Datasets"
    kind = "dataset"
    families = (DATASETS, REPOSITORY)
    homepage = "https://huggingface.co/datasets"
    docs_url = "https://huggingface.co/docs/hub/api"
    supports_datasets = True
    credential_hint = "HUGGINGFACE_TOKEN (optional; needed for gated/private repos)"
    notes = (
        "Machine-learning dataset repositories with file listings, licenses, "
        "task tags and (via datasets-server) verified split sizes."
    )

    def credentials_present(self) -> bool:
        return True  # public metadata works anonymously

    def _headers(self) -> dict[str, str]:
        token = get_settings().huggingface_token
        return {"Authorization": f"Bearer {token}"} if token else {}

    def search(self, query: SearchQuery) -> SearchPage:
        limit = min(max(query.limit, 1), 100)
        want = min(limit + query.offset, 200)
        params: dict[str, Any] = {
            "search": query.text,
            "limit": want,
            "full": "true",
            "sort": "downloads",
            "direction": "-1",
        }
        payload = self._fetch_json(SEARCH_URL, params=params, headers=self._headers())
        items = payload if isinstance(payload, list) else (payload or {}).get("datasets") or []
        sliced = items[query.offset : query.offset + limit]
        records = [r for r in (self._to_record(item) for item in sliced) if r]

        warnings: list[str] = []
        if len(items) >= want:
            warnings.append(
                "Hugging Face search returns a capped ranked window; deeper "
                "pages are approximate rather than a true offset pagination."
            )
        next_offset = query.offset + limit if len(items) >= want else None
        return SearchPage(
            records=records,
            total_available=len(items),
            next_offset=next_offset,
            warnings=warnings,
            raw_result_count=len(items),
        )

    def fetch_detail(self, repo_id: str) -> dict | None:
        """Fetch authoritative repo metadata (license, files) for one dataset."""
        try:
            payload = self._fetch_json(
                DETAIL_URL.format(repo_id=repo_id), params={"full": "true"}, headers=self._headers()
            )
        except AdapterError:
            return None
        return payload if isinstance(payload, dict) else None

    def fetch_verified_size(self, repo_id: str) -> dict | None:
        """Ask datasets-server for real split sizes and row counts."""
        try:
            size = self._fetch_json(SIZE_URL, params={"dataset": repo_id}, headers=self._headers())
        except (AdapterError, HttpError):
            return None
        if not isinstance(size, dict):
            return None
        splits = ((size.get("size") or {}).get("splits")) or []
        total_rows = 0
        datasets_bytes = 0
        known_rows = False
        for entry in splits:
            rows = ((entry.get("num_rows")) if isinstance(entry, dict) else None)
            if isinstance(rows, int):
                total_rows += rows
                known_rows = True
            bytes_value = entry.get("num_bytes_original_files") if isinstance(entry, dict) else None
            if isinstance(bytes_value, int):
                datasets_bytes += bytes_value
        if not known_rows:
            return None
        return {"samples": total_rows, "size_bytes": datasets_bytes or None}

    def _to_record(self, item: dict) -> NormalizedRecord | None:
        repo_id = item.get("id") or item.get("_id")
        if not repo_id:
            return None
        card = item.get("cardData") or {}
        tags = item.get("tags") or []
        license_value = card.get("license") or ""
        if isinstance(license_value, list):
            license_value = ", ".join(str(v) for v in license_value)

        task_categories = card.get("task_categories") or []
        if isinstance(task_categories, str):
            task_categories = [task_categories]
        modality = ""
        for task in task_categories:
            modality = TASK_TO_MODALITY.get(str(task), "")
            if modality:
                break
        if not modality:
            modality = _modality_from_tags(tags)

        files = []
        formats: set[str] = set()
        for sibling in item.get("siblings") or []:
            filename = sibling.get("rfilename") if isinstance(sibling, dict) else None
            if not filename:
                continue
            files.append(
                {
                    "name": filename,
                    "url": RESOLVE_URL.format(
                        repo_id=repo_id, revision=item.get("sha") or "main", filename=filename
                    ),
                    "size_bytes": sibling.get("size"),
                }
            )
            extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
            if extension:
                formats.add(extension)

        features: list[str] = []
        splits_meta: list[dict] = []
        for config in (card.get("configs") or [])[:4]:
            for feature in ((config.get("data_files") or []) if isinstance(config, dict) else []):
                pass
        dataset_info = card.get("dataset_info")
        if isinstance(dataset_info, dict):
            for name, info in list(dataset_info.items())[:4]:
                if not isinstance(info, dict):
                    continue
                for split_name, split in (info.get("splits") or {}).items():
                    if isinstance(split, dict):
                        splits_meta.append(
                            {
                                "config": name,
                                "split": split_name,
                                "num_examples": split.get("num_examples"),
                                "num_bytes": split.get("num_bytes"),
                            }
                        )
                for feature_name in (info.get("features") or {}).keys():
                    if feature_name not in features:
                        features.append(str(feature_name))

        gated = bool(item.get("gated")) or bool(item.get("private"))
        access = "restricted" if gated else "open"
        declared_samples = None
        for split in splits_meta:
            if isinstance(split.get("num_examples"), int):
                declared_samples = (declared_samples or 0) + split["num_examples"]

        dataset_meta = {
            "repo_id": repo_id,
            "publisher": item.get("author") or repo_id.split("/")[0],
            "creators": [item.get("author")] if item.get("author") else [],
            "samples": declared_samples,
            "samples_source": "declared in dataset card" if declared_samples else "",
            "labels": features,
            "features": features,
            "splits": splits_meta,
            "format": ", ".join(sorted(formats)) if formats else "",
            "files": files[:200],
            "download_url": files[0]["url"] if files else "",
            "source_record_url": f"https://huggingface.co/datasets/{repo_id}",
            "version": item.get("sha") or "",
            "updated": item.get("lastModified") or "",
            "gated": gated,
            "downloads": item.get("downloads"),
            "likes": item.get("likes"),
            "size_categories": card.get("size_categories") or [],
            "languages": card.get("language") or [],
            "tags": tags[:30],
            "modality": modality,
            "access_instructions": (
                "Accept the dataset conditions on the Hub page, then re-run the "
                "download with HUGGINGFACE_TOKEN configured."
                if gated
                else ""
            ),
        }

        description = self._clean_text(card.get("pretty_name") or "")
        return NormalizedRecord(
            source=self.name,
            source_id=repo_id,
            kind="dataset",
            title=self._clean_text(card.get("pretty_name") or repo_id),
            abstract=self._clean_text(item.get("description") or ""),
            authors=[item.get("author")] if item.get("author") else [],
            year=self._year_from(item.get("lastModified")),
            published_date=(item.get("lastModified") or "")[:10] or None,
            venue="Hugging Face Hub",
            publisher=item.get("author") or "",
            url=f"https://huggingface.co/datasets/{repo_id}",
            license=str(license_value),
            access=access,
            citation_count=None,
            concepts=[str(t) for t in task_categories][:8],
            modality=modality,
            dataset=dataset_meta,
            raw={
                "downloads": item.get("downloads"),
                "likes": item.get("likes"),
                "sha": item.get("sha"),
                "gated": gated,
                "description_note": description,
            },
            retrieved_at="",
            source_family="datasets",
        )


def _modality_from_tags(tags: list) -> str:
    for tag in tags:
        text = str(tag).lower()
        if text.startswith("modality:"):
            return text.split(":", 1)[1]
        if text.startswith("size_categories:") or text.startswith("language:"):
            continue
        if text in TASK_TO_MODALITY:
            return TASK_TO_MODALITY[text]
    return ""
