import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

const css = readFileSync(
  fileURLToPath(new URL("../src/styles/tokens.css", import.meta.url)),
  "utf8",
);
const tokens = Object.fromEntries(
  [...css.matchAll(/--([\w-]+):\s*(#[\da-fA-F]{6})\s*;/g)].map(
    ([, key, value]) => [key, value],
  ),
);

// Resolve the simple anchor selectors affecting the skip link in stylesheet
// order. Checking the winning declarations catches a generic a:hover override,
// rather than trusting an intended colour pair when the cascade renders another.
const styles = ["base", "shell", "ledger", "detail", "forms", "articles"]
  .map((name) =>
    readFileSync(new URL(`../src/styles/${name}.css`, import.meta.url), "utf8"),
  )
  .join("\n")
  .replace(/\/\*[\s\S]*?\*\//g, "");

function skipLinkPair(states) {
  const values = { color: "ink", background: "page" };
  const specificity = { color: -1, background: -1 };
  for (const [, selectors, body] of styles.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    for (const selector of selectors.split(",").map((value) => value.trim())) {
      if (!/^(?:a|\.skip-link|a\.skip-link)(?::[\w-]+)*$/.test(selector)) {
        continue;
      }
      const pseudos = [...selector.matchAll(/:([\w-]+)/g)].map((match) => match[1]);
      if (pseudos.some((state) => !states.includes(state))) continue;
      const weight =
        (selector.includes(".skip-link") ? 10 : 0) +
        pseudos.length * 10 + (selector.startsWith("a") ? 1 : 0);
      const declarations = body.matchAll(
        /(?:^|;)\s*(color|background):\s*var\(--([\w-]+)\)\s*(?=;|$)/g,
      );
      for (const [, property, token] of declarations) {
        if (weight >= specificity[property]) {
          specificity[property] = weight;
          values[property] = token;
        }
      }
    }
  }
  return [values.color, values.background, `Skip link: ${states.join("+") || "default"}`];
}

// Audited against base, shell, ledger, detail, forms and articles CSS. Focus outlines do
// not change text colours. Transparent controls use their containing background.
// Each entry names the actual states sharing a pair, even when another control
// uses the same colours. Chips/badges are spans with no interactive state styles.
const pairs = [
  ["ink", "surface", "Body, table cells, JSON, article text"],
  ["ink", "page", "Body, expanded step records"],
  ["secondary", "surface", "Help, labels, mode pill, table metadata, queue indicator"],
  ["secondary", "page", "Help, headings, inspector bars, pagination"],
  ["accent", "surface", "Links: default, focus, active"],
  ["accent", "page", "Links: default, focus, active"],
  ["accent-hover", "surface", "Links and queue indicator: hover, hover+focus, hover+active"],
  ["accent-hover", "page", "Links and queue indicator: hover, hover+focus, hover+active"],
  ["ink", "surface", "Wordmark: default, focus, active"],
  ["accent-hover", "surface", "Wordmark: hover, hover+focus, hover+active"],
  ["ink", "surface", "Nav: default, focus, active and current page"],
  ["secondary", "surface", "Quiet Settings nav: default, focus, active"],
  ["accent", "surface", "Nav: hover, including quiet/current/focused/active"],
  ...[
    [],
    ["hover"],
    ["focus"],
    ["focus", "focus-visible"],
    ["hover", "focus", "focus-visible"],
    ["active"],
    ["hover", "focus", "active"],
  ].map(skipLinkPair),
  ["surface", "accent", "Primary button: default, focus"],
  ["surface", "accent-hover", "Primary button: hover, active, hover+focus"],
  ["secondary", "page", "Primary button: disabled, disabled+hover"],
  ["ink", "surface", "Secondary/icon buttons: default, focus, active"],
  ["ink", "page", "Secondary/icon buttons: hover, hover+focus, hover+active"],
  ["secondary", "page", "Secondary button: disabled, disabled+hover"],
  ["accent", "surface", "Text buttons: default, focus, active"],
  ["accent", "page", "Copy JSON: default, focus, active"],
  ["accent-hover", "surface", "Text buttons: hover, hover+focus, hover+active"],
  ["accent-hover", "page", "Copy JSON: hover, hover+focus, hover+active"],
  ["accent", "failure-bg", "Error links/retry: default, focus, active"],
  ["accent-hover", "failure-bg", "Error links/retry: hover, hover+focus"],
  ["ink", "surface", "Reaction buttons: default, hover, focus, active"],
  ["surface", "ink", "Selected reaction: default, hover, focus, active"],
  ["ink", "surface", "Ledger article link: default"],
  ["ink", "page", "Ledger row hover/focus/active: title and cell text"],
  ["secondary", "page", "Ledger row hover/focus/active: metadata, pending"],
  ["accent", "page", "Ledger article link: hover, hover+focus, hover+active"],
  ["ink", "surface", "Step toggle: default, hover, focus, active, expanded"],
  ["secondary", "surface", "Step toggle: number, disabled, disabled+hover"],
  ["ink", "surface", "Inputs/selects: text and selected option, all states"],
  ["secondary", "surface", "Inputs/textarea placeholders: all states"],
  ["ink", "surface", "Disclosures/scroll regions: hover, focus, active, open"],
  ["ink", "page", "Step disclosures: hover, focus, active, open"],
  ["ink", "surface", "Neutral badges/status chips, including row hover/focus"],
  ...[
    "review",
    "failure",
    "not-removed",
    "moved",
    "likely-moved",
    "found",
    "inconclusive",
    "low-interest",
  ].map((tone) => [
    `${tone}-text`,
    `${tone}-bg`,
    `${tone} chip/badge (unchanged on row hover/focus) or error text`,
  ]),
  ["found-text", "page", "Review saved message"],
  ["found-text", "surface", "Review saved message on surface"],
];

export function luminance(hex) {
  const rgb = hex
    .slice(1)
    .match(/../g)
    .map((v) => parseInt(v, 16) / 255)
    .map((v) =>
      v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4,
    );
  return rgb[0] * 0.2126 + rgb[1] * 0.7152 + rgb[2] * 0.0722;
}

let failed = false;
for (const [foreground, background, states] of pairs) {
  if (!tokens[foreground] || !tokens[background]) {
    throw new Error(`Missing colour token: ${foreground} / ${background}`);
  }
  const values = [
    luminance(tokens[foreground]),
    luminance(tokens[background]),
  ].sort((a, b) => b - a);
  const ratio = (values[0] + 0.05) / (values[1] + 0.05);
  const pass = ratio >= 4.5;
  console.log(
    `${pass ? "PASS" : "FAIL"} ${foreground} / ${background}: ${ratio.toFixed(2)}:1`,
    `(${states})`,
  );
  if (!pass) failed = true;
}
if (failed) process.exitCode = 1;
else console.log(`All ${pairs.length} declared text/state pairs meet 4.5:1.`);
