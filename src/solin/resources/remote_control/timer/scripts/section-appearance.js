import { element, uiIcon } from "../../shared/scripts/dom.js";
import { t } from "../../shared/scripts/i18n.js";
import { applySectionColor, isSectionColor, sectionCatalog, sectionColorSwatch, sectionMark } from "./section-mark.js";

function tileGroup({ name, label, choices, selected, onSelect, color = false }) {
  const fieldset = element("fieldset", { className: "appearance-fieldset" });
  fieldset.append(element("legend", { text: label }));
  const tiles = element("div", { className: `appearance-tiles${color ? " appearance-swatches" : ""}` });
  for (const { value, label, glyph } of choices) {
    const radio = element("input", {
      className: "visually-hidden",
      attrs: { type: "radio", name, value, "aria-label": label },
    });
    radio.checked = value === selected;
    radio.addEventListener("change", () => onSelect(value));
    const tile = element("label", { className: "appearance-tile", attrs: { title: label } });
    tile.append(radio, glyph);
    tiles.append(tile);
  }
  fieldset.append(tiles);
  return fieldset;
}

export function sectionAppearanceFields(container, name, { icon = "default", color = "" } = {}) {
  const { icons, colors } = sectionCatalog();
  let selectedIcon = icon;
  const preview = element("div", { className: "section-appearance-preview", dataset: { section: "" }, attrs: { "aria-hidden": "true" } });
  const badge = element("span", { className: "section-icon-badge" });
  const title = element("strong", { className: "section-title" });
  preview.append(badge, title);
  const hex = element("input", {
    attrs: { type: "text", placeholder: "#41b6c4", maxlength: 7, autocomplete: "off", spellcheck: "false" },
  });
  hex.value = color ?? "";
  const hexField = element("label", { className: "field appearance-hex", text: t("scheduleEditor.customHex") });
  hexField.append(hex);
  const readColor = () => {
    const value = hex.value.trim().toLowerCase();
    return value && !value.startsWith("#") ? `#${value}` : value;
  };
  let previewColor = readColor();
  const renderPreview = () => {
    const value = readColor();
    if (!value || isSectionColor(value)) previewColor = value;
    const mark = sectionMark(selectedIcon, previewColor);
    applySectionColor(preview, previewColor);
    badge.hidden = !mark;
    badge.replaceChildren(...(mark ? [mark] : []));
    title.textContent = name.value.trim() || t("scheduleEditor.sectionPreview");
  };
  const colorTiles = tileGroup({
    name: "sectionColor", label: t("scheduleEditor.color"),
    selected: previewColor, color: true,
    choices: [{ value: "", label: t("scheduleEditor.default"), glyph: uiIcon("ban") }, ...colors.map(([key, value]) => ({
      value, label: t(`scheduleEditor.color.${key}`), glyph: sectionColorSwatch(value),
    }))],
    onSelect: (value) => { hex.value = value; renderPreview(); },
  });
  hex.addEventListener("input", () => {
    for (const radio of colorTiles.querySelectorAll("input")) radio.checked = radio.value === readColor();
    renderPreview();
  });
  name.addEventListener("input", renderPreview);
  const iconTiles = tileGroup({
    name: "sectionIcon", label: t("scheduleEditor.icon"), selected: selectedIcon,
    choices: icons.map((value) => ({
      value,
      label: t(`scheduleEditor.${value === "default" || value === "none" ? value : `icon.${value}`}`),
      glyph: sectionMark(value) ?? uiIcon("ban"),
    })),
    onSelect: (value) => { selectedIcon = value; renderPreview(); },
  });
  container.append(preview, colorTiles, hexField, iconTiles);
  renderPreview();
  return { hex, readColor, readIcon: () => selectedIcon };
}
