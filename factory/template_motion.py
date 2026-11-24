"""Versioned opening graphics for the original four moving-reel recipes."""

import json
import math
import re
import sys

from . import media, portal_templates

VERSION = "original-four-clear-before-dialogue-v1"
DIALOGUE_VERSION = "original-four-phrase-boundary-v2"
PACK = portal_templates.PACK
ROLE = {"hook-card":("headline","montserrat"),"commentary-stripe":("phrase","anton"),
        "gold-camera":("optional_dialogue","barlowsemicondensed"),"discovery-tabs":("headline","archivo")}


def pin(assignment):
    identity = assignment["template"]
    if identity not in ROLE or assignment["revision"]!=1:
        raise media.MediaError("Moving style requires one of the four shipped revisions.")
    entry = next(x for x in portal_templates.CATALOG["templates"] if x["id"]==identity)
    recipe = PACK/entry["recipe"]
    font_id = ROLE[identity][1]
    font = portal_templates.asset("font",font_id)
    fonts = json.loads((PACK/"fonts"/"manifest.v1.json").read_text(encoding="utf-8"))["fonts"]
    expected = next(x["sha256"] for x in fonts if x["id"]==font_id)
    if not font.is_file() or media.checksum(font)!=expected:
        raise media.MediaError(f"Moving style font failed verification: {font_id}.")
    treatment = assignment.get("treatment",VERSION)
    if treatment not in (VERSION,DIALOGUE_VERSION):
        raise media.MediaError("Unknown moving template treatment.")
    return dict(assignment,treatment=treatment,recipe_sha256=media.checksum(recipe),font_id=font_id,font_sha256=expected)


def opening_time(plan, recipe):
    if plan.get("owner_viewing"):
        return 2.8
    lower,upper = recipe.get("timing",{}).get("hold_range_ms",[1350,2800])
    proof = plan["proof"]["edit"]
    fps = plan["fps"]
    preferred = min(upper/1000,max(lower/1000,len(plan["hook"])/17,2.8 if plan.get("origin")=="audio-first" else 1.7))
    out = math.floor(min(preferred,proof-.20)*fps)/fps
    if out < max(lower/1000,len(plan["hook"])/20):
        raise media.MediaError("The headline cannot clear before the visual proof at a readable speed.")
    return round(out,6)


def library():
    location = str(media.TOOLS/"gta-streamer-template-raster")
    if location not in sys.path:
        sys.path.insert(0,location)
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont, features
        from fontTools.ttLib import TTFont
    except ImportError:
        raise media.MediaError("Install the local Pillow and fonttools template renderer.") from None
    if not features.check("raqm"):
        raise media.MediaError("Pillow needs RAQM for reproducible template typography.")
    return Image, ImageDraw, ImageFilter, ImageFont, TTFont


def lines(draw, font, text, width, *, force_two=False):
    if not force_two and draw.textlength(text,font=font)<=width:
        return [text]
    words = text.split()
    choices = [(" ".join(words[:n])," ".join(words[n:])) for n in range(1,len(words))]
    fits = [(max(draw.textlength(a,font=font),draw.textlength(b,font=font)),a,b) for a,b in choices]
    size,a,b = min(fits,default=(math.inf,text,""))
    if size>width:
        raise media.MediaError("The hook does not fit the selected template. Shorten it before export.")
    return [a,b]


def center_hook(draw, text, font_at):
    width = 920
    for size in range(108,53,-2):
        font = font_at(size)
        try:
            wrapped = lines(draw,font,text,width)
        except media.MediaError:
            wrapped = []
            for word in text.split():
                combined = f"{wrapped[-1]} {word}" if wrapped else word
                if wrapped and draw.textlength(combined,font=font)<=width:
                    wrapped[-1] = combined
                else:
                    wrapped.append(word)
        if len(wrapped)<=3 and all(draw.textlength(line,font=font)<=width for line in wrapped):
            break
    else:
        raise media.MediaError("The opening hook is too wide. Use a shorter sentence.")
    advance = round(size*1.14)
    half_height = len(wrapped)*advance/2+24
    half_width = max(draw.textlength(line,font=font) for line in wrapped)/2+32
    draw.rounded_rectangle((540-half_width,960-half_height,540+half_width,960+half_height),
                           radius=28,fill=(14,14,14,226))
    for index,line in enumerate(wrapped):
        y = 960+(index-(len(wrapped)-1)/2)*advance
        draw.text((540,y),line,font=font,anchor="mm",fill="white",stroke_width=4,stroke_fill="#101010")


def save_layers(target,plan):
    style = plan["render_style"]
    if pin(style)!=style:
        raise media.MediaError("A pinned recipe or font changed since this edition was planned.")
    recipe_entry = next(x for x in portal_templates.CATALOG["templates"] if x["id"]==style["template"])
    recipe = json.loads((PACK/recipe_entry["recipe"]).read_text(encoding="utf-8"))
    Image,Draw,Filter,Font,TTFont = library()
    identity = style["template"]
    text_spec = recipe[ROLE[identity][0]]
    font_path = portal_templates.asset("font",style["font_id"])

    def font_at(size):
        font = Font.truetype(str(font_path),size,layout_engine=Font.Layout.RAQM)
        if axes := text_spec.get("axes"):
            with TTFont(font_path) as variable:
                font.set_variation_by_axes([axes.get(axis.axisTag,axis.defaultValue) for axis in variable["fvar"].axes])
        return font

    def layout(draw,text,width,*,two=False):
        maximum = text_spec["size_px"]
        minimum = text_spec.get("min_size_px",maximum)
        for size in range(maximum,minimum-1,-2):
            font = font_at(size)
            try:
                return font,lines(draw,font,text,width,force_two=two)
            except media.MediaError:
                continue
        raise media.MediaError("The hook does not fit the selected template. Shorten it before export.")

    opening = Image.new("RGBA",(1080,1920))
    art = Draw.Draw(opening)
    decoration = Image.new("RGBA",opening.size)
    ornaments = Draw.Draw(decoration)
    hook = plan["hook"]
    large = plan.get("story_v4",False)
    expiry = opening_time(plan,recipe)
    fade = recipe.get("timing",{}).get("exit_ms",80)/1000
    if identity=="hook-card" and not large:
        card = recipe["card"]
        font,contents = layout(art,hook,card["width_px"]-2*card["padding_xy_px"][0])
        x,y = card["anchor_cx_cy_px"]
        half = card["width_px"]/2
        advance = text_spec["line_advance_px"]
        depth = len(contents)*advance+2*card["padding_xy_px"][1]-16
        art.rounded_rectangle((x-half,y-depth/2,x+half,y+depth/2),radius=card["radius_px"],fill=card["fill"])
        colors = text_spec["role_colors"]
        words = re.findall(r"[\w’]+",hook)
        action = next((word for word in words if word.isupper() and len(word)>1),None)
        if not action:
            action = next((word for word in words if word.casefold() in {"cops","storm","truck","tyre","police"}),None)
        if not action:
            action = max(words,key=len,default="")
        creator_words = set(re.findall(r"[\w’]+",plan.get("credit","").casefold()))
        for i,line in enumerate(contents):
            left = x-art.textlength(line,font=font)/2
            line_y = y+(i-(len(contents)-1)/2)*advance
            art.text((left,line_y),line,font=font,anchor="lm",fill=colors["body"])
            for match in re.finditer(r"[\w’]+",line):
                word = match[0]
                if word==action or word.casefold() in creator_words:
                    prior = line[:match.start()]
                    offset = art.textlength(prior,font=font)
                    art.text((left+offset,line_y),word,font=font,anchor="lm",
                             fill=colors["creator"] if word.casefold() in creator_words else colors["action"])
    elif identity=="commentary-stripe":
        for rule in recipe["seam_rules"]:
            x,y,w,h = rule["rect_xywh"]
            ornaments.rectangle((x,y,x+w-1,y+h-1),fill=rule["fill"])
        mark = recipe["optional_mark"]
        if mark["enabled"]:
            x,y = mark["center_xy_px"]
            for dx in (-9,7):
                ornaments.line([(x+dx-7,y-8),(x+dx+2,y),(x+dx-7,y+8)],fill=mark["outline"],width=7)
                ornaments.line([(x+dx-7,y-8),(x+dx+2,y),(x+dx-7,y+8)],fill=mark["fill"],width=3)
        if not large:
            sx = text_spec["glyph_scale_x"]
            plate = Image.new("RGBA",(int(1080/sx),280))
            pen = Draw.Draw(plate)
            font,parts = layout(pen,hook,text_spec["max_width_px"]/sx-22)
            for i,line in enumerate(parts):
                pen.text((plate.width/2,140+(i-(len(parts)-1)/2)*text_spec["line_advance_px"]),line,
                         font=font,anchor="mm",fill=text_spec["fill"],stroke_width=text_spec["outline_px"],
                         stroke_fill=text_spec["outline"])
            opening.alpha_composite(plate.resize((1080,280),Image.Resampling.LANCZOS),(0,text_spec["center_xy_px"][1]-140))
    elif identity=="gold-camera":
        cam,rim = recipe["camera_inset"],recipe["rim"]
        x,y,w,h = cam["rect_xywh"]
        radius = cam["radius_px"]
        mask = Image.new("L",(w,h))
        Draw.Draw(mask).rounded_rectangle((0,0,w-1,h-1),radius,fill=255)
        if target is not None:
            mask.save(target/"camera-mask.png")
        halo = Image.new("RGBA",opening.size)
        Draw.Draw(halo).rounded_rectangle((x,y,x+w-1,y+h-1),radius,outline=rim["glow"],width=18)
        decoration.alpha_composite(halo.filter(Filter.GaussianBlur(rim["glow_blur_px"])))
        ornaments.rounded_rectangle((x,y,x+w-1,y+h-1),radius,outline=rim["edge"],width=rim["core_width_px"]+rim["edge_width_px"])
        ornaments.rounded_rectangle((x+2,y+2,x+w-3,y+h-3),radius-2,outline=rim["core"],width=rim["core_width_px"])
        if not large:
            font = font_at(text_spec["size_px"])
            if art.textlength(hook,font=font)>930:
                raise media.MediaError("The single-line Gold camera hook is too wide.")
            art.text(tuple(text_spec["center_xy_px"]),hook,font=font,anchor="mm",fill=text_spec["fill"],
                     stroke_width=text_spec["outline_px"],stroke_fill=text_spec["outline"])
    elif identity=="discovery-tabs" and not large:
        plates = recipe["per_line_plates"]
        px,py = plates["cap_box_padding_xy_px"]
        rake = plates["rake_px"]
        font,parts = layout(art,hook.upper(),plates["max_outer_width_px"]-2*px-rake,two=True)
        x,y = plates["anchor_cx_top_px"]
        for i,line in enumerate(parts):
            x0,y0,x1,y1 = art.textbbox((0,0),line,font=font)
            width = x1-x0+2*px
            depth = y1-y0+2*py
            left = x+plates["line_center_dx_px"][i]-(width+rake)/2
            art.polygon([(left+rake,y),(left+width+rake,y),(left+width,y+depth),(left,y+depth)],fill=plates["fill"])
            art.text((left+(width+rake)/2-(x1+x0)/2,y+py-y0),line,font=font,fill=text_spec["fill"])
            y += depth+plates["line_gap_px"]
    if large:
        center_hook(art,hook,font_at)
    persistent = identity in ("commentary-stripe","gold-camera")
    if target is not None:
        opening.save(target/"opening.png")
        if persistent:
            decoration.save(target/"decoration.png")
    return dict(hook_end=expiry,fade=min(fade,.12),persistent=persistent,gold=identity=="gold-camera")
