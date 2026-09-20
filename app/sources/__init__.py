"""Gemeinsame Helfer für die Quellen."""

import re
import xml.etree.ElementTree as ET

# DTDs und selbst definierte Entitäten kommen in den Quelldaten nicht vor, können aber zum Aufblähen
# des Speichers missbraucht werden („billion laughs“). Sie werden deshalb abgelehnt.
_DOCTYPE = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)", re.IGNORECASE)
_PROLOG_BYTES = 8192  # so weit vorne steht ein DOCTYPE in wohlgeformtem XML


def parse_xml(content: bytes | str) -> ET.Element:
    """XML einlesen – ohne DTD/Entitäten. Wirft ValueError, wenn doch eine enthalten ist."""
    raw = content.encode("utf-8") if isinstance(content, str) else content
    if _DOCTYPE.search(raw[:_PROLOG_BYTES]):
        raise ValueError("XML mit DTD oder Entitäten wird nicht verarbeitet")
    return ET.fromstring(raw)
