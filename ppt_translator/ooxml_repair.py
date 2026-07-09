"""
Post-save OOXML relationship repair.

python-pptx (used to write translated decks) can silently drop package
relationships it does not model. The most common victim is an embedded-font
relationship whose ``Target`` is the literal ``"NULL"`` placeholder used for a
font style that is not actually embedded. When the relationship is dropped but
the element that references it (e.g. ``<p:embeddedFont>`` in
``presentation.xml``) is kept, the package ends up with a *dangling* relationship
reference. PowerPoint then reports the file as corrupt and offers to "Repair" it.

This module restores such dropped relationships by copying their definitions
back from the original (pre-translation) file, which is the source of truth.
It is intentionally best-effort and byte-conservative: every part that is not
affected is copied through unchanged, and if there is nothing to fix the output
file is left exactly as-is.
"""
import logging
import posixpath
import shutil
import zipfile
from typing import Dict, List, Set

from lxml import etree

logger = logging.getLogger(__name__)

# Namespace used for relationship references on elements (r:id, r:embed, ...).
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# Namespace used inside .rels parts.
_PKG_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_REL_TAG = "{%s}Relationship" % _PKG_NS
_R_PREFIX = "{%s}" % _R_NS


def _rels_path(part_name: str) -> str:
    """Return the ``.rels`` part path that governs *part_name*."""
    return posixpath.join(
        posixpath.dirname(part_name), "_rels", posixpath.basename(part_name) + ".rels"
    )


def _defined_rel_ids(zf: zipfile.ZipFile, rels_path: str, names: Set[str]) -> Set[str]:
    if rels_path not in names:
        return set()
    root = etree.fromstring(zf.read(rels_path))
    return {r.get("Id") for r in root.iter(_REL_TAG)}


def _referenced_rel_ids(root: etree._Element) -> Set[str]:
    ids: Set[str] = set()
    for el in root.iter():
        for key, val in el.attrib.items():
            if key.startswith(_R_PREFIX):
                ids.add(val)
    return ids


def _find_dangling(zf: zipfile.ZipFile, names: Set[str]) -> Dict[str, Set[str]]:
    """Map ``part -> {rIds referenced but not defined in its .rels}``."""
    dangling: Dict[str, Set[str]] = {}
    for name in names:
        if not name.endswith(".xml") or name == "[Content_Types].xml":
            continue
        try:
            root = etree.fromstring(zf.read(name))
        except etree.XMLSyntaxError:
            continue
        refs = _referenced_rel_ids(root)
        if not refs:
            continue
        missing = refs - _defined_rel_ids(zf, _rels_path(name), names)
        if missing:
            dangling[name] = missing
    return dangling


def _relationship_xml(rid: str, rtype: str, target: str, mode: str) -> str:
    mode_attr = ' TargetMode="%s"' % mode if mode else ""
    return '<Relationship Id="%s" Type="%s" Target="%s"%s/>' % (
        rid, rtype, target, mode_attr
    )


def repair_relationships(original_pptx: str, output_pptx: str) -> Dict[str, List[str]]:
    """Restore relationships dropped from *output_pptx*, sourced from *original_pptx*.

    Returns ``{part_name: [restored_rId, ...]}``. An empty dict means the file
    had no dangling references and was left untouched.

    This function is best-effort. Callers should treat it as such and not let a
    repair failure abort an otherwise successful translation.
    """
    with zipfile.ZipFile(output_pptx) as zout:
        out_names = set(zout.namelist())
        dangling = _find_dangling(zout, out_names)
        if not dangling:
            return {}

        patched_rels: Dict[str, bytes] = {}   # rels part path -> new bytes
        parts_to_add: Dict[str, bytes] = {}   # missing target part -> bytes
        restored: Dict[str, List[str]] = {}

        with zipfile.ZipFile(original_pptx) as zorig:
            orig_names = set(zorig.namelist())
            for part, missing in dangling.items():
                rels_path = _rels_path(part)
                if rels_path not in orig_names:
                    logger.warning("repair: no original rels for %s; skipping", part)
                    continue
                orig_map = {
                    r.get("Id"): r
                    for r in etree.fromstring(zorig.read(rels_path)).iter(_REL_TAG)
                }
                snippets: List[str] = []
                for rid in sorted(missing):
                    rel = orig_map.get(rid)
                    if rel is None:
                        logger.warning(
                            "repair: %s not found in original rels for %s", rid, part
                        )
                        continue
                    target = rel.get("Target")
                    mode = rel.get("TargetMode")
                    snippets.append(_relationship_xml(rid, rel.get("Type"), target, mode))
                    restored.setdefault(part, []).append(rid)
                    # If the relationship points at an internal part that the
                    # output is missing, bring that part across too.
                    if mode != "External":
                        resolved = posixpath.normpath(
                            posixpath.join(posixpath.dirname(part), target)
                        )
                        if resolved not in out_names and resolved in orig_names:
                            parts_to_add[resolved] = zorig.read(resolved)
                if snippets:
                    base = patched_rels.get(rels_path) or zout.read(rels_path)
                    patched_rels[rels_path] = base.replace(
                        b"</Relationships>",
                        "".join(snippets).encode("utf-8") + b"</Relationships>",
                    )

        if not restored:
            return {}

        # Rewrite the package: patched .rels get new bytes, missing targets are
        # appended, everything else is copied through byte-for-byte.
        tmp_path = output_pptx + ".repair.tmp"
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zw:
            for item in zout.infolist():
                if item.filename in patched_rels:
                    zw.writestr(item, patched_rels[item.filename])
                else:
                    zw.writestr(item, zout.read(item.filename))
            for pname, pdata in parts_to_add.items():
                if pname not in out_names:
                    zw.writestr(pname, pdata)

    shutil.move(tmp_path, output_pptx)
    total = sum(len(v) for v in restored.values())
    logger.info(
        "repair: restored %d dropped relationship(s) across %d part(s)",
        total,
        len(restored),
    )
    return restored
