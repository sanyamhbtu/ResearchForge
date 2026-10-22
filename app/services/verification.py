"""Verification pipeline.

Rules implemented here (all deterministic, all evidence-carrying):

* A record existing in an index does not make its metadata correct.
* A DOI resolving does not mean it supports the sentence next to it.
* A source that cannot be reached is labelled inaccessible — never "read".
* Disagreements between the record and the registrar are surfaced as
  ``conflict``, never silently overwritten.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from ..http_client import HttpClient, HttpError
from ..logging_utils import get_logger
from ..sources.crossref import CrossrefAdapter
from ..sources.datacite import DataCiteAdapter
from .dedupe import jaccard, normalize_doi, title_tokens

log = get_logger("services.verification")

VERIFIED = "verified"
FAILED = "failed"
CONFLICT = "conflict"
INCONCLUSIVE = "inconclusive"
SKIPPED = "skipped"
INACCESSIBLE = "inaccessible"

DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
TITLE_MATCH_THRESHOLD = 0.5


@dataclass
class CheckResult:
    check_name: str
    status: str
    method: str
    deterministic: bool = True
    detail: str = ""
    evidence_url: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class VerificationReport:
    record_key: str
    checks: list[CheckResult] = field(default_factory=list)
    resolved_metadata: dict = field(default_factory=dict)

    def status_of(self, check_name: str) -> str:
        for check in self.checks:
            if check.check_name == check_name:
                return check.status
        return SKIPPED

    @property
    def overall(self) -> str:
        statuses = {check.status for check in self.checks}
        if CONFLICT in statuses:
            return CONFLICT
        if FAILED in statuses:
            return FAILED
        if statuses and statuses <= {VERIFIED}:
            return VERIFIED
        if VERIFIED in statuses:
            return "partially_verified"
        if INACCESSIBLE in statuses:
            return INACCESSIBLE
        return INCONCLUSIVE

    def as_dict(self) -> dict:
        return {
            "record_key": self.record_key,
            "overall": self.overall,
            "checks": [check.as_dict() for check in self.checks],
            "resolved_metadata": self.resolved_metadata,
        }


class Verifier:
    def __init__(self, http: HttpClient | None = None) -> None:
        self.http = http or HttpClient(logger=get_logger("services.verification.http"))
        self.crossref = CrossrefAdapter(self.http)
        self.datacite = DataCiteAdapter(self.http)

    # -- per-record verification -------------------------------------------
    def verify(self, record: dict, *, check_links: bool = True) -> VerificationReport:
        report = VerificationReport(record_key=record.get("record_key", record.get("uid", "")))
        doi = normalize_doi(record.get("doi"))

        report.checks.append(self._identifier_check(record, doi))
        resolved = self._resolve_doi(record, doi, report)
        if resolved:
            report.resolved_metadata = _summarize_resolved(resolved)
            report.checks.append(self._title_match_check(record, resolved))
            report.checks.append(self._license_check(record, resolved))
        else:
            report.checks.append(self._license_check(record, None))

        if check_links:
            report.checks.extend(self._link_checks(record))

        log.info(
            "record_verified",
            record_key=report.record_key,
            overall=report.overall,
            checks=len(report.checks),
        )
        return report

    # -- individual checks -------------------------------------------------
    def _identifier_check(self, record: dict, doi: str) -> CheckResult:
        if not doi:
            return CheckResult(
                check_name="stable_identifier",
                status=INCONCLUSIVE,
                method="presence of DOI / repository identifier in source metadata",
                detail=(
                    "No DOI in the source metadata. A repository identifier "
                    f"({record.get('source')}:{record.get('source_id')}) is retained instead."
                ),
                evidence_url=record.get("url") or "",
            )
        if not DOI_RE.match(doi):
            return CheckResult(
                check_name="stable_identifier",
                status=FAILED,
                method="DOI syntax check (10.<registrant>/<suffix>)",
                detail=f"'{doi}' does not match DOI syntax.",
            )
        return CheckResult(
            check_name="stable_identifier",
            status=VERIFIED,
            method="DOI syntax check (10.<registrant>/<suffix>)",
            detail=f"DOI {doi} is syntactically valid.",
            evidence_url=f"https://doi.org/{doi}",
        )

    def _resolve_doi(self, record: dict, doi: str, report: VerificationReport) -> dict | None:
        if not doi:
            report.checks.append(
                CheckResult(
                    check_name="registrar_resolution",
                    status=SKIPPED,
                    method="Crossref / DataCite DOI resolution",
                    detail="No DOI available, so registrar resolution cannot be attempted.",
                )
            )
            return None

        is_dataset = record.get("kind") == "dataset"
        adapters = [self.datacite, self.crossref] if is_dataset else [self.crossref, self.datacite]
        for adapter in adapters:
            if not hasattr(adapter, "resolve_doi"):
                continue
            try:
                resolved = adapter.resolve_doi(doi)
            except Exception as exc:  # network failure ≠ non-existence
                report.checks.append(
                    CheckResult(
                        check_name="registrar_resolution",
                        status=INCONCLUSIVE,
                        method=f"{adapter.label} /dois/{{doi}} lookup",
                        detail=(
                            f"Lookup failed ({exc}). This is a retrieval failure, NOT evidence "
                            "that the DOI is invalid."
                        ),
                        evidence_url=f"https://doi.org/{doi}",
                    )
                )
                return None
            if resolved:
                report.checks.append(
                    CheckResult(
                        check_name="registrar_resolution",
                        status=VERIFIED,
                        method=f"{adapter.label} /dois/{{doi}} lookup",
                        detail=f"The DOI is registered and resolvable via {adapter.label}.",
                        evidence_url=f"https://doi.org/{doi}",
                    )
                )
                return resolved
            report.checks.append(
                CheckResult(
                    check_name="registrar_resolution",
                    status=FAILED,
                    method=f"{adapter.label} /dois/{{doi}} lookup",
                    detail=f"{adapter.label} reports no record for this DOI.",
                    evidence_url=f"https://doi.org/{doi}",
                )
            )
        return None

    def _title_match_check(self, record: dict, resolved: dict) -> CheckResult:
        remote_title = _resolved_title(resolved)
        if not remote_title:
            return CheckResult(
                check_name="metadata_agreement",
                status=INCONCLUSIVE,
                method="token-overlap comparison of record title vs registrar title",
                detail="The registrar response contained no title to compare against.",
            )
        similarity = jaccard(title_tokens(remote_title), title_tokens(record.get("title", "")))
        if similarity >= TITLE_MATCH_THRESHOLD:
            return CheckResult(
                check_name="metadata_agreement",
                status=VERIFIED,
                method="token-overlap (Jaccard) of record title vs registrar title",
                detail=f"Titles agree (similarity {similarity:.2f}). Registrar title: “{remote_title}”.",
            )
        return CheckResult(
            check_name="metadata_agreement",
            status=CONFLICT,
            method="token-overlap (Jaccard) of record title vs registrar title",
            detail=(
                f"Registrar title disagrees with the source record (similarity {similarity:.2f}). "
                f"Record: “{record.get('title', '')}” | Registrar: “{remote_title}”."
            ),
        )

    def _license_check(self, record: dict, resolved: dict | None) -> CheckResult:
        license_local = (record.get("license") or "").strip()
        license_remote = ""
        if resolved:
            if "license" in resolved and isinstance(resolved.get("license"), dict):
                license_remote = resolved["license"].get("URL") or resolved["license"].get("id") or ""
            for right in resolved.get("rightsList") or []:
                license_remote = right.get("rightsIdentifier") or right.get("rightsUri") or ""
                if license_remote:
                    break
        if license_local and license_remote and license_local.lower() != license_remote.lower():
            return CheckResult(
                check_name="license_consistency",
                status=CONFLICT,
                method="comparison of source-declared license vs registrar license",
                detail=f"Source declares '{license_local}' while the registrar declares '{license_remote}'.",
            )
        if license_local or license_remote:
            return CheckResult(
                check_name="license_consistency",
                status=VERIFIED,
                method="presence of a declared license in source or registrar metadata",
                detail=f"License declared: '{license_local or license_remote}'. Usage must obey it.",
            )
        return CheckResult(
            check_name="license_consistency",
            status=INCONCLUSIVE,
            method="presence of a declared license in source or registrar metadata",
            detail=(
                "No license is declared. Unknown licensing is reported as unknown, not assumed "
                "permissive."
            ),
        )

    def _link_checks(self, record: dict) -> list[CheckResult]:
        checks: list[CheckResult] = []
        url = record.get("url") or ""
        if url:
            checks.append(self._probe("source_link", url, "HEAD request to the source record URL"))
        dataset = record.get("dataset") or {}
        files = dataset.get("files") or []
        if record.get("kind") == "dataset" and files:
            first = files[0]
            target = first.get("url") if isinstance(first, dict) else None
            if target:
                checks.append(
                    self._probe(
                        "dataset_file_reachable",
                        target,
                        "HEAD request to the first listed data file",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        check_name="dataset_file_reachable",
                        status=INCONCLUSIVE,
                        method="HEAD request to the first listed data file",
                        detail="Files are listed but no direct file URL was published by the source.",
                    )
                )
        return checks

    def _probe(self, check_name: str, url: str, method: str) -> CheckResult:
        try:
            status = self.http.head_status(url)
        except HttpError as exc:
            return CheckResult(
                check_name=check_name,
                status=INCONCLUSIVE,
                method=method,
                detail=f"Could not probe the link ({exc}). Reachability is unknown.",
                evidence_url=url,
            )
        if 200 <= status < 400:
            return CheckResult(
                check_name=check_name,
                status=VERIFIED,
                method=method,
                detail=f"Link answered HTTP {status}.",
                evidence_url=url,
            )
        if status in {401, 402, 403, 451}:
            return CheckResult(
                check_name=check_name,
                status=INACCESSIBLE,
                method=method,
                detail=(
                    f"HTTP {status}: access is controlled (authentication, paywall or legal "
                    "restriction). ResearchForge does not bypass access controls."
                ),
                evidence_url=url,
            )
        if status == 404:
            return CheckResult(
                check_name=check_name,
                status=FAILED,
                method=method,
                detail="HTTP 404: the advertised link does not resolve.",
                evidence_url=url,
            )
        if status >= 400:
            return CheckResult(
                check_name=check_name,
                status=INCONCLUSIVE,
                method=method,
                detail=f"HTTP {status} returned; reachability is uncertain.",
                evidence_url=url,
            )
        return CheckResult(
            check_name=check_name,
            status=INCONCLUSIVE,
            method=method,
            detail=f"Unexpected HTTP {status}.",
            evidence_url=url,
        )


def _resolved_title(resolved: dict) -> str:
    if not resolved:
        return ""
    if isinstance(resolved.get("title"), list):
        return str(resolved["title"][0]) if resolved["title"] else ""
    if isinstance(resolved.get("title"), str):
        return resolved["title"]
    titles = resolved.get("titles") or []
    if titles and isinstance(titles[0], dict):
        return str(titles[0].get("title") or "")
    return ""


def _summarize_resolved(resolved: dict) -> dict:
    license_value = ""
    if isinstance(resolved.get("license"), dict):
        license_value = resolved["license"].get("URL") or resolved["license"].get("id") or ""
    for right in resolved.get("rightsList") or []:
        license_value = right.get("rightsIdentifier") or right.get("rightsUri") or license_value
        if license_value:
            break
    creators = []
    for creator in (resolved.get("author") or resolved.get("creators") or [])[:12]:
        if isinstance(creator, dict):
            name = creator.get("name") or " ".join(
                part for part in [creator.get("given"), creator.get("family")] if part
            )
            if name:
                creators.append(name)
    return {
        "title": _resolved_title(resolved),
        "doi": resolved.get("DOI") or resolved.get("doi") or "",
        "publisher": resolved.get("publisher") or "",
        "license": license_value,
        "creators": creators,
        "publication_year": resolved.get("publicationYear")
        or ((resolved.get("issued") or {}).get("date-parts") or [[None]])[0][0],
        "type": resolved.get("type")
        or ((resolved.get("types") or {}).get("resourceTypeGeneral") if resolved.get("types") else ""),
    }
