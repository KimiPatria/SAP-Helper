"""Free-text -> master-data resolution.

"the steel supplier" or "hex bolts M8" won't match SAP's internal codes,
so every entity reference goes through this resolver, which returns
*ranked candidates* - never a single silent guess. The preview tool turns
zero candidates into "not found" and multiple plausible candidates into a
question back to the user.

Search strategy (cheapest first, each level only if the previous found
nothing):
  1. exact key lookup, if the text already looks like a code
  2. whole-phrase substring search on the description field
  3. AND of the individual words (word order in names rarely matches speech)
  4. per-word searches merged, ranked by how many words matched

Lookups are described by `LookupSpec` records rather than per-entity code:
adding a new master-data type (e.g. cost centers for the future GR/Invoice
tools) is one new spec, no new resolver logic. Specs also carry the OData
version because S/4HANA Cloud releases plant master data only as OData v4,
whose filter dialect differs from the v2 supplier/product services.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.mcp.client import SAPClient
from app.mcp.errors import SAPRequestError

# Words that describe the entity type rather than the entity's name; they
# would poison word-level searches ("steel supplier" must not require the
# supplier's name to contain "supplier").
_STOPWORDS = {
    "the", "a", "an", "our", "my", "for", "from", "of", "supplier", "vendor",
    "material", "product", "item", "plant", "site", "factory", "warehouse",
    "usual", "regular", "company", "inc", "co",
}

_CODE_PATTERN = re.compile(r"[A-Za-z0-9_/-]{1,40}")


@dataclass(frozen=True)
class LookupSpec:
    """Where and how to search one master-data type."""

    entity_type: str        # "supplier" | "material" | "plant"
    service_root: str       # OData service path on the tenant
    entity_set: str         # entity set searched by description
    code_field: str         # field holding the SAP code
    description_field: str  # field holding the human-readable name
    odata_version: int      # 2 or 4 - changes filter syntax + result shape
    extra_filter: str = ""  # e.g. language restriction for product descriptions
    # Entity set for exact-key validation when it differs from the search
    # set (product descriptions live apart from the product itself).
    key_entity_set: str = ""


@dataclass(frozen=True)
class Candidate:
    code: str
    description: str
    match: str  # "exact-code" | "exact-name" | "name-match" | "partial"


class MasterDataResolver:
    def __init__(self, client: SAPClient, specs: dict[str, LookupSpec],
                 max_candidates: int = 5):
        self._client = client
        self._specs = specs
        self._max = max_candidates

    def entity_types(self) -> list[str]:
        return sorted(self._specs)

    def resolve(self, entity_type: str, text: str) -> list[Candidate]:
        """Ranked candidates for a free-text reference. Empty list = no match."""
        spec = self._specs.get(entity_type)
        if spec is None:
            raise SAPRequestError(
                f"Unknown entity type '{entity_type}'. "
                f"Valid types: {', '.join(self.entity_types())}."
            )
        text = text.strip()
        if not text:
            return []

        if _CODE_PATTERN.fullmatch(text):
            exact = self._key_lookup(spec, text) or (
                self._key_lookup(spec, text.upper()) if text != text.upper() else None
            )
            if exact:
                return [exact]

        candidates = self._search(spec, [text])
        if not candidates:
            words = [w for w in re.findall(r"[A-Za-z0-9]+", text.lower())
                     if w not in _STOPWORDS] or \
                    [w for w in re.findall(r"[A-Za-z0-9]+", text.lower())]
            if len(words) > 1:
                candidates = self._search(spec, words)
            if not candidates and len(words) > 1:
                candidates = self._union_search(spec, words)
        return self._rank(candidates, text)[: self._max]

    # ---- exact key ------------------------------------------------------

    def _key_lookup(self, spec: LookupSpec, code: str) -> Candidate | None:
        entity_set = spec.key_entity_set or spec.entity_set
        path = f"{spec.service_root}/{entity_set}('{code.replace(chr(39), chr(39) * 2)}')"
        try:
            data = self._client.get(path, context=f"validating {spec.entity_type} code {code}")
        except SAPRequestError as exc:
            if exc.status_code == 404:
                return None
            raise
        record = data.get("d", data)
        description = str(record.get(spec.description_field, "")).strip()
        if not description and spec.key_entity_set:
            # e.g. products: the name lives on the description entity set.
            hits = self._search(spec, [], filter_override=f"{spec.code_field} eq '{code}'")
            description = hits[0].description if hits else ""
        return Candidate(code=code, description=description, match="exact-code")

    # ---- description search ---------------------------------------------

    def _search(self, spec: LookupSpec, words: list[str],
                filter_override: str = "") -> list[Candidate]:
        """One $filter query; retries without tolower() if the service
        rejects it (not every SAP service enables that function)."""
        for case_insensitive in (True, False):
            clauses = [self._contains_clause(spec, w, case_insensitive) for w in words]
            if filter_override:
                clauses = [filter_override]
            if spec.extra_filter:
                clauses.append(spec.extra_filter)
            params = {
                "$filter": " and ".join(clauses),
                "$top": str(self._max * 4),
                "$select": f"{spec.code_field},{spec.description_field}",
            }
            try:
                data = self._client.get(
                    f"{spec.service_root}/{spec.entity_set}", params=params,
                    context=f"searching {spec.entity_type}s",
                )
            except SAPRequestError as exc:
                if exc.status_code == 400 and case_insensitive and not filter_override:
                    continue  # retry with the case-sensitive variant
                raise
            return [
                Candidate(
                    code=str(r.get(spec.code_field, "")).strip(),
                    description=str(r.get(spec.description_field, "")).strip(),
                    match="name-match",
                )
                for r in self._rows(data)
                if r.get(spec.code_field)
            ]
        return []

    def _union_search(self, spec: LookupSpec, words: list[str]) -> list[Candidate]:
        """Last resort: candidates matching ANY word, best-covered first."""
        seen: dict[str, tuple[int, Candidate]] = {}
        for word in words:
            for cand in self._search(spec, [word]):
                count, _ = seen.get(cand.code, (0, cand))
                seen[cand.code] = (count + 1, cand)
        ranked = sorted(seen.values(), key=lambda item: -item[0])
        return [
            Candidate(code=c.code, description=c.description,
                      match="name-match" if count == len(words) else "partial")
            for count, c in ranked
        ]

    @staticmethod
    def _contains_clause(spec: LookupSpec, needle: str, case_insensitive: bool) -> str:
        escaped = (needle.lower() if case_insensitive else needle).replace("'", "''")
        field = f"tolower({spec.description_field})" if case_insensitive \
            else spec.description_field
        if spec.odata_version >= 4:
            return f"contains({field},'{escaped}')"
        return f"substringof('{escaped}', {field})"

    @staticmethod
    def _rows(data: dict) -> list[dict]:
        # OData v2 wraps results as {"d": {"results": [...]}}; v4 as {"value": [...]}.
        d = data.get("d")
        if isinstance(d, dict):
            return d.get("results") or []
        return data.get("value") or []

    @staticmethod
    def _rank(candidates: list[Candidate], query: str) -> list[Candidate]:
        query_lower = query.lower()

        def score(c: Candidate) -> tuple:
            name = c.description.lower()
            if name == query_lower:
                return (0, len(name))
            if name.startswith(query_lower):
                return (1, len(name))
            return (2 if c.match != "partial" else 3, len(name))

        deduped: dict[str, Candidate] = {}
        for cand in candidates:
            deduped.setdefault(cand.code, cand)
        ranked = sorted(deduped.values(), key=score)
        return [
            Candidate(c.code, c.description, "exact-name")
            if c.description.lower() == query_lower and c.match == "name-match" else c
            for c in ranked
        ]


def build_specs(settings) -> dict[str, LookupSpec]:
    """The three POC lookup types. GR/Invoice later add specs here (e.g.
    cost centers), not new resolver code."""
    return {
        "supplier": LookupSpec(
            entity_type="supplier",
            service_root=settings.sap_supplier_service,
            entity_set="A_Supplier",
            code_field="Supplier",
            description_field="SupplierName",
            odata_version=2,
        ),
        "material": LookupSpec(
            entity_type="material",
            service_root=settings.sap_product_service,
            entity_set="A_ProductDescription",
            code_field="Product",
            description_field="ProductDescription",
            odata_version=2,
            extra_filter=f"Language eq '{settings.sap_material_search_language}'",
            key_entity_set="A_Product",
        ),
        "plant": LookupSpec(
            entity_type="plant",
            service_root=settings.sap_plant_service,
            entity_set="Plant",
            code_field="Plant",
            description_field="PlantName",
            odata_version=4,
        ),
    }
