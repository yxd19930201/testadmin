from PIL import Image, ImageDraw

sizes = [256, 128, 64, 48, 32, 24, 16]
images = []

for size in sizes:
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 渐变背景：深蓝到青色
    for y in range(size):
        ratio = y / size
        r = int(15 + (14 - 15) * ratio)
        g = int(23 + (165 - 23) * ratio)
        b = int(42 + (233 - 42) * ratio)
        draw.line([(0, y), (size, y)], fill=(r, g, b, 255))

    # 圆角裁剪
    mask = Image.new('L', (size, size), 0)
    mdraw = ImageDraw.Draw(mask)
    radius = size // 5
    mdraw.rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    img.putalpha(mask)
    draw = ImageDraw.Draw(img)

    # 红色录制小点（左上角）
    dot_r = max(1, size // 16)
    draw.ellipse(
        [size * 0.12, size * 0.12, size * 0.12 + dot_r * 2, size * 0.12 + dot_r * 2],
        fill=(239, 68, 68, 255),
    )

    # 白色对勾
    margin = size * 0.25
    x1 = margin
    y1 = size * 0.52
    x2 = size * 0.44
    y2 = size * 0.72
    x3 = size * 0.78
    y3 = size * 0.32
    line_w = max(2, size // 12)
    draw.line([(x1, y1), (x2, y2)], fill=(255, 255, 255, 255), width=line_w)
    draw.line([(x2, y2), (x3, y3)], fill=(255, 255, 255, 255), width=line_w)

    draw.ellipse(
        [x1 - line_w / 2, y1 - line_w / 2, x1 + line_w / 2, y1 + line_w / 2],
        fill=(255, 255, 255, 255),
    )
    draw.ellipse(
        [x3 - line_w / 2, y3 - line_w / 2, x3 + line_w / 2, y3 + line_w / 2],
        fill=(255, 255, 255, 255),
    )

    images.append(img)

images[0].save(
    r'c:\Users\PC\Desktop\testadmin\assets\icon.ico',
    format='ICO',
    append_images=images[1:],
)
print('icon.ico generated with', len(images), 'sizes')
