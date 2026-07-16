"""Render the app icon (same mouse+curve motif as the tray icon) to icon.ico."""
import os

from PIL import Image, ImageDraw


def draw(size):
    s = size / 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([8 * s, 4 * s, 56 * s, 60 * s], radius=22 * s,
                        fill=(24, 27, 34, 255), outline=(90, 226, 187, 255),
                        width=max(1, int(3 * s)))
    d.line([12 * s, 52 * s, 24 * s, 48 * s, 34 * s, 38 * s, 44 * s, 22 * s,
            52 * s, 12 * s], fill=(90, 226, 187, 255),
           width=max(1, int(4 * s)), joint="curve")
    return img


if __name__ == "__main__":
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
    sizes = [16, 24, 32, 48, 64, 128, 256]
    imgs = [draw(sz) for sz in sizes]
    imgs[-1].save(out, sizes=[(sz, sz) for sz in sizes],
                  append_images=imgs[:-1])
    print("wrote", out)
