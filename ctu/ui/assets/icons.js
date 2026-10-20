// Inline SVG icons (16 px grid, stroke 1.3, currentColor). Level icons use a
// filled shape in the state colour with the glyph in --on-status, so they work
// on light, dark and solid surfaces. All icons are decorative (aria-hidden):
// the meaning is always also in a word next to them.

const S = 'fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"';

const P = {
  now: `<circle cx="8" cy="8" r="6.2" ${S}/><path d="M8 4.5V8l2.5 1.6" ${S}/>`,
  sessions: `<rect x="1.5" y="2.5" width="13" height="11" rx="2" ${S}/><path d="M4.5 6.5l2 1.5-2 1.5M8 10h3.5" ${S}/>`,
  team: `<circle cx="5.5" cy="6" r="2.3" ${S}/><circle cx="11" cy="6.5" r="1.9" ${S}/><path d="M1.5 13c.6-2.3 2.1-3.4 4-3.4s3.4 1.1 4 3.4M10 9.6c1.9-.2 3.4.8 4.3 3" ${S}/>`,
  history: `<path d="M2 13.5h12M3.5 11V7M6.5 11V4M9.5 11V8M12.5 11V5.5" ${S}/>`,
  settings: `<circle cx="8" cy="8" r="2.2" ${S}/><path d="M8 1.8v1.7M8 12.5v1.7M1.8 8h1.7M12.5 8h1.7M3.6 3.6l1.2 1.2M11.2 11.2l1.2 1.2M3.6 12.4l1.2-1.2M11.2 4.8l1.2-1.2" ${S}/>`,
  refresh: `<path d="M13 8a5 5 0 1 1-1.5-3.6M13 2.6v2.6h-2.6" ${S}/>`,
  pin: `<path d="M9.6 1.8l4.6 4.6-2.2 1.1-2.6 2.6.4 2.8-1.1 1.1L2 7.3l1.1-1.1 2.8.4 2.6-2.6z" ${S}/><path d="M4.7 11.3L1.8 14.2" ${S}/>`,
  pinned: `<path d="M9.6 1.8l4.6 4.6-2.2 1.1-2.6 2.6.4 2.8-1.1 1.1L2 7.3l1.1-1.1 2.8.4 2.6-2.6z" fill="currentColor" stroke="currentColor" stroke-width="1.3" stroke-linejoin="round"/><path d="M4.7 11.3L1.8 14.2" ${S}/>`,
  "chevron-up": `<path d="M3.5 10l4.5-4.5 4.5 4.5" ${S}/>`,
  "chevron-down": `<path d="M3.5 6l4.5 4.5L12.5 6" ${S}/>`,
  "chevron-left": `<path d="M10 3.5L5.5 8l4.5 4.5" ${S}/>`,
  "chevron-right": `<path d="M6 3.5L10.5 8 6 12.5" ${S}/>`,
  dashboard: `<rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1.6" ${S}/><path d="M1.8 5.8h12.4" ${S}/>`,
  overlay: `<rect x="1.8" y="5.8" width="12.4" height="4.4" rx="1.6" ${S}/><path d="M4.5 8h2.5M9 8h2.5" ${S}/>`,
  "bell-off": `<path d="M4 6.5a4 4 0 0 1 6.9-2.8M12 7v2.5l1.3 2H5.5M2.7 11.5L4 9.5V8M6.5 13.5h3" ${S}/><path d="M2 2l12 12" ${S}/>`,
  bell: `<path d="M4 6.5a4 4 0 0 1 8 0v3l1.3 2H2.7L4 9.5z M6.5 13.5h3" ${S}/>`,
  copy: `<rect x="5.5" y="5.5" width="8" height="8.5" rx="1.5" ${S}/><path d="M10.5 3.5V3a1.5 1.5 0 0 0-1.5-1.5H4A1.5 1.5 0 0 0 2.5 3v6.5A1.5 1.5 0 0 0 4 11h.5" ${S}/>`,
  download: `<path d="M8 2v8M4.8 7l3.2 3.2L11.2 7M2.5 13.5h11" ${S}/>`,
  "sign-out": `<path d="M6.5 2.5H4A1.5 1.5 0 0 0 2.5 4v8A1.5 1.5 0 0 0 4 13.5h2.5M10 5l3 3-3 3M13 8H6" ${S}/>`,
  "sign-in": `<path d="M9.5 2.5H12A1.5 1.5 0 0 1 13.5 4v8a1.5 1.5 0 0 1-1.5 1.5H9.5M6 5l3 3-3 3M9 8H2" ${S}/>`,
  lock: `<rect x="3" y="7" width="10" height="7" rx="1.5" ${S}/><path d="M5.2 7V5a2.8 2.8 0 0 1 5.6 0v2" ${S}/>`,
  external: `<path d="M9 2.5h4.5V7M13.5 2.5L7.5 8.5M11.5 9.5v3A1.5 1.5 0 0 1 10 14H4a1.5 1.5 0 0 1-1.5-1.5v-6A1.5 1.5 0 0 1 4 5h3" ${S}/>`,
  close: `<path d="M3.5 3.5l9 9M12.5 3.5l-9 9" ${S}/>`,
  check: `<path d="M3 8.5l3.2 3L13 4.5" ${S}/>`,
  link: `<path d="M6.8 9.2a2.6 2.6 0 0 0 3.7 0l2.4-2.4a2.6 2.6 0 0 0-3.7-3.7l-.8.8M9.2 6.8a2.6 2.6 0 0 0-3.7 0L3.1 9.2a2.6 2.6 0 0 0 3.7 3.7l.8-.8" ${S}/>`,
  phone: `<rect x="4" y="1.5" width="8" height="13" rx="1.8" ${S}/><path d="M7 12h2" ${S}/>`,
  "arrow-up": `<path d="M8 13V3M4 7l4-4 4 4" ${S}/>`,
  "arrow-down": `<path d="M8 3v10M4 9l4 4 4-4" ${S}/>`,
  mail: `<rect x="1.8" y="3.3" width="12.4" height="9.4" rx="1.5" ${S}/><path d="M2.3 4.3L8 8.7l5.7-4.4" ${S}/>`,
  key: `<circle cx="5" cy="10.5" r="2.7" ${S}/><path d="M7 8.5l6-6M11 4.5l1.6 1.6M9.4 6.1l1.2 1.2" ${S}/>`,
  info: `<circle cx="8" cy="8" r="6.2" ${S}/><path d="M8 7.2v4" ${S}/><circle cx="8" cy="4.9" r=".9" fill="currentColor"/>`,
  plug: `<path d="M5.5 1.8v3M10.5 1.8v3M3.5 4.8h9v2.7a4.5 4.5 0 0 1-9 0zM8 12v2.2" ${S}/>`,
  user: `<circle cx="8" cy="5.5" r="2.8" ${S}/><path d="M2.8 14c.7-2.6 2.7-4 5.2-4s4.5 1.4 5.2 4" ${S}/>`,
  sparkle: `<path d="M8 2v3M8 11v3M2 8h3M11 8h3M4 4l1.6 1.6M10.4 10.4L12 12M4 12l1.6-1.6M10.4 5.6L12 4" ${S}/>`,
  folder: `<path d="M1.8 4.5A1.5 1.5 0 0 1 3.3 3h3l1.5 1.7h4.9a1.5 1.5 0 0 1 1.5 1.5v5.3a1.5 1.5 0 0 1-1.5 1.5H3.3a1.5 1.5 0 0 1-1.5-1.5z" ${S}/>`,
  grip: `<circle cx="6" cy="4" r="1" fill="currentColor"/><circle cx="10" cy="4" r="1" fill="currentColor"/><circle cx="6" cy="8" r="1" fill="currentColor"/><circle cx="10" cy="8" r="1" fill="currentColor"/><circle cx="6" cy="12" r="1" fill="currentColor"/><circle cx="10" cy="12" r="1" fill="currentColor"/>`,
  logo: `<rect x="1.5" y="9" width="13" height="4" rx="2" ${S}/><rect x="1.5" y="9" width="8" height="4" rx="2" fill="currentColor"/><path d="M3 6.5h10" ${S}/>`,
};

/** A UI icon as an SVG string. */
export function icon(name, { size = 16, cls = "" } = {}) {
  const body = P[name] || P.info;
  return `<svg class="ic ${cls}" width="${size}" height="${size}" viewBox="0 0 16 16" aria-hidden="true" focusable="false">${body}</svg>`;
}

const L = {
  ok: `<circle cx="8" cy="8" r="7" fill="currentColor"/><path class="gs" d="M4.8 8.2l2.1 2 4.3-4.3" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`,
  watch: `<circle cx="8" cy="8" r="7" fill="currentColor"/><path class="gs" d="M8 4.3V8l2.4 1.5" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`,
  will_hit: `<path d="M8 1.6c.4 0 .8.2 1 .6l6 10.6c.4.8-.1 1.7-1 1.7H2c-.9 0-1.4-.9-1-1.7L7 2.2c.2-.4.6-.6 1-.6z" fill="currentColor"/><path class="gs" d="M8 6v3.6" stroke-width="1.7" stroke-linecap="round"/><circle class="g" cx="8" cy="11.9" r="1" stroke="none"/>`,
  limited: `<circle cx="8" cy="8" r="7" fill="currentColor"/><path class="gs" d="M8 4.3V8l2.4 1.5" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`,
  stale: `<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M3.8 12.2l8.4-8.4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/>`,
  gathering: `<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.6"/><circle cx="5.3" cy="8" r="1" fill="currentColor"/><circle cx="8" cy="8" r="1" fill="currentColor"/><circle cx="10.7" cy="8" r="1" fill="currentColor"/>`,
  "svc-ok": `<circle cx="8" cy="8" r="7" fill="currentColor"/><path class="gs" d="M4.8 8.2l2.1 2 4.3-4.3" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`,
  "svc-minor": `<circle cx="8" cy="8" r="7" fill="currentColor"/><path class="gs" d="M8 4.4v4.2" stroke-width="1.7" stroke-linecap="round"/><circle class="g" cx="8" cy="11.3" r="1" stroke="none"/>`,
  "svc-major": `<path d="M8 1.6c.4 0 .8.2 1 .6l6 10.6c.4.8-.1 1.7-1 1.7H2c-.9 0-1.4-.9-1-1.7L7 2.2c.2-.4.6-.6 1-.6z" fill="currentColor"/><path class="gs" d="M8 6v3.6" stroke-width="1.7" stroke-linecap="round"/><circle class="g" cx="8" cy="11.9" r="1" stroke="none"/>`,
  "svc-unknown": `<circle cx="8" cy="8" r="6" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M6.3 6.4a1.8 1.8 0 1 1 2.4 1.7c-.5.2-.7.6-.7 1.1v.3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/><circle cx="8" cy="11.4" r=".9" fill="currentColor"/>`,
  error: `<circle cx="8" cy="8" r="7" fill="currentColor"/><path class="gs" d="M5.6 5.6l4.8 4.8M10.4 5.6l-4.8 4.8" stroke-width="1.7" stroke-linecap="round"/>`,
  success: `<circle cx="8" cy="8" r="7" fill="currentColor"/><path class="gs" d="M4.8 8.2l2.1 2 4.3-4.3" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>`,
};

/** A filled status icon: level is ok | watch | will_hit | limited | stale | gathering | svc-*. */
export function levelIcon(level, size = 16, cls = "") {
  const body = L[level] || L.ok;
  const tone = level === "error" ? "will_hit" : level === "success" ? "svc-ok" : level;
  return `<svg class="lvl-icon is-${tone} ${cls}" width="${size}" height="${size}" viewBox="0 0 16 16" aria-hidden="true" focusable="false">${body}</svg>`;
}
