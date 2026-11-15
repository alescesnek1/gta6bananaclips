"""
banany.py - end cards "BANANA SCORE N/10" for gta6bananaclips: the short is a jar and N big
banana guys drop into it (real rigid-body physics); 10 bananas fill the whole 9:16 frame.

Blender 5.2, no window:
  blender -b --factory-startup -P tools/rating/banany.py -- <out_dir> [N ...]

Plays AFTER the clip on its frozen last frame (klipuj.py), so it never covers a moment.
Look borrowed from the Rody series (Videos/serial): blocky low-poly toy characters (block
eyes with a glint, mood mouth), warm sun + cool rim light, spring pop of the score, camera
shake on every impact, the series' dark rounded "DEN 13" label. Each score is its own scene:
the count, the faces, how they fall (1 = one lonely sad banana ... 10 = a pile up to the top
and a golden king banana with a crown + confetti). Transparent, 1080x1920, 30 fps, 2.6 s.

Writes <out_dir>/score_N/0001.png ... and <out_dir>/score_N.json (impact times for the sound).
"""
import json
import math
import random
import sys
from pathlib import Path

import bmesh
import bpy
from mathutils import Euler, Vector

ARGS = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
OUT = Path(ARGS[0]) if ARGS else Path("out_rating")
SCORES = [int(x) for x in ARGS[1:] if x.isdigit()] or list(range(1, 11))
FONT = Path(__file__).resolve().parents[2] / "fonts" / "cap" / "montserrat-900.ttf"
FPS, FRAMES = 30, 78                          # 2.6 s
W, H = 1080, 1920
HALF_W, HALF_H = 5.0, 5.0 * H / W             # frame in world units (ortho camera, 10 wide)
BANANA = 3.6                                  # banana guy scale: 10 of them fill the frame

def spring(t, freq=2.6, damp=6.0):
    return 0.0 if t <= 0 else 1 - math.exp(-damp * t) * math.cos(2 * math.pi * freq * t)

def clamp01(t):
    return min(max(t, 0.0), 1.0)

def ease_out(t):
    return 1 - (1 - clamp01(t)) ** 3

def srgb(h):
    c = [int(h[i:i + 2], 16) / 255 for i in (1, 3, 5)]
    return tuple(x / 12.92 if x <= 0.04045 else ((x + 0.055) / 1.055) ** 2.4 for x in c)

def mat(name, hexc, emit=0.0, rough=0.55):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    rgb = srgb(hexc)
    b.inputs["Base Color"].default_value = (*rgb, 1)
    b.inputs["Roughness"].default_value = rough
    if emit:
        b.inputs["Emission Color"].default_value = (*rgb, 1)
        b.inputs["Emission Strength"].default_value = emit
    return m

def clear():
    bpy.ops.wm.read_factory_settings(use_empty=True)

def box_mesh(name, parts, mats):
    """parts: [(center, size xyz, rotation euler, material index)] -> one flat-shaded mesh object."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    for c, s, r, mi in parts:
        geom = bmesh.ops.create_cube(bm, size=1.0)["verts"]
        bmesh.ops.scale(bm, vec=Vector(s), verts=geom)
        bmesh.ops.rotate(bm, cent=Vector((0, 0, 0)), matrix=Euler(r).to_matrix(), verts=geom)
        bmesh.ops.translate(bm, vec=Vector(c), verts=geom)
        for f in {f for v in geom for f in v.link_faces}:
            f.material_index = mi
    bm.to_mesh(me)
    bm.free()
    for m in mats:
        me.materials.append(m)
    o = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(o)
    return o

def key(o, f, loc=None, rot=None, sc=None):
    if loc is not None:
        o.location = loc
        o.keyframe_insert("location", frame=f)
    if rot is not None:
        o.rotation_euler = rot
        o.keyframe_insert("rotation_euler", frame=f)
    if sc is not None:
        o.scale = (sc, sc, sc) if isinstance(sc, (int, float)) else sc
        o.keyframe_insert("scale", frame=f)

def frames():
    for f in range(1, FRAMES + 1):
        yield f, (f - 1) / FPS

M = {}

def set_scale(k):
    """Banana size; build() shrinks it until all N bananas fit inside the frame."""
    global L_BAN, R_BAN, SAG, FS, FACE_Y, FACE_Z
    L_BAN, R_BAN, SAG = 7.0 * k, 1.15 * k, 0.8 * k      # banana length, max radius, curve
    FS = 2.9 * k                                         # face scale: big googly cartoon eyes
    FACE_Y = -0.809 * R_BAN - 0.01                       # the flat front ridge (pentagon side facing -y)
    FACE_Z = -SAG * 0.35

set_scale(1.0)

def spine(s):
    """Banana centre line at s (0 = stem end, 1 = tip): (x, z, tangent angle)."""
    x = (s - 0.5) * L_BAN
    z = SAG * (2 * (s - 0.5)) ** 2 - SAG * 0.35
    return x, z, math.atan(SAG * 8 * (s - 0.5) / L_BAN)

def banana_body(name, gold, rnd):
    """
    A real banana shape: a 5-sided ridged tube along a curved spine, fat in the middle, thin
    ends, greenish neck, brown stem and tip, a few brown specks. Lies along x, ~4.6 long.
    """
    RINGS, SIDES = 16, 5
    bm = bmesh.new()
    rings = []
    for k in range(RINGS + 1):
        s = k / RINGS
        x, z, ang = spine(s)
        r = R_BAN * (0.22 + 0.78 * math.sin(math.pi * (0.06 + 0.88 * s)) ** 0.55)
        ring = []
        for j in range(SIDES):
            a = 2 * math.pi * j / SIDES + math.radians(144)      # one flat side faces the camera
            ly, lz = r * math.cos(a), r * math.sin(a) * 0.92
            ring.append(bm.verts.new((x - lz * math.sin(ang), ly, z + lz * math.cos(ang))))
        rings.append(ring)
    for k in range(RINGS):
        sk = (k + 0.5) / RINGS
        for j in range(SIDES):
            f = bm.faces.new((rings[k][j], rings[k][(j + 1) % SIDES], rings[k + 1][(j + 1) % SIDES], rings[k + 1][j]))
            front = j == 0
            band = 1 if sk > 0.95 else 2 if sk < 0.1 else 5 if sk < 0.2 else 6 if sk > 0.86 else 0
            f.material_index = 3 if (band == 0 and not front and rnd.random() < 0.06) else band
    bm.faces.new(rings[-1]).material_index = 1
    bm.faces.new(list(reversed(rings[0]))).material_index = 2
    x0, z0, a0 = spine(0.0)                                      # stem: a short bent 5-sided prism
    stem = bmesh.ops.create_cone(bm, cap_ends=True, segments=5, radius1=0.15, radius2=0.11, depth=0.7)["verts"]
    bmesh.ops.rotate(bm, cent=Vector((0, 0, 0)), matrix=Euler((0, math.radians(-90) - a0 - 0.5, 0)).to_matrix(), verts=stem)
    bmesh.ops.translate(bm, vec=Vector((x0 - 0.3, 0, z0 + 0.18)), verts=stem)
    for f in {f for v in stem for f in v.link_faces}:
        f.material_index = 4
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces[:])
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    for m in (M["gold"] if gold else M["yellow"], M["brown"], M["neck"], M["speck"], M["stem"],
              M["gold"] if gold else M["light"], M["gold"] if gold else M["ochre"]):
        me.materials.append(m)
    o = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(o)
    for k in range(rnd.randint(4, 11) if not gold else 0):          # ripe brown specks on the peel
        sk = rnd.uniform(0.15, 0.85)
        x, z, ang = spine(sk)
        r = R_BAN * (0.22 + 0.78 * math.sin(math.pi * (0.06 + 0.88 * sk)) ** 0.55)
        a = rnd.uniform(math.radians(40), math.radians(140))            # top half, seen from the front
        if abs(x) < 1.3 and a > math.radians(60) and a < math.radians(120):
            continue                                                    # keep the face area clean
        sp = ball(f"{name}_sp{k}", rnd.uniform(0.05, 0.11), (1.4, 0.4, 1), (x, -r * math.cos(a) * 0.95, z + r * math.sin(a) * 0.88),
                  M["speck"], o)
        sp.rotation_euler = (math.radians(90) - a, 0, 0)
    return o

def ball(name, r, scale, loc, m, parent):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=r)
    bm.to_mesh(me)
    bm.free()
    for poly in me.polygons:
        poly.use_smooth = True
    me.materials.append(m)
    o = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(o)
    o.scale, o.location, o.parent = scale, loc, parent
    return o

def bar(name, size, loc, m, parent, rot=(0, 0, 0)):
    o = box_mesh(name, [((0, 0, 0), size, (0, 0, 0), 0)], [m])
    o.location, o.rotation_euler, o.parent = loc, rot, parent
    return o

# ten faces: eye sizes (l, r), pupil size, lids (eye height 0-1), brows (l, r angle) or None,
# mouth (width, height, x offset), teeth, tongue, sunglasses
FACES = [
    dict(name="grin", eyes=(1, 1), pupil=1.0, lids=1.0, brows=(0, 0), mouth=(1.5, 0.8, 0), teeth=True),
    dict(name="shock", eyes=(1.3, 1.3), pupil=0.85, lids=1.0, brows=(0.35, -0.35), mouth=(0.6, 1.1, 0)),
    dict(name="angry", eyes=(1, 1), pupil=1.0, lids=0.75, brows=(-0.5, 0.5), mouth=(1.2, 0.25, 0), teeth=True),
    dict(name="sleepy", eyes=(1, 1), pupil=1.0, lids=0.35, brows=None, mouth=(0.6, 0.35, 0.05)),
    dict(name="crazy", eyes=(1.35, 0.8), pupil=0.8, lids=1.0, brows=(0.4, 0.1), mouth=(1.3, 0.9, 0), tongue=True),
    dict(name="cool", eyes=(1, 1), pupil=1.0, lids=1.0, brows=None, mouth=(0.9, 0.3, 0.12), glasses=True),
    dict(name="wink", eyes=(1, 1), pupil=1.0, lids=1.0, wink=True, brows=(0.2, 0), mouth=(1.4, 0.7, 0), teeth=True),
    dict(name="scared", eyes=(1.2, 1.2), pupil=0.9, lids=1.0, brows=(0.45, -0.45), mouth=(1.0, 0.45, 0), wobble=True),
    dict(name="smug", eyes=(1, 1), pupil=1.0, lids=0.55, brows=(0.15, -0.25), mouth=(0.9, 0.3, 0.15)),
    dict(name="hype", eyes=(1.25, 1.25), pupil=1.2, lids=1.0, brows=(0.3, -0.3), mouth=(1.4, 1.3, 0), teeth=True, tongue=True),
]
POOLS = {"low": [1, 2, 3, 7, 8], "mid": [0, 3, 4, 6, 8, 2], "high": [0, 1, 4, 5, 6, 9]}

def banana_guy(name, n, i, rnd, gold=False, crown=False):
    """Detailed banana + its own animated face (separate parts parented to the body)."""
    body = banana_body(name, gold, rnd)
    pool = POOLS["low" if n <= 3 else "mid" if n <= 6 else "high"]
    face = dict(FACES[pool[(i * 3 + n) % len(pool)]])
    if crown:
        face = dict(FACES[9])
    parts = {"face": face, "eyes": [], "pupils": [], "brows": [], "mouth": None}
    root = bpy.data.objects.new(f"{name}_face", None)          # all face parts live in this scaled frame
    bpy.context.scene.collection.objects.link(root)
    root.location, root.scale, root.parent = (0.05, FACE_Y, FACE_Z + 0.05), (FS, FS, FS), body
    for side, k in ((-1, 0), (1, 1)):
        es = face["eyes"][k]
        eye = ball(f"{name}_eye{k}", 0.2 * es, (1, 0.35, 1.15), (side * (0.04 + 0.2 * es), 0, 0.14), M["white"], root)
        pup = ball(f"{name}_pup{k}", 0.1 * face["pupil"], (1, 1, 1 / 1.15), (0, -0.19 * es, 0), M["black"], eye)
        ball(f"{name}_hl{k}", 0.03, (1, 1, 1), (-0.035, -0.06, 0.04), M["white"], pup)
        parts["eyes"].append(eye)
        parts["pupils"].append(pup)
        if face.get("brows") is not None:
            parts["brows"].append(bar(f"{name}_brow{k}", (0.27, 0.06, 0.065), (side * 0.25, -0.02, 0.42),
                                      M["browc"], root, rot=(0, side * face["brows"][k], 0)))
    if face.get("glasses"):
        for side in (-1, 1):
            bar(f"{name}_gl{side}", (0.36, 0.06, 0.26), (side * 0.25, -0.1, 0.15), M["black"], root)
        bar(f"{name}_glb", (0.2, 0.05, 0.05), (0, -0.1, 0.2), M["black"], root)
    for side in (-1, 1):                                       # rosy cheeks
        ball(f"{name}_ch{side}", 0.07, (1.5, 0.3, 0.8), (side * 0.42, -0.01, -0.1), M["blush"], root)
    mw, mh, mx = face["mouth"]
    mouth = ball(f"{name}_mouth", 0.13, (mw, 0.35, mh), (mx, 0.02, -0.2), M["mouthc"], root)
    if face.get("teeth"):
        bar(f"{name}_teeth", (0.2, 0.05, 0.05), (0, -0.06, 0.06), M["white"], mouth)
    if face.get("tongue"):
        ball(f"{name}_tongue", 0.07, (1.2, 0.6, 0.8), (0, -0.07, -0.05), M["tongue"], mouth)
    parts["mouth"] = mouth
    if crown:
        cr = box_mesh(f"{name}_crown", [((-0.22 + 0.22 * j, 0, 0.12 + 0.07 * (j == 1)), (0.13, 0.2, 0.3), (0, 0, 0), 0) for j in range(3)]
                      + [((0, 0, -0.02), (0.6, 0.24, 0.12), (0, 0, 0), 0)], [M["crown"]])
        cr.location, cr.parent = (0.1, 0, R_BAN + FACE_Z + 0.25), body
    return body, parts

def animate_face(parts, t_hit, t_num, rnd, n):
    """Falling = wide eyes + yelling 'O'; impact = squeezed shut; then the face's own expression,
    pupils wander, random blinks, brows bounce, laugh (high score) or droop (low) at the score."""
    face = parts["face"]
    blinks = [t_hit + rnd.uniform(0.4, 0.8)]
    while blinks[-1] < 2.6:
        blinks.append(blinks[-1] + rnd.uniform(0.7, 1.4))
    ph = rnd.uniform(0, 6.28)
    for f, t in frames():
        u = t - t_hit
        falling = u < 0
        hit = 0 <= u < 0.12
        blink = any(0 <= t - b < 0.1 for b in blinks)
        for k, (eye, pup) in enumerate(zip(parts["eyes"], parts["pupils"])):
            es = face["eyes"][k]
            lid = face["lids"] * (0.12 if face.get("wink") and k == 1 else 1)
            hz = 1.15 * (1.25 if falling else 0.12 if (hit or blink) else lid)
            key(eye, f, sc=(1.0 * (1.15 if falling else 1), 0.35, hz))
            if falling:
                px, pz = 0.0, 0.06
            elif t > t_num:
                px, pz = -0.02, 0.07                     # look up at the score
            else:
                px, pz = 0.05 * math.sin(3.1 * t + ph), 0.035 * math.cos(2.3 * t + ph)
            key(pup, f, loc=(px, -0.19 * es, pz))
        for k, br in enumerate(parts["brows"]):
            jump = 0.12 * math.exp(-8 * max(u, 0)) if u > 0 else (0.1 if falling else 0)
            key(br, f, loc=(br.location.x, br.location.y, 0.42 + jump))
        mw, mh, mx = face["mouth"]
        if falling:
            sx, sz = 0.7, 1.3                            # AAAAA
        elif hit:
            sx, sz = mw * 1.2, 0.15
        else:
            sx, sz = mw, mh
            if t > t_num and n >= 7:
                sz = mh * (0.75 + 0.45 * abs(math.sin(16 * (t - t_num))))     # laughing
            elif t > t_num and n <= 3:
                sz = mh * 0.6
            if face.get("wobble"):
                sx *= 1 + 0.12 * math.sin(30 * t)
        key(parts["mouth"], f, loc=(mx, 0.02, -0.2), sc=(sx, 0.35, sz))

def text(body, size, mfront, mback):
    out = []
    for m, ext, off in ((mfront, 0.1, 0.0), (mback, 0.26, 0.09)):
        cu = bpy.data.curves.new("t", "FONT")
        cu.body = body
        cu.font = bpy.data.fonts.load(str(FONT), check_existing=True)
        cu.size, cu.align_x, cu.align_y = size, "CENTER", "CENTER"
        cu.extrude, cu.offset = ext, off               # fatter dark copy behind = chunky outline
        cu.materials.append(m)
        o = bpy.data.objects.new("txt", cu)
        bpy.context.scene.collection.objects.link(o)
        out.append(o)
    out[1].parent = out[0]
    out[1].location = (0.0, 0.0, -0.32)            # fully behind the front letters
    return out[0]

def pill(label):
    o = box_mesh("pill", [((0, 0, 0), (6.0, 0.3, 1.05), (0, 0, 0), 0)], [M["navy"]])
    bev = o.modifiers.new("round", "BEVEL")
    bev.width, bev.segments = 0.44, 6
    cu = bpy.data.curves.new("pt", "FONT")
    cu.body = label
    cu.font = bpy.data.fonts.load(str(FONT), check_existing=True)
    cu.size, cu.align_x, cu.align_y = 0.6, "CENTER", "CENTER"
    cu.materials.append(M["white"])
    t = bpy.data.objects.new("pilltxt", cu)
    bpy.context.scene.collection.objects.link(t)
    t.parent = o
    t.location = (0, -0.17, -0.02)
    t.rotation_euler = (math.radians(90), 0, 0)
    return o

def cube(name, size, m):
    return box_mesh(name, [((0, 0, 0), (size, size, size), (0, 0, 0), 0)], [m])

PALETTE = {1: "#E63946", 2: "#E85D3A", 3: "#F08A24", 4: "#F4A62A", 5: "#F7C531",
           6: "#C9D63A", 7: "#8BD346", 8: "#3CCB7F", 9: "#2EC4B6", 10: "#FFD000"}

def setup(n):
    sc = bpy.context.scene
    for eng in ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"):
        try:
            sc.render.engine = eng
            break
        except TypeError:
            continue
    sc.render.resolution_x, sc.render.resolution_y = W, H
    sc.render.fps = FPS
    sc.frame_start, sc.frame_end = 1, FRAMES
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    try:
        sc.view_settings.view_transform = "Standard"
    except TypeError:
        pass
    sc.eevee.taa_render_samples = 24
    cam = bpy.data.objects.new("cam", bpy.data.cameras.new("cam"))
    cam.data.type = "ORTHO"
    cam.data.ortho_scale = 2 * HALF_H                 # ortho_scale spans the longer side (height)
    sc.collection.objects.link(cam)
    sc.camera = cam
    sun = bpy.data.objects.new("sun", bpy.data.lights.new("sun", "SUN"))
    sun.data.energy, sun.data.color, sun.data.angle = 3.6, (1.0, 0.93, 0.82), math.radians(8)
    sun.rotation_euler = (math.radians(55), math.radians(-20), math.radians(-30))
    sc.collection.objects.link(sun)
    rim = bpy.data.objects.new("rim", bpy.data.lights.new("rim", "SUN"))
    rim.data.energy, rim.data.color = 1.5, (0.75, 0.85, 1.0)
    rim.rotation_euler = (math.radians(-60), 0, math.radians(160))
    sc.collection.objects.link(rim)
    world = bpy.data.worlds.new("World")
    sc.world = world
    world.use_nodes = True
    world.node_tree.nodes["Background"].inputs[0].default_value = (0.6, 0.68, 0.82, 1)
    world.node_tree.nodes["Background"].inputs[1].default_value = 0.8
    col = PALETTE[n]
    M.clear()
    M.update(light=mat("li", "#F4E15A", rough=0.4), ochre=mat("oc", "#E9B536", rough=0.45), blush=mat("bh", "#FF8FA3"),
             yellow=mat("y", "#FFD43B", rough=0.38), pale=mat("p", "#D8CB86"), gold=mat("g", "#FFC21A", emit=0.8, rough=0.25),
             brown=mat("b", "#5B3A1E"), black=mat("k", "#141414", rough=0.3), white=mat("w", "#FFFFFF"),
             crown=mat("cr", "#FFB000", emit=1.2, rough=0.2), navy=mat("n", "#1E2230", rough=0.4),
             num=mat("num", col, emit=0.3), numback=mat("nb", "#1B1B22"), blue=mat("bl", "#5AB0FF", emit=0.6),
             conf1=mat("c1", "#FF4D6D", emit=0.6), conf2=mat("c2", "#4DD4FF", emit=0.6), conf3=mat("c3", "#8CFF4D", emit=0.6),
             neck=mat("ne", "#C9D24A"), speck=mat("sp", "#7A5A2A"), stem=mat("st", "#5E4B25"), browc=mat("bc", "#3B2414"),
             mouthc=mat("mc", "#3A1010", rough=0.3), tongue=mat("tg", "#FF6F91"))
    # the jar: floor + walls at the frame edges, front/back glass keeps them in a slab
    if sc.rigidbody_world is None:
        bpy.ops.rigidbody.world_add()
    rw = sc.rigidbody_world
    rw.point_cache.frame_start, rw.point_cache.frame_end = 1, FRAMES
    rw.substeps_per_frame, rw.solver_iterations = 20, 20
    sc.gravity = (0, 0, -85)                          # big bananas, short card: fall fast
    for name, loc, size in (("floor", (0, 0, -HALF_H - 0.5), (2 * HALF_W + 4, 6, 1)),
                            ("wl", (-HALF_W - 0.5, 0, 10), (1, 6, 60)), ("wr", (HALF_W + 0.5, 0, 10), (1, 6, 60)),
                            ("front", (0, -1.18, 10), (2 * HALF_W + 4, 0.4, 60)), ("back", (0, 1.4, 10), (2 * HALF_W + 4, 0.4, 60))):
        w = box_mesh(name, [((0, 0, 0), size, (0, 0, 0), 0)], [M["navy"]])
        w.location = loc
        w.hide_render = True
        bpy.context.view_layer.objects.active = w
        bpy.ops.rigidbody.object_add(type="PASSIVE")
        w.rigid_body.friction, w.rigid_body.restitution = 0.9, 0.1
    return cam

def mood_of(n):
    return "sad" if n <= 2 else "meh" if n <= 4 else "happy" if n <= 7 else "wow"

def build(n):
    clear()
    cam = setup(n)
    rnd = random.Random(n * 17)
    mood = mood_of(n)
    king = n - 1 if n == 10 else -1                   # 10/10: the last one is the golden king
    guys, faces = [], []
    floor = bpy.data.objects["floor"]
    for i in range(n):
        g, parts = banana_guy(f"b{i}", n, i, rnd, gold=(i == king), crown=(i == king))
        faces.append(parts)
        side = (-1) ** i
        g.location = (side * rnd.uniform(0.2, 1.4) if n > 1 else 0.3, 0, HALF_H + 4.5 + i * (L_BAN + 0.6))   # a standing banana is 7 long: no overlap at spawn
        # mixed tumble: lying, diagonal, standing on end, upside down (Aleš: not only stacked lengthwise)
        kind = rnd.random()
        ang = (rnd.uniform(-20, 20) if kind < 0.3 else rnd.choice((-1, 1)) * rnd.uniform(30, 60) if kind < 0.6
               else rnd.choice((-1, 1)) * rnd.uniform(70, 90))
        if i != king and rnd.random() < 0.2:
            ang += 180
        g.rotation_euler = (0, math.radians(ang), 0)
        bpy.context.view_layer.objects.active = g
        bpy.ops.rigidbody.object_add(type="ACTIVE")
        rb = g.rigid_body
        rb.collision_shape = "CONVEX_HULL"
        rb.mass, rb.friction, rb.restitution = 1.0, 0.8, 0.25 if n >= 8 else 0.12
        rb.linear_damping, rb.angular_damping = 0.1, 0.6
        # (no constraint: a generic constraint over this distance blew the solver up; the tight
        # front/back glass of the jar keeps every banana facing the camera)
        guys.append(g)

    # pass 1: run the physics, record when each banana hits something (velocity flips)
    impacts, prev = [], {g.name: None for g in guys}
    sc = bpy.context.scene
    for f in range(1, FRAMES + 1):
        sc.frame_set(f)
        for g in guys:
            z = g.matrix_world.translation.z
            p = prev[g.name]
            if p is not None and p[1] is not None and p[1] < -0.25 and z - p[0] > -0.05:
                if not any(abs((f - 1) / FPS - t) < 0.12 and nm == g.name for t, nm in impacts):
                    impacts.append(((f - 1) / FPS, g.name))
            prev[g.name] = (z, None if p is None else z - p[0])
    # every banana must end up inside the frame (Aleš: 10 means 10 you can count)
    tops = [max((g.matrix_world @ Vector(c)).z for c in g.bound_box) for g in guys]
    fits = max(tops) < HALF_H - 0.3 and all(abs(g.matrix_world.translation.x) < HALF_W for g in guys)
    print(f"score {n}: highest banana top {max(tops):.1f} / frame {HALF_H:.1f} -> {'fits' if fits else 'overflows'}")
    first_hit = {}
    for t, nm in impacts:
        first_hit.setdefault(nm, t)
    t_last = max(first_hit.values(), default=0.6)
    sc.frame_set(1)

    # score: label + number punch in after the last banana lands (top area of the jar)
    t_num = min(t_last + 0.12, 1.9)
    lab = pill("BANANA SCORE")
    num = text(f"{n}/10", 4.0, M["num"], M["numback"])
    for f, t in frames():
        u = t - t_num
        s = spring(u, 2.3, 5.0)
        tilt = math.radians(-12) * math.exp(-5 * max(u, 0)) * math.cos(12 * max(u, 0)) if u > 0 else 0
        key(num, f, loc=(0, -8.0, HALF_H * 0.42), rot=(math.radians(90), tilt, 0), sc=max(s, 0.0001))
        key(lab, f, loc=(0, -8.0, HALF_H * 0.42 + 3.2 + 3 * (1 - spring(u + 0.08, 2.2, 7))), sc=max(spring(u + 0.08, 2.2, 7), 0.0001))

    for g, parts in zip(guys, faces):
        animate_face(parts, first_hit.get(g.name, 0.7), t_num, rnd, n)

    # confetti for 8+
    if n >= 8:
        mats = [M["conf1"], M["conf2"], M["conf3"], M["gold"]]
        for c in range(24 if n < 10 else 60):
            q = cube(f"cf{c}", rnd.uniform(0.14, 0.3), mats[c % 4])
            ang = rnd.uniform(0.1 * math.pi, 0.9 * math.pi)
            sp, spin = rnd.uniform(6, 13), rnd.uniform(5, 14)
            ox = rnd.choice((-HALF_W + 0.5, HALF_W - 0.5)) if n == 10 else 0
            for f, t in frames():
                u = t - t_num
                if u < 0:
                    key(q, f, loc=(ox, -2.6, HALF_H * 0.42), rot=(0, 0, 0), sc=0.0001)
                    continue
                vx = math.cos(ang) * sp * (-1 if ox > 0 else 1)
                key(q, f, loc=(ox + vx * u, -2.6, HALF_H * 0.42 + math.sin(ang) * sp * u - 9 * u * u),
                    rot=(spin * u, spin * u * 0.7, 0), sc=max(0.0001, 1 - u / 1.2))

    # camera shake on impacts and on the score punch
    shakes = sorted(set(first_hit.values())) + [t_num + 0.04]
    for f, t in frames():
        dx = dz = 0.0
        for i, ts in enumerate(shakes):
            u = t - ts
            if 0 < u < 0.35:
                a = (0.12 if i < len(shakes) - 1 else 0.18) * math.exp(-10 * u)
                dx += a * math.sin(70 * u)
                dz += a * math.cos(55 * u)
        key(cam, f, loc=(dx, -40, dz), rot=(math.radians(90), 0, 0))

    pops = sorted([(t, "thud") for t in first_hit.values()] + [(t_num, "num")] + ([(t_num, "burst")] if n >= 8 else []))
    return pops, fits

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for n in SCORES:
        k = 1.0
        for _ in range(8):                               # shrink until all n fit in the jar
            set_scale(k)
            pops, fits = build(n)
            if fits:
                break
            k *= 0.93
        if '--probe' in ARGS:
            continue
        d = OUT / f"score_{n}"
        d.mkdir(exist_ok=True)
        bpy.context.scene.render.filepath = str(d) + "/"
        bpy.ops.render.render(animation=True)
        (OUT / f"score_{n}.json").write_text(json.dumps({"score": n, "pops": pops, "dur": FRAMES / FPS}))
        print(f"score {n}: done, impacts {[round(t, 2) for t, k in pops if k == 'thud']}")

main()
