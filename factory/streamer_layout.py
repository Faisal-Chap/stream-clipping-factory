"""Camera and gameplay geometry for the standard portrait recipe."""

import math
import sys

from . import media

RECIPE = "source-edits-35-65-v1"
WIDTH, FACE_ROWS, GAME_ROWS = 1080, 672, 1248


class FramingRequired(media.MediaError):
    def __init__(self, reason):
        super().__init__(f"Needs camera framing / source unsuitable. {reason}")


def rectangle(value):
    if not value or not value.strip():
        return None
    try:
        rect = [float(part) for part in value.split(",")]
        x, y, w, h = rect
        if all(math.isfinite(n) and n >= 0 for n in rect) and min(w, h) >= .035 and x+w <= 1 and y+h <= 1:
            return rect
    except ValueError:
        pass
    raise ValueError("Enter a normalized x, y, width, height rectangle inside the recording.")


def pixels(rect, width, height):
    x, y, w, h = rect
    edge = lambda n: round(n/2)*2
    left, top = edge(x*width), edge(y*height)
    return left, top, edge((x+w)*width)-left, edge((y+h)*height)-top


def overlap(first, second):
    return all(max(first[i],second[i]) < min(first[i]+first[i+2],second[i]+second[i+2]) for i in (0,1))


def gameplay(camera, override):
    x, _, w, _ = camera
    if override:
        if override[0]+override[2] <= x+.002 or override[0] >= x+w-.002:
            return override
        raise FramingRequired("The gameplay region overlaps the saved camera. Adjust the source rectangles.")
    if x+w <= .40:
        return [x+w, 0, 1-x-w, 1]
    if x >= .60:
        return [0, 0, x, 1]
    raise FramingRequired("A central camera leaves no verified gameplay area. Set the gameplay region in the editor.")


def overlay_boxes(frames, cv2, np):
    """Find side overlays with horizontal and vertical boundaries across the shot."""
    height,width = frames[0].shape
    stack = np.asarray(frames,dtype=np.int16)
    horizontal = ((np.abs(np.diff(stack,axis=1))>=12).mean(axis=0)>=.45).astype(np.uint8)
    vertical = ((np.abs(np.diff(stack,axis=2))>=12).mean(axis=0)>=.45).astype(np.uint8)
    lines = cv2.HoughLinesP(horizontal,1,np.pi/180,26,minLineLength=width*.085,maxLineGap=12)
    rails = sorted({(round((y1+y2)/2),min(x1,x2),max(x1,x2)) for x1,y1,x2,y2 in
                    (lines[:,0] if lines is not None else []) if abs(y1-y2)<=2 and
                    (max(x1,x2)<=width*.42 or min(x1,x2)>=width*.58)})
    boxes = []
    # Edges flush with the screen have no second vertical rail. Score the
    # exposed rail and both horizontal edges directly along each margin.
    for flipped in (False,True):
        rows = horizontal[:,::-1] if flipped else horizontal
        cols = vertical[:,::-1] if flipped else vertical
        sums = rows.cumsum(axis=1)
        for wide in range(round(width*.09),round(width*.35)):
            rail = cols[:,wide-1]
            if rail.sum()<height*.09:
                continue
            coverage = sums[:,wide-1]/wide
            peaks = np.flatnonzero((coverage>=.50)&(coverage>=np.r_[0,coverage[:-1]])&
                                   (coverage>=np.r_[coverage[1:],0]))
            for i,top in enumerate(peaks):
                for bottom in peaks[i+1:]:
                    tall = bottom-top
                    if tall>min(height*.42,wide/1.1):
                        break
                    if tall<max(height*.11,wide/2.25):
                        continue
                    strength = rail[top:bottom].mean()
                    if strength<.58:
                        continue
                    score = strength+min(coverage[top],coverage[bottom])
                    boxes.append((score,[(width-wide)/width if flipped else 0,(top+1)/height,
                                         wide/width,tall/height]))
    vertical = cv2.dilate(vertical,np.ones((1,3),np.uint8))
    for index,(top,left,right) in enumerate(rails):
        for bottom,a,b in rails[index+1:]:
            tall = bottom-top
            if tall>height*.42:
                break
            if tall<height*.11 or max(abs(a-left),abs(b-right))>width*.035:
                continue
            x1 = 0 if min(a,left)<9 else round((a+left)/2)
            x2 = width if max(b,right)>width-10 else round((b+right)/2+1)
            wide = x2-x1
            if not width*.09<=wide<=width*.35 or not 1.1<=wide/tall<=2.25:
                continue
            edges = [vertical[top:bottom,max(0,x-3):min(width-1,x+4)].max(axis=1).mean()
                     for x in (x1,x2) if 0<x<width]
            if not edges or min(edges)<.50:
                continue
            score = min(edges) + min(horizontal[top,x1:x2].mean(),horizontal[bottom,x1:x2].mean())
            boxes.append((score,[x1/width,(top+1)/height,wide/width,tall/height]))
    boxes.sort(key=lambda box:box[0],reverse=True)
    distinct = []
    for _,box in boxes:
        if not any(max(abs(a-b) for a,b in zip(box,other))<.025 for other in distinct):
            distinct.append(box)
        if len(distinct)==4:
            break
    return distinct


def evidence(master, cut, override):
    """Sample the same decoded source interval used by the compositor, including its ends."""
    tools = str(media.TOOLS/"streamer-asr")
    if tools not in sys.path:
        sys.path.insert(0,tools)
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise FramingRequired("The local camera detector is unavailable.") from None
    start, end = cut["start"], cut["end"]
    interval = end-start
    step = .4 if interval < 20 else .65
    count = max(2, math.ceil(interval/step)+1)
    stamp = max(start,end-.05)
    # Decode one sequential input, avoiding repeated seeks on long-GOP archives.
    period = max(.05, (stamp-start)/(count-1))
    filters = f"fps=1/{period:.7f}:start_time=0:round=up,scale=640:-2,format=gray"
    stream = media.run([media.require("ffmpeg"),"-v","error","-threads","2","-ss",f"{start:.6f}",
                        "-i",str(master),"-t",f"{interval+period:.6f}","-vf",filters,
                        "-frames:v",str(count),"-c:v","mjpeg","-threads","1","-q:v","3","-f","image2pipe","-"],
                       max(90,math.ceil(interval*2.5)))
    frames = [cv2.imdecode(np.frombuffer(b"\xff\xd8"+piece,np.uint8),cv2.IMREAD_GRAYSCALE)
              for piece in stream.stdout.split(b"\xff\xd8")[1:]]
    if stream.returncode or len(frames) < count or any(frame is None for frame in frames):
        raise FramingRequired(f"Camera evidence did not decode across the shot at {start:.2f}s.")
    models = [cv2.CascadeClassifier(cv2.data.haarcascades+name) for name in
              ("haarcascade_frontalface_default.xml","haarcascade_frontalface_alt2.xml")]
    profile = cv2.CascadeClassifier(cv2.data.haarcascades+"haarcascade_profileface.xml")
    height,width = frames[0].shape
    regions = ([(override,True)] if override else
               [(box,True) for box in overlay_boxes(frames,cv2,np)] +
               [([0,0,.29,1],False),([.71,0,.29,1],False)])
    reasons = []
    for region,boxed in regions:
        x,y,w,h = pixels(region,width,height)
        detects, faces = [], []
        for frame in frames:
            roi = frame[y:y+h,x:x+w]
            enlarged = cv2.resize(roi,None,fx=2,fy=2)
            gray = cv2.equalizeHist(enlarged)
            hit = ()
            for classifier, view, neighbors in [(model,gray,4) for model in models]+[
                (profile,gray,4),(profile,cv2.flip(gray,1),4)
            ]:
                boxes = classifier.detectMultiScale(view,1.10,neighbors,minSize=(28,28))
                hits = [box for box in boxes if box[2]<=.26*width and box[3]<=.75*view.shape[0]]
                if hits:
                    hit = max(hits,key=lambda box: box[2])
                    if classifier is profile and view is not gray:
                        hit = (gray.shape[1]-hit[0]-hit[2],*hit[1:])
                    break
            detects.append(bool(len(hit)))
            faces.append([hit[0]/2+x,hit[1]/2+y,hit[2]/2,hit[3]/2] if len(hit) else None)
        observed = [box for box in faces if box]
        if len(observed)<max(2, math.ceil(len(frames)*(.18 if boxed and not override else .35))):
            reasons.append(f"A face was detected in {len(observed)}/{len(frames)} samples.")
            continue
        center = np.median([[a+c/2,b+d/2] for a,b,c,d in observed],axis=0)
        stable = [abs(a+c/2-center[0])<=width*.065 and abs(b+d/2-center[1])<=height*.12
                  for a,b,c,d in observed]
        if not boxed and sum(stable)<len(observed)*.85:
            reasons.append("The face did not stay in one source region.")
            continue
        extent = math.ceil(2.0/period)
        missing = [i for i, hit in enumerate(detects) if not hit]
        if (not boxed or override) and any(not any(detects[max(0,i-extent):i+extent+1]) for i in missing):
            reasons.append("A long part of the shot had no observable face.")
            continue
        fx,fy = (int(n) for n in center)
        inner = ([frame[y+4:y+h-4,x+4:x+w-4] for frame in frames] if boxed and not override else
                 [frame[max(0,fy-12):fy+12,max(0,fx-12):fx+12] for frame in frames])
        motion = [np.mean(cv2.absdiff(a,b))>.65 for a,b in zip(inner,inner[1:])]
        freeze = math.ceil(3.0/period)
        if len(frames)>3 and (not any(motion) or
                              any(not any(motion[i:i+freeze]) for i in range(len(motion)-freeze+1))):
            reasons.append("The face region appeared frozen.")
            continue
        if boxed:
            camera = [float(value) for value in region]
        else:
            face_w,face_h = np.median([[c,d] for _,_,c,d in observed],axis=0)
            crop_h = min(height*.41,max(65,face_h*1.65))
            crop_w = min(width*.34,crop_h*WIDTH/FACE_ROWS)
            camera = [float(np.clip(center[0]-crop_w/2,0,width-crop_w)/width),
                      float(np.clip(center[1]-crop_h*.49,0,height-crop_h)/height),
                      float(crop_w/width),float(crop_h/height)]
        label = "Detected webcam box" if boxed and not override else "Live camera"
        return camera, f"{label} observed in {len(observed)}/{len(frames)} local samples"
    raise FramingRequired(f"Camera could not be verified near {start:.2f}–{end:.2f}s. {' '.join(reasons)} "
                          "Adjust the saved crop or choose a neighboring interval with visible camera.")
