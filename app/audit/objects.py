"""Drawing-like object inventories per sheet and package (shared by both rulesets)."""

from app.workbooks.drawings import (
    DrawingInventory,
    UnsupportedObject,
    inventory_drawing,
    inventory_vml,
)
from app.workbooks.package import OoxmlPackage, Relationship, rel_type_is
from app.workbooks.structure import SheetInfo, SheetReference


def sheet_inventories(package: OoxmlPackage, sheet: SheetInfo) -> list[DrawingInventory]:
    rels = package.relationships(sheet.part)
    inventories: list[DrawingInventory] = []
    for ref in sheet.references:
        rel = rels.get(ref.rel_id or "")
        resolved = rel.resolved if rel is not None and not rel.external else None
        if ref.element == "drawing":
            if resolved is not None and rel is not None and rel_type_is(rel.rel_type, "drawing"):
                inventories.append(inventory_drawing(package, resolved))
            else:
                inventories.append(_review_reference(sheet, ref, rel))
            continue
        if resolved is not None and resolved.casefold().endswith(".vml"):
            inventory = inventory_vml(package, resolved)
            if inventory.objects or inventory.unsupported:
                inventories.append(inventory)
                continue
        inventories.append(_review_reference(sheet, ref, rel))
    if sheet.rich_value_cells or sheet.rich_value_cells_omitted:
        inventories.append(_cell_image_inventory(sheet))
    return inventories


def _cell_image_inventory(sheet: SheetInfo) -> DrawingInventory:
    """Cells holding an in-cell picture, an IMAGE() result or another rich value."""
    limitation = (
        f"Worksheet part {sheet.part} has cell images or rich values; their contents were "
        "not inspected."
    )
    unsupported = [
        UnsupportedObject(
            sheet.part,
            cell.path,
            f"In-cell picture or rich value{f' in {cell.ref}' if cell.ref else ''}",
            limitation,
        )
        for cell in sheet.rich_value_cells
    ]
    extra = []
    if sheet.rich_value_cells_omitted:
        extra.append(
            f"Worksheet part {sheet.part} has {sheet.rich_value_cells_omitted} more cell "
            "image(s) beyond the listed limit; review them manually."
        )
    return DrawingInventory([], unsupported, extra)


def package_inventory(package: OoxmlPackage) -> DrawingInventory:
    """Cell-image and rich-data parts, reported at workbook level (pod contract 1.1 §8)."""
    unsupported = [
        UnsupportedObject(
            name,
            (),
            "Cell-image or rich-data part",
            f"Package part {name}: cell-image/rich-data representations are not assessed.",
            part_root=True,
        )
        for name in package.entry_names
        if name.startswith("xl/richData/") or name == "xl/cellimages.xml"
    ]
    return DrawingInventory([], unsupported, [])


REFERENCE_LABELS = {
    "drawing": "Drawing reference",
    "legacyDrawing": "Legacy drawing reference",
    "legacyDrawingHF": "Header/footer drawing reference",
    "picture": "Worksheet background image",
    "control": "Form control",
    "oleObject": "Embedded OLE object",
}


def _review_reference(
    sheet: SheetInfo, ref: SheetReference, rel: Relationship | None
) -> DrawingInventory:
    label = REFERENCE_LABELS[ref.element]
    if rel is None or rel.resolved is None and not rel.external:
        detail = "its relationship does not resolve to a part"
    elif rel.external:
        detail = "external content is not fetched"
    else:
        detail = "it is not automatically assessed"
    return DrawingInventory(
        [],
        [UnsupportedObject(sheet.part, ref.path, label, f"{label} in {sheet.part}: {detail}.")],
        [],
    )
