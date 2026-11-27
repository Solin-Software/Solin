import { svgElement, uiIcon } from "../../shared/scripts/dom.js";

let catalog;

export function sectionCatalog() {
  if (!catalog) {
    const { icons, colors } = document.getElementById("timer-section-catalog").dataset;
    catalog = { icons: ["default", "none", ...JSON.parse(icons)], colors: JSON.parse(colors) };
  }
  return catalog;
}

export const isSectionColor = (color) => typeof color === "string" && /^#[0-9a-f]{6}$/i.test(color);

export function sectionColorSwatch(color) {
  const swatch = svgElement("svg", {
    className: "ui-icon section-color-swatch",
    attrs: { viewBox: "0 0 24 24", "aria-hidden": "true" },
  });
  swatch.append(svgElement("circle", { attrs: { cx: 12, cy: 12, r: 9, fill: color } }));
  return swatch;
}

export function applySectionColor(node, color) {
  if (!isSectionColor(color)) {
    delete node.dataset.sectionTone;
    return;
  }
  const rgb = (hex) => [1, 3, 5].map((offset) => parseInt(hex.slice(offset, offset + 2), 16));
  const target = rgb(color);
  let nearestTone = "";
  let nearestDistance = Infinity;
  for (const [tone, preset] of sectionCatalog().colors) {
    const distance = rgb(preset).reduce((total, value, index) => total + (value - target[index]) ** 2, 0);
    if (distance < nearestDistance) {
      nearestTone = tone;
      nearestDistance = distance;
    }
  }
  node.dataset.sectionTone = nearestTone;
}

export function sectionMark(icon, color) {
  const hasColor = isSectionColor(color);
  if (icon === "none") {
    return hasColor ? sectionColorSwatch(color) : null;
  }
  const key = icon === "section_group" ? "default" : icon;
  const symbol = uiIcon(document.getElementById(`icon-sec-${key}`) ? `sec-${key}` : "sec-default");
  if (hasColor) symbol.setAttribute("color", color);
  return symbol;
}
