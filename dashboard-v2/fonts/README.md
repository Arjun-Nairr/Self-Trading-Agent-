# Self-hosted fonts

Two families, subset to latin (plus latin-ext for Archivo), served from
this directory by `css/app.css`'s own `@font-face` rules. **No external
runtime font request is made** — nothing in the shipped app talks to
`fonts.googleapis.com` or `fonts.gstatic.com`.

| File | Family | Weights | Bytes |
|---|---|---|---|
| `archivo-latin.woff2` | Archivo (variable) | 400–700 | 34 KB |
| `archivo-latin-ext.woff2` | Archivo (variable) | 400–700 | 32 KB |
| `plexmono-400-latin.woff2` | IBM Plex Mono | 400 | 15 KB |
| `plexmono-600-latin.woff2` | IBM Plex Mono | 600 | 15 KB |

98 KB total, and only the subsets a given page actually needs are fetched.
`font-display: swap` means text is readable from the first frame, and every
rule declares a real fallback stack (`--font` / `--mono` in `css/app.css`).
Deliberately **not** preloaded: with `unicode-range` subsetting, a preload
hint fires a "preloaded but not used" console warning while buying almost
nothing for a 34 KB face.

Archivo carries the UI, headings and body. IBM Plex Mono carries every
displayed numeral, so figures align in columns and read as measurements
rather than prose.

## Licensing

Both families are licensed under the **SIL Open Font License 1.1**, which
permits redistribution and web embedding.

- Archivo — Omnibus-Type. <https://github.com/Omnibus-Type/Archivo>
- IBM Plex Mono — IBM. <https://github.com/IBM/plex>

Files were fetched from Google Fonts' own `fonts.gstatic.com` woff2 builds
(the unicode-range subsets in `app.css` match those builds exactly). To
refresh them, re-fetch the URLs from the Google Fonts CSS API for
`Archivo:wght@400..700` and `IBM+Plex+Mono:wght@400;600`, and keep the
`unicode-range` declarations in sync with whatever subsets you download.
