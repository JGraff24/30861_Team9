"""Cell style, theme and palette resolution for the color/contrast screen (section 7.7).

The workstation's Excel theme is never consulted: theme colors come from the workbook's
own theme part. Anything that cannot be resolved to one concrete RGB value is returned as
an unsupported reason so the rule can ask for manual review instead of guessing.
"""

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from xml.etree.ElementTree import Element

from app.workbooks.package import OoxmlPackage
from app.workbooks.xmlsafe import local_name

RGB = tuple[int, int, int]
THEME_INDEX_NAMES = (
    "lt1",
    "dk1",
    "lt2",
    "dk2",
    "accent1",
    "accent2",
    "accent3",
    "accent4",
    "accent5",
    "accent6",
    "hlink",
    "folHlink",
)
#: Frozen fallback indexed palette, indices 0-63 (phase4-policy-vectors.json).
DEFAULT_INDEXED = (
    "000000 FFFFFF FF0000 00FF00 0000FF FFFF00 FF00FF 00FFFF "
    "000000 FFFFFF FF0000 00FF00 0000FF FFFF00 FF00FF 00FFFF "
    "800000 008000 000080 808000 800080 008080 C0C0C0 808080 "
    "9999FF 993366 FFFFCC CCFFFF 660066 FF8080 0066CC CCCCFF "
    "000080 FF00FF FFFF00 00FFFF 800080 800000 008080 0000FF "
    "00CCFF CCFFFF CCFFCC FFFF99 99CCFF FF99CC CC99FF FFCC99 "
    "3366FF 33CCCC 99CC00 FFCC00 FF9900 FF6600 666699 969696 "
    "003366 339966 003300 333300 993300 993366 333399 333333"
).split()
TRUE_VALUES = frozenset({"1", "true"})


def hex_rgb(text: str) -> RGB | None:
    text = text.strip()
    if len(text) == 8:
        text = text[2:]
    if len(text) != 6:
        return None
    try:
        value = int(text, 16)
    except ValueError:
        return None
    return (value >> 16) & 0xFF, (value >> 8) & 0xFF, value & 0xFF


def rgb_hex(rgb: RGB) -> str:
    return "".join(f"{channel:02X}" for channel in rgb)


def _hue_to_rgb(p: Fraction, q: Fraction, t: Fraction) -> Fraction:
    if t < 0:
        t += 1
    if t > 1:
        t -= 1
    if t < Fraction(1, 6):
        return p + (q - p) * 6 * t
    if t < Fraction(1, 2):
        return q
    if t < Fraction(2, 3):
        return p + (q - p) * (Fraction(2, 3) - t) * 6
    return p


def apply_tint(rgb: RGB, tint: Fraction) -> RGB:
    """HSL lightness tint with exact rational arithmetic and half-up rounding.

    Negative tint: L*(1+t); positive tint: L*(1-t)+t. Channels are clamped and rounded to
    the nearest integer with exact halves rounded upward (specification section 7.7).
    """
    if tint == 0:
        return rgb
    r, g, b = (Fraction(channel, 255) for channel in rgb)
    high, low = max(r, g, b), min(r, g, b)
    lightness = (high + low) / 2
    if high == low:
        hue = saturation = Fraction(0)
    else:
        delta = high - low
        saturation = (
            delta / (2 - high - low) if lightness > Fraction(1, 2) else delta / (high + low)
        )
        if high == r:
            hue = ((g - b) / delta) % 6
        elif high == g:
            hue = (b - r) / delta + 2
        else:
            hue = (r - g) / delta + 4
        hue /= 6
    lightness = lightness * (1 + tint) if tint < 0 else lightness * (1 - tint) + tint
    lightness = min(max(lightness, Fraction(0)), Fraction(1))
    if saturation == 0:
        channels = (lightness, lightness, lightness)
    else:
        q = (
            lightness * (1 + saturation)
            if lightness < Fraction(1, 2)
            else lightness + saturation - lightness * saturation
        )
        p = 2 * lightness - q
        channels = (
            _hue_to_rgb(p, q, hue + Fraction(1, 3)),
            _hue_to_rgb(p, q, hue),
            _hue_to_rgb(p, q, hue - Fraction(1, 3)),
        )
    result = []
    for channel in channels:
        scaled = min(max(channel * 255, Fraction(0)), Fraction(255))
        result.append(int(scaled + Fraction(1, 2)))  # floor(x + 1/2): halves round up
    return result[0], result[1], result[2]


@dataclass(frozen=True)
class ColorSpec:
    rgb: str | None = None
    theme: str | None = None
    indexed: str | None = None
    auto: bool = False
    tint: str | None = None


def color_spec(element: Element | None) -> ColorSpec | None:
    if element is None:
        return None
    return ColorSpec(
        element.get("rgb"),
        element.get("theme"),
        element.get("indexed"),
        element.get("auto", "").strip() in TRUE_VALUES,
        element.get("tint"),
    )


@dataclass(frozen=True)
class Resolved:
    rgb: RGB | None
    reason: str | None  # why no concrete color could be established


@dataclass(frozen=True)
class FontStyle:
    color: ColorSpec | None
    size: Fraction
    bold: bool
    size_valid: bool = True


@dataclass(frozen=True)
class FillStyle:
    kind: str  # none, solid, pattern, gradient
    color: ColorSpec | None


@dataclass(frozen=True)
class CellFormat:
    font_id: int | None
    fill_id: int | None
    ambiguous: str | None  # an inheritance conflict that prevents a confident result


@dataclass(frozen=True)
class DifferentialFormat:
    changes_color: bool


@dataclass
class Stylesheet:
    fonts: list[FontStyle] = field(default_factory=list)
    fills: list[FillStyle] = field(default_factory=list)
    cell_formats: list[CellFormat] = field(default_factory=list)
    differential: list[DifferentialFormat] = field(default_factory=list)
    palette: list[str] = field(default_factory=lambda: list(DEFAULT_INDEXED))
    custom_palette: bool = False
    theme: dict[str, RGB | None] = field(default_factory=dict)
    has_theme: bool = False

    def resolve(self, spec: ColorSpec) -> Resolved:
        sources = sum(
            (spec.rgb is not None, spec.theme is not None, spec.indexed is not None, spec.auto)
        )
        if sources > 1:
            return Resolved(None, "conflicting color sources")
        if spec.auto:
            return Resolved(None, "automatic color without a concrete fallback")
        rgb: RGB | None
        if spec.rgb is not None:
            rgb = hex_rgb(spec.rgb)
            if rgb is None:
                return Resolved(None, "unreadable RGB color")
        elif spec.theme is not None:
            try:
                index = int(spec.theme)
            except ValueError:
                return Resolved(None, "unreadable theme color index")
            if not 0 <= index < len(THEME_INDEX_NAMES) or not self.has_theme:
                return Resolved(None, "theme color not defined by the workbook")
            rgb = self.theme.get(THEME_INDEX_NAMES[index])
            if rgb is None:
                return Resolved(None, "theme color without a concrete value")
        elif spec.indexed is not None:
            try:
                index = int(spec.indexed)
            except ValueError:
                return Resolved(None, "unreadable indexed color")
            if not 0 <= index < len(self.palette):
                return Resolved(None, "system or undefined indexed color")
            rgb = hex_rgb(self.palette[index])
            if rgb is None:
                return Resolved(None, "unreadable palette color")
        else:
            return Resolved(None, "color element without a value")
        if spec.tint is not None:
            try:
                tint = Fraction(Decimal(spec.tint.strip()))
            except (InvalidOperation, ValueError):
                return Resolved(None, "unreadable color tint")
            if not -1 <= tint <= 1:
                return Resolved(None, "color tint outside -1 to 1")
            rgb = apply_tint(rgb, tint)
        return Resolved(rgb, None)


def _int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return -1


def _child(element: Element, name: str) -> Element | None:
    return next((child for child in element if local_name(child.tag) == name), None)


def _font(element: Element) -> FontStyle:
    size_element = _child(element, "sz")
    size = Fraction(11)
    valid = True
    if size_element is not None:
        try:
            size = Fraction(Decimal(size_element.get("val", "").strip()))
        except (InvalidOperation, ValueError):
            valid = False
    bold_element = _child(element, "b")
    bold = bold_element is not None and bold_element.get("val", "1").strip() in TRUE_VALUES
    return FontStyle(color_spec(_child(element, "color")), size, bold, valid)


def _fill(element: Element) -> FillStyle:
    pattern = _child(element, "patternFill")
    if _child(element, "gradientFill") is not None:
        return FillStyle("gradient", None)
    if pattern is None:
        return FillStyle("none", None)
    kind = pattern.get("patternType", "none")
    if kind == "none":
        return FillStyle("none", None)
    if kind == "solid":
        return FillStyle("solid", color_spec(_child(pattern, "fgColor")))
    return FillStyle("pattern", None)


def _formats(container: Element | None) -> list[Element]:
    if container is None:
        return []
    return [child for child in container if local_name(child.tag) == "xf"]


def read_stylesheet(
    package: OoxmlPackage, styles_part: str | None, theme_part: str | None
) -> Stylesheet:
    sheet = Stylesheet()
    if theme_part is not None:
        sheet.has_theme = True
        root = package.parse_xml(theme_part)
        schemes = [node for node in root.iter() if local_name(node.tag) == "clrScheme"]
        if schemes:
            for entry in schemes[0]:
                name = local_name(entry.tag)
                value: RGB | None = None
                for color in entry:
                    color_name = local_name(color.tag)
                    if color_name == "srgbClr":
                        value = hex_rgb(color.get("val", ""))
                    elif color_name == "sysClr":
                        value = hex_rgb(color.get("lastClr", ""))
                sheet.theme[name] = value
        else:
            sheet.has_theme = False
    if styles_part is None:
        sheet.fonts = [FontStyle(None, Fraction(11), False)]
        sheet.fills = [FillStyle("none", None)]
        sheet.cell_formats = [CellFormat(0, 0, None)]
        return sheet
    root = package.parse_xml(styles_part)
    sections = {local_name(child.tag): child for child in root}
    fonts = sections.get("fonts")
    sheet.fonts = (
        [_font(font) for font in fonts if local_name(font.tag) == "font"]
        if fonts is not None
        else []
    )
    fills = sections.get("fills")
    sheet.fills = (
        [_fill(fill) for fill in fills if local_name(fill.tag) == "fill"]
        if fills is not None
        else []
    )
    bases = _formats(sections.get("cellStyleXfs"))
    for xf in _formats(sections.get("cellXfs")):
        font_id, fill_id = _int(xf.get("fontId")), _int(xf.get("fillId"))
        ambiguous = None
        base_id = _int(xf.get("xfId"))
        base = bases[base_id] if base_id is not None and 0 <= base_id < len(bases) else None
        if base_id is not None and base is None:
            ambiguous = "cell style refers to an undefined base style"
        elif base is not None:
            for attribute, flag in (("fontId", "applyFont"), ("fillId", "applyFill")):
                applied = xf.get(flag)
                own, inherited = xf.get(attribute), base.get(attribute)
                if applied is not None and applied.strip() not in TRUE_VALUES and own != inherited:
                    ambiguous = "cell style disables a differing font or fill"
        sheet.cell_formats.append(CellFormat(font_id, fill_id, ambiguous))
    dxfs = sections.get("dxfs")
    if dxfs is not None:
        for dxf in dxfs:
            if local_name(dxf.tag) != "dxf":
                continue
            font = _child(dxf, "font")
            changes = _child(dxf, "fill") is not None or (
                font is not None and _child(font, "color") is not None
            )
            sheet.differential.append(DifferentialFormat(changes))
    colors = sections.get("colors")
    indexed = _child(colors, "indexedColors") if colors is not None else None
    if indexed is not None:
        custom = [node.get("rgb", "") for node in indexed if local_name(node.tag) == "rgbColor"]
        if custom:
            sheet.palette = [value[-6:] for value in custom]
            sheet.custom_palette = True
    return sheet
