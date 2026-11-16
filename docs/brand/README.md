# Herald brand

| file | use |
|---|---|
| `banner.jpg` | README header, 2560 × 640 |
| `logo.png` | avatar and social preview, 1024 × 1024 (`logo-256.png`, `favicon.png` smaller) |
| `plate.jpg` | background texture for the setup page |
| `archivo.woff2` | Archivo variable font, SIL Open Font License 1.1 |

- Accent: `#5b8cff`. Everything else is neutral: ink `#ecebe4` on `#0a0c12`.
- Type: Archivo 800. Wide (`wdth 125`) for the wordmark, condensed (`wdth 62`) for the monogram.
- Tagline: "A second you that never sleeps."

`python docs/brand/render/render.py` regenerates every image from the shader
in `render/plate.html`.
