"""Tally HTTP-XML gateway client.

Tally Prime exposes an XML-over-HTTP gateway (default port 9000). We send
``Export`` requests for ``Collection`` objects and receive XML back.

Robustness choices:
* Every response is written verbatim to ``data/raw/<name>.xml`` first, so we
  always hold a complete, auditable snapshot of the source even if downstream
  parsing needs refinement (supports the "capture all records" guarantee).
* Tally emits a handful of illegal XML control characters and uses ``&#4;``
  style entities as field separators; we strip/repair these before parsing.
"""
from __future__ import annotations

import re
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

from .config import DATA_DIR, get_config

# Control chars that are illegal in XML 1.0 (except tab/newline/carriage return).
_ILLEGAL_XML = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f]"
)
# Numeric character references (e.g. Tally's "&#4;" field separators).
_NUM_REF = re.compile(r"&#(x[0-9a-fA-F]+|[0-9]+);")


def _legal_xml_codepoint(cp: int) -> bool:
    # XML 1.0 valid char ranges.
    return (
        cp in (0x9, 0xA, 0xD)
        or 0x20 <= cp <= 0xD7FF
        or 0xE000 <= cp <= 0xFFFD
        or 0x10000 <= cp <= 0x10FFFF
    )


def _strip_bad_num_ref(m: re.Match) -> str:
    body = m.group(1)
    cp = int(body[1:], 16) if body[0] in "xX" else int(body)
    return m.group(0) if _legal_xml_codepoint(cp) else ""


# Namespace-style prefixes Tally emits without declaring (e.g. <UDF:FOO>).
_NS_TAG = re.compile(r"(</?)([A-Za-z_][\w.-]*):")


def sanitize_xml(text: str) -> str:
    # Tally uses bare '&', stray control chars, and illegal numeric refs.
    text = _ILLEGAL_XML.sub("", text)
    # Drop numeric character references pointing at illegal codepoints (&#4; etc).
    text = _NUM_REF.sub(_strip_bad_num_ref, text)
    # Neutralize undeclared namespace prefixes in tag names (UDF: -> UDF_).
    text = _NS_TAG.sub(r"\1\2_", text)
    # Bare ampersands not part of an entity -> &amp;
    text = re.sub(r"&(?!#?\w+;)", "&amp;", text)
    return text


def _envelope(report_id: str, body_desc: str, company: str,
              from_date: str | None = None, to_date: str | None = None) -> str:
    sv = [f"<SVCURRENTCOMPANY>{company}</SVCURRENTCOMPANY>",
          "<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>"]
    if from_date:
        sv.append(f'<SVFROMDATE TYPE="Date">{from_date}</SVFROMDATE>')
    if to_date:
        sv.append(f'<SVTODATE TYPE="Date">{to_date}</SVTODATE>')
    return (
        '<ENVELOPE><HEADER><VERSION>1</VERSION>'
        '<TALLYREQUEST>Export</TALLYREQUEST><TYPE>Collection</TYPE>'
        f'<ID>{report_id}</ID></HEADER><BODY><DESC>'
        f'<STATICVARIABLES>{"".join(sv)}</STATICVARIABLES>'
        f'{body_desc}</DESC></BODY></ENVELOPE>'
    )


class TallyClient:
    def __init__(self) -> None:
        cfg = get_config()
        self.url = cfg.tally["url"]
        self.company = cfg.tally["company"]
        self.timeout = int(cfg.tally.get("request_timeout", 180))
        self.from_date = str(cfg.tally.get("from_date", ""))
        self.to_date = str(cfg.tally.get("to_date", ""))
        self.raw_dir: Path = DATA_DIR / "raw"

    def _post(self, payload: str, retries: int = 3) -> str:
        last_err: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                resp = requests.post(
                    self.url, data=payload.encode("utf-8"),
                    headers={"Content-Type": "text/xml; charset=utf-8"},
                    timeout=self.timeout,
                )
                resp.raise_for_status()
                return resp.text
            except requests.RequestException as exc:  # noqa: PERF203
                last_err = exc
                time.sleep(2 * attempt)
        raise RuntimeError(f"Tally request failed after {retries} attempts: {last_err}")

    def export_collection(
        self,
        name: str,
        coll_type: str,
        methods: list[str] | None = None,
        fetch: list[str] | None = None,
        dated: bool = False,
        filters: dict[str, str] | None = None,
        save_as: str | None = None,
    ) -> ET.Element:
        """Export a custom collection and return the parsed root element.

        ``methods``  -> <NATIVEMETHOD> entries (scalar fields)
        ``fetch``    -> <FETCH> entries (used to pull nested list fields verbatim)
        ``filters``  -> {name: formula} system filters applied to the collection
        """
        parts = [f'<COLLECTION NAME="{name}" ISMODIFY="No" ISFIXED="No">',
                 f"<TYPE>{coll_type}</TYPE>"]
        for m in methods or []:
            parts.append(f"<NATIVEMETHOD>{m}</NATIVEMETHOD>")
        if fetch:
            parts.append(f"<FETCH>{','.join(fetch)}</FETCH>")
        sys_filters = ""
        if filters:
            names = ",".join(filters)
            parts.append(f"<FILTER>{names}</FILTER>")
            sys_filters = "".join(
                f'<SYSTEM TYPE="Formulae" NAME="{n}">{f}</SYSTEM>'
                for n, f in filters.items()
            )
        parts.append("</COLLECTION>")
        body = f"<TDL><TDLMESSAGE>{''.join(parts)}{sys_filters}</TDLMESSAGE></TDL>"

        envelope = _envelope(
            name, body, self.company,
            self.from_date if dated else None,
            self.to_date if dated else None,
        )
        raw = self._post(envelope)

        out = self.raw_dir / f"{save_as or name}.xml"
        out.write_text(raw, encoding="utf-8")

        cleaned = sanitize_xml(raw)
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError as exc:
            # Surface where parsing broke; raw file is on disk for inspection.
            raise RuntimeError(
                f"Failed to parse Tally response for '{name}': {exc}. "
                f"Raw XML saved at {out}"
            ) from exc

    def export_daybook(self, from_date: str, to_date: str,
                       save_as: str) -> ET.Element:
        """Export vouchers via the native 'Day Book' report with EXPLODEFLAG.

        This is far faster and more complete than a custom Voucher collection:
        Tally streams every voucher in the window with full ledger entries,
        bill allocations, and inventory entries in its standard XML format.
        """
        envelope = (
            '<ENVELOPE><HEADER><TALLYREQUEST>Export Data</TALLYREQUEST></HEADER>'
            '<BODY><EXPORTDATA><REQUESTDESC>'
            '<REPORTNAME>Day Book</REPORTNAME>'
            '<STATICVARIABLES>'
            '<SVEXPORTFORMAT>$$SysName:XML</SVEXPORTFORMAT>'
            f'<SVCURRENTCOMPANY>{self.company}</SVCURRENTCOMPANY>'
            f'<SVFROMDATE TYPE="Date">{from_date}</SVFROMDATE>'
            f'<SVTODATE TYPE="Date">{to_date}</SVTODATE>'
            '<EXPLODEFLAG>Yes</EXPLODEFLAG>'
            '</STATICVARIABLES></REQUESTDESC></EXPORTDATA></BODY></ENVELOPE>'
        )
        raw = self._post(envelope)
        out = self.raw_dir / f"{save_as}.xml"
        out.write_text(raw, encoding="utf-8")
        cleaned = sanitize_xml(raw)
        try:
            return ET.fromstring(cleaned)
        except ET.ParseError as exc:
            raise RuntimeError(
                f"Failed to parse Day Book {from_date}-{to_date}: {exc}. "
                f"Raw XML saved at {out}"
            ) from exc
