"""Re-render Herald's logo, banner and plate.

The look is an emulsion plate: a procedural field, a three-stop tone ramp
and film grain, rendered by WebGL in headless Chromium. Archivo 800 sets
the type. Run from anywhere: `python docs/brand/render/render.py`.
Needs playwright with a Chromium (`playwright install chromium`).
"""
import asyncio
from pathlib import Path

from PIL import Image
from playwright.async_api import async_playwright

HERE = Path(__file__).resolve().parent
OUT = HERE.parent
INK = "#ecebe4"
BLUE_RAMP = {"c0": "#0a0c12", "c1": "#1c2f5c", "c2": "#9db9f5"}
TAGLINE = "A second you that never sleeps."

BANNER_PLATE = {"shape": 2, "angle": -86, "scale": 1.25, "warp": .5, "bands": 1.6,
                "blur": .25, "mottle": .25, "exposure": -.12, "black": .04,
                "gamma": 1.1, "vignette": .3, "grain": .2, "gresp": .4, "seed": .61,
                **BLUE_RAMP}
ORB_PLATE = {"shape": 5, "scale": .85, "soft": .55, "warp": .5, "exposure": -.1,
             "vignette": .3, "grain": .2, "gamma": 1.6,
             "c0": "#08090d", "c1": "#1a2f63", "c2": "#a9c2ff"}

WORDMARK = (f"<div style=\"position:absolute;left:30px;bottom:26px;font-size:74px;"
            f"letter-spacing:-.03em;font-variation-settings:'wdth' 125;color:{INK}\">Herald</div>"
            f"<div style=\"position:absolute;right:28px;bottom:31px;font-size:12.5px;"
            f"font-weight:500;letter-spacing:.01em;font-variation-settings:'wdth' 100;"
            f"color:#d4d6de\">{TAGLINE}</div>")
MONOGRAM = (f"<div style=\"margin:auto;font-size:360px;font-variation-settings:'wdth' 62;"
            f"color:{INK};transform:translateY(14px)\">H</div>")

JOBS = [
    # name, css width, css height, html, plate, light
    ("banner", 640, 160, WORDMARK, BANNER_PLATE, ""),
    ("plate", 640, 160, "", BANNER_PLATE, ""),
    ("logo", 512, 512, MONOGRAM, ORB_PLATE, "?light=front"),
]


async def main() -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(args=[
            "--use-gl=angle", "--use-angle=swiftshader", "--enable-unsafe-swiftshader",
            "--allow-file-access-from-files"])
        for name, w, h, html, plate, light in JOBS:
            page = await browser.new_page(viewport={"width": w, "height": h},
                                          device_scale_factor=4)
            await page.goto((HERE / "plate.html").as_uri() + light)
            await page.evaluate("cfg => render(cfg)",
                                {"w": w, "h": h, "html": html, "plate": plate})
            await page.wait_for_timeout(800)
            raw = OUT / f".{name}.png"
            await page.locator("#w").screenshot(path=str(raw))
            await page.close()
            img = Image.open(raw).convert("RGB")
            raw.unlink()
            if name == "banner":
                img.save(OUT / "banner.jpg", quality=90, progressive=True)
            elif name == "plate":
                img.resize((1600, 400), Image.LANCZOS).save(OUT / "plate.jpg", quality=82,
                                                           progressive=True)
            else:
                for size, fname in ((1024, "logo.png"), (256, "logo-256.png"),
                                    (64, "favicon.png")):
                    img.resize((size, size), Image.LANCZOS).save(OUT / fname, optimize=True)
        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
