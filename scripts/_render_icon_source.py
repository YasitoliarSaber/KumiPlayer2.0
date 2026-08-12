# -*- coding: utf-8 -*-
"""4x 超采样 + 圆点并集描边（无折线接缝）渲染图标全套。"""
import math
from PIL import Image, ImageDraw, ImageFilter

S = 4
W = H = 1024 * S
img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
def sc(v): return v * S

d.rounded_rectangle([0, 0, sc(1024), sc(1024)], radius=sc(224), fill=(0x1A, 0x1A, 0x1A, 0xFF))
sheen = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(sheen).ellipse([sc(90), sc(40), sc(480), sc(300)], fill=(255, 255, 255, 26))
sheen = sheen.filter(ImageFilter.GaussianBlur(60 * S))
img = Image.alpha_composite(img, sheen)
depth = Image.new("RGBA", (W, H), (0, 0, 0, 0))
ImageDraw.Draw(depth).ellipse([sc(100), sc(700), sc(924), sc(1100)], fill=(0, 0, 0, 77))
depth = depth.filter(ImageFilter.GaussianBlur(80 * S))
img = Image.alpha_composite(img, depth)
d = ImageDraw.Draw(img)
d.rounded_rectangle([sc(72), sc(72), sc(952), sc(952)], radius=sc(152), outline=(255,255,255,255), width=8*S)
for box, a0, a1 in [([24,24,424,424],180,270),([600,24,1000,424],270,360),([24,600,424,1000],90,180),([600,600,1000,1000],0,90)]:
    d.arc([sc(box[0]), sc(box[1]), sc(box[2]), sc(box[3])], a0, a1, fill=(255,255,255,230), width=6*S)

def qpts(p0, c, p1, n=200):
    pts=[]
    for i in range(n+1):
        t=i/n
        x=(1-t)**2*p0[0]+2*(1-t)*t*c[0]+t*t*p1[0]
        y=(1-t)**2*p0[1]+2*(1-t)*t*c[1]+t*t*p1[1]
        pts.append((x,y))
    return pts

WHITE=(255,255,255,255)
R = 72/2  # 笔画半径(1024空间)
def brush(pts):
    # 沿曲线密集盖不透明圆，形成无接缝平滑笔画
    for (x,y) in pts:
        r=sc(R)
        d.ellipse([sc(x)-r, sc(y)-r, sc(x)+r, sc(y)+r], fill=WHITE)

stem = qpts((400,300),(374,512),(400,724))
chev = qpts((640,316),(556,402),(486,512)) + qpts((486,512),(566,612),(652,724))[1:]
brush(stem)
brush(chev)

master = img.resize((1024,1024), Image.LANCZOS)
base = r"D:\01_Software\KumiPlayer2.0\src-tauri\icons"
def save(size,name):
    master.resize((size,size), Image.LANCZOS).save(base+"\\"+name,"PNG")
save(32,"32x32.png"); save(128,"128x128.png"); save(256,"128x128@2x.png")
ico = master.resize((256,256), Image.LANCZOS)
ico.save(base+"\\icon.ico", format="ICO", sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(256,256)])
try:
    master.save(base+"\\icon.icns", format="ICNS"); print("icns ok")
except Exception as e:
    print("icns skipped:", e)
print("done")
