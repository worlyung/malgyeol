"""말결 벡터 로고와 Windows 다중 해상도 아이콘. 실행: python -m scripts.make_icon

Pillow가 있는 제작용 Python에서 실행합니다. 앱 실행에는 Pillow가 필요하지 않습니다.
말풍선(대화) 안에 세 개의 음성 파형(기록할 말)을 배치합니다.
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
GREEN = '#24664b'
CREAM = '#f7faf4'
SYMBOL = '''<rect width="64" height="64" rx="17" fill="#24664b"/>
<path d="M21 16h22a7 7 0 0 1 7 7v16a7 7 0 0 1-7 7H29l-10 7v-8a7 7 0 0 1-5-6V23a7 7 0 0 1 7-7Z" fill="none" stroke="#f7faf4" stroke-width="3" stroke-linejoin="round"/>
<path d="M24 28v8m8-13v18m8-11v4" fill="none" stroke="#f7faf4" stroke-width="3.5" stroke-linecap="round"/>'''


def icon(size=1024):
    unit = size / 64
    image = Image.new('RGBA', (size,size))
    draw = ImageDraw.Draw(image)
    def rect(coords): return tuple(round(n*unit) for n in coords)
    draw.rounded_rectangle((0,0,size-1,size-1),radius=17*unit,fill=GREEN)
    draw.rounded_rectangle(rect((14,16,50,46)),radius=7*unit,outline=CREAM,width=round(3*unit))
    # ㅁ 모양 말풍선의 왼쪽 아래를 열어 꼬리로 이어 줍니다.
    draw.rectangle(rect((17.5,42,30,48)),fill=GREEN)
    points=[(19,43),(19,53),(29,46),(42,46)]
    draw.line([(round(x*unit),round(y*unit)) for x,y in points],fill=CREAM,width=round(3*unit),joint='curve')
    for x,a,b in ((24,28,36),(32,23,41),(40,30,34)):
        r=1.75*unit
        draw.rounded_rectangle((x*unit-r,a*unit-r,x*unit+r,b*unit+r),radius=r,fill=CREAM)
    return image


def main():
    assets, web = ROOT/'assets', ROOT/'web'
    svg='<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'+SYMBOL+'</svg>\n'
    (assets/'brand-icon.svg').write_text(svg,encoding='utf-8')
    (web/'brand-icon.svg').write_text(svg,encoding='utf-8')
    logo='''<svg xmlns="http://www.w3.org/2000/svg" width="600" height="160" viewBox="0 0 600 160">
<title>말결 — 대화를 기록으로 잇다</title>
<g transform="translate(8 16) scale(2)">'''+SYMBOL+'''</g>
<text x="168" y="88" fill="#203e30" font-family="Pretendard, Malgun Gothic, sans-serif" font-size="68" font-weight="700" letter-spacing="-3">말결</text>
<text x="171" y="124" fill="#627168" font-family="Pretendard, Malgun Gothic, sans-serif" font-size="20">대화를 기록으로 잇다</text></svg>\n'''
    (assets/'malgyeol-logo.svg').write_text(logo,encoding='utf-8')
    big=icon()
    for path,size in ((assets/'icon.png',512),(assets/'icon-44.png',44),(web/'favicon.png',64)):
        big.resize((size,size),Image.Resampling.LANCZOS).save(path)
    big.resize((256,256),Image.Resampling.LANCZOS).save(assets/'icon.ico',sizes=[(s,s) for s in (16,20,24,32,40,48,64,128,256)])
    # Windows 아이콘 캐시가 이전 파일 경로를 재사용하지 않도록 새 이름도 제공합니다.
    (assets/'malgyeol.ico').write_bytes((assets/'icon.ico').read_bytes())
    fontroot=Path('C:/Windows/Fonts')
    bold=ImageFont.truetype(str(fontroot/'malgunbd.ttf'),136)
    regular=ImageFont.truetype(str(fontroot/'malgun.ttf'),40)
    lockup=Image.new('RGBA',(1200,320))
    lockup.alpha_composite(big.resize((256,256),Image.Resampling.LANCZOS),(16,32))
    draw=ImageDraw.Draw(lockup)
    draw.text((336,40),'말결',font=bold,fill='#203e30',stroke_width=0)
    draw.text((342,216),'대화를 기록으로 잇다',font=regular,fill='#627168')
    lockup.save(assets/'malgyeol-logo.png')
    print('말결: SVG 로고, 투명 PNG, 9종 해상도 ICO, favicon 생성 완료')


if __name__ == '__main__':
    main()
