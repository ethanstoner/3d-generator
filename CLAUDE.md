# 3D Generator

Local web app for generating 3D models from images via a local ComfyUI / Hunyuan3D pipeline. Friends-only, password-gated. See `README.md` for setup.

## Backpack Generation Rules

These rules govern prompt creation for image-generation models (ChatGPT / Gemini / Claude image gen). The image is then fed into Hunyuan3D 2.1 to produce a GLB used as a Roblox UGC back accessory.

### IDEA SELECTION — what makes a good backpack

Must be trending RIGHT NOW:
- Currently airing/recently released anime, TV shows, movies
- Trending games (especially what Roblox kids are playing — Steal a Brainrot, Grow A Garden, Fisch, etc.)
- Pop Mart / blind box / collectible toy trends (Labubu, Sonny Angel, Skullpanda, etc.)
- Viral memes (Italian Brainrot characters)
- Upcoming holidays & seasonal events
- Cultural moments / music releases / viral TikTok stuff

Must appeal to Roblox audience:
- Skews younger (Gen Alpha)
- Brand-recognizable IPs > generic concepts
- Cute/plush/kawaii sells huge
- Funny/meme characters sell huge

Must fit the proven aesthetic:
- Smooth glossy sculptural OR simple rounded companion
- Chunky toy-like proportions
- High visual recognizability at small scale

### SUBJECT RULES — what the backpack itself can be

ONE single subject only. Never combine two items (no "book AND sword" — pick one).

Frame it as a plush/sculpted figurine character, not a traditional backpack. The Roblox attachment is handled in-engine — the 3D mesh is just the standalone toy form.

Good silhouettes:
- Plush characters in sitting/upright poses
- Smooth glossy sculptural shapes
- Rounded companion creatures with simple features
- Chunky geometry, thick proportions

### NEVER include these (image→3D pipeline can't handle them)

- Straps, strap loops, sculpted straps
- Zippers, buckles, clips, mechanical hardware
- Fabric, cloth, capes — these don't convert to 3D
- Flame effects, fire, glow effects
- Particles, transparency, wispy/thin elements
- Cards, flat thin objects
- Baskets/containers with many small items inside
- Rocky/rough textures, crystal shards
- Multiple subjects in one image
- Fine mechanical detail

### IMAGE GENERATION RULES

Background: Solid white background (clean cutout). No environment, no scenery, no props besides the subject.

Composition: Front-facing, centered in frame, soft diffused studio lighting.

Style anchors that work:
- "premium Pop Mart vinyl plush"
- "high-end collectible toy"
- "smooth glossy figurine"
- "premium Funko Pop"
- "high-quality vinyl figure"
- "premium Japanese arcade prize plush"

Surface descriptors: smooth, glossy, matte, fuzzy, plush, chunky toy proportions, exaggerated rounded proportions.

### PROMPT STRUCTURE

Natural language, NOT comma-separated tags. 50–150 words. Describe like explaining to a human.

Always:
1. NAME the character AND franchise (lets the AI use its own knowledge).
2. Include detailed visual backup (exact colors, shapes, proportions, materials) in case the AI gets details wrong.
3. End with composition/lighting line: "Solid white background, 3D render style, front-facing centered composition, soft studio lighting".

The prompt must be fully self-contained — never reference an "attached image", "reference image", or "based on this image".
