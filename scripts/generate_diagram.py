#!/usr/bin/env python3
"""
Generate the Excalidraw data-flow diagram for the code review ingestion pipeline.
Run: python scripts/generate_diagram.py
Output: docs/ingestion_pipeline.excalidraw
"""

import json
import os
import random

random.seed(42)


# ─── helpers ──────────────────────────────────────────────────────────────────

def _base(id_, type_, x, y, w, h, **kw):
    return {
        "id": id_, "type": type_,
        "x": x, "y": y, "width": w, "height": h,
        "angle": 0,
        "strokeColor": kw.get("stroke", "#364fc7"),
        "backgroundColor": kw.get("bg", "transparent"),
        "fillStyle": "solid",
        "strokeWidth": kw.get("stroke_width", 2),
        "strokeStyle": kw.get("stroke_style", "solid"),
        "roughness": 0,
        "opacity": 100,
        "groupIds": [],
        "frameId": None,
        "roundness": kw.get("roundness", {"type": 3}),
        "seed": random.randint(1, 999999),
        "version": 1,
        "versionNonce": random.randint(1, 999999),
        "isDeleted": False,
        "boundElements": None,
        "updated": 1,
        "link": None,
        "locked": False,
    }


def rect(id_, x, y, w, h, label, *, bg="#dbe4ff", stroke="#364fc7",
         stroke_style="solid", stroke_width=2, font_size=13,
         label_color="#1a1a2e", sub_label=None):
    """Rectangle + centred text label (optional second line)."""
    els = []

    r = _base(id_, "rectangle", x, y, w, h,
              bg=bg, stroke=stroke, stroke_style=stroke_style,
              stroke_width=stroke_width)
    els.append(r)

    lines = [label]
    if sub_label:
        lines.append(sub_label)
    full_text = "\n".join(lines)
    n_lines = len(lines)
    line_h = font_size * 1.55
    text_h = line_h * n_lines
    ty = y + (h - text_h) / 2

    t = _base(id_ + "_t", "text", x + 4, ty, w - 8, text_h,
              stroke=label_color, bg="transparent",
              stroke_width=1, roundness=None)
    t.update({
        "text": full_text,
        "fontSize": font_size,
        "fontFamily": 1,
        "textAlign": "center",
        "verticalAlign": "middle",
        "baseline": font_size,
        "containerId": None,
        "originalText": full_text,
    })
    els.append(t)
    return els


def text_el(id_, x, y, w, content, *, font_size=11, color="#495057",
            align="center", font_family=1):
    """Standalone text element."""
    h = font_size * 1.55 * content.count("\n") + font_size * 1.55
    t = _base(id_, "text", x, y, w, h, stroke=color, bg="transparent",
              stroke_width=1, roundness=None)
    t.update({
        "text": content,
        "fontSize": font_size,
        "fontFamily": font_family,
        "textAlign": align,
        "verticalAlign": "top",
        "baseline": font_size,
        "containerId": None,
        "originalText": content,
    })
    return t


def arrow(id_, x1, y1, x2, y2, *, color="#495057", dashed=False,
          label=None, label_offset_x=0, label_offset_y=-14):
    """Straight arrow + optional midpoint label."""
    els = []
    dx, dy = x2 - x1, y2 - y1

    a = _base(id_, "arrow", x1, y1, abs(dx), abs(dy),
              stroke=color, bg="transparent", stroke_width=2,
              stroke_style="dashed" if dashed else "solid",
              roundness={"type": 2})
    a.update({
        "points": [[0, 0], [dx, dy]],
        "lastCommittedPoint": None,
        "startBinding": None,
        "endBinding": None,
        "startArrowhead": None,
        "endArrowhead": "arrow",
    })
    els.append(a)

    if label:
        mx = x1 + dx / 2 - 55 + label_offset_x
        my = y1 + dy / 2 + label_offset_y
        els.append(text_el(id_ + "_lbl", mx, my, 110, label,
                           font_size=10, color="#868e96"))
    return els


def bent_arrow(id_, points_list, *, color="#495057", dashed=False):
    """Multi-segment arrow defined by absolute waypoints."""
    x0, y0 = points_list[0]
    relative = [[px - x0, py - y0] for px, py in points_list]
    xs = [p[0] for p in points_list]
    ys = [p[1] for p in points_list]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)

    a = _base(id_, "arrow", x0, y0, max(w, 1), max(h, 1),
              stroke=color, bg="transparent", stroke_width=2,
              stroke_style="dashed" if dashed else "solid",
              roundness={"type": 2})
    a.update({
        "points": relative,
        "lastCommittedPoint": None,
        "startBinding": None,
        "endBinding": None,
        "startArrowhead": None,
        "endArrowhead": "arrow",
    })
    return a


# ─── layout constants ─────────────────────────────────────────────────────────

# Main center column
MX = 310          # left edge
MW = 290          # width
MCX = MX + MW // 2  # center x = 455

# Left column (external services)
LX = 10
LW = 240

# Right column (supporting components)
RX = 660
RW = 250

# Sub-analysis (fits in center column, slightly inset)
SX = MX + 15
SW = MW - 30

# Box heights
H_TITLE = 60
H_INPUT = 50
H_MAIN = 75    # pipeline steps
H_SUB = 52     # sub-analysis steps
H_OUT = 90

# Y top edges
Y = {
    "title":    0,
    "input":    75,
    "1a":       158,
    "1b":       268,
    "1c":       378,
    "1d":       488,
    "1e":       598,
    "1f":       708,
    # analysis sub-block (no extra gap — connects straight from 1f bottom)
    "ab":       800,   # analysis border top
    "1fst":     812,
    "1fla":     876,
    "1fsbr":    940,
    "ab_end":   1005,  # = ab + border_h
    "1g":       1028,
    "1h":       1138,  # optional
    "1i":       1248,
    "1j":       1358,
    "1k":       1468,  # optional
    "out":      1578,
}

AB_H = Y["ab_end"] - Y["ab"]  # height of analysis border

# External y alignments
EXT_Y = {
    "github":   Y["1a"],
    "haiku":    Y["1h"],
    "embed":    Y["1h"] + H_MAIN + 10,
    "qdrant":   Y["1k"],
}

# Right component y alignments
RC_Y = {
    "parsers":    Y["1f"],
    "analyzers":  Y["1fla"],
}


# ─── colour palette ───────────────────────────────────────────────────────────

C = {
    "main_bg":     "#dbe4ff",
    "main_stroke": "#364fc7",
    "opt_bg":      "#fff9db",
    "opt_stroke":  "#e67700",
    "ext_bg":      "#ffe8cc",
    "ext_stroke":  "#d9480f",
    "sub_bg":      "#d3f9d8",
    "sub_stroke":  "#2b8a3e",
    "right_bg":    "#f8f0fc",
    "right_stroke":"#7048e8",
    "out_bg":      "#b2f2bb",
    "out_stroke":  "#2b8a3e",
    "input_bg":    "#f1f3f5",
    "input_stroke":"#495057",
    "title_bg":    "#1e3a8a",
    "title_text":  "#ffffff",
    "arrow":       "#495057",
    "opt_arrow":   "#e67700",
    "ab_stroke":   "#2b8a3e",
    "ab_bg":       "#f0fdf4",
    "badge_bg":    "#fff4e6",
    "badge_stroke":"#fd7e14",
}


# ─── build elements ───────────────────────────────────────────────────────────

def build():
    els = []

    def add(*items):
        for it in items:
            if isinstance(it, list):
                els.extend(it)
            else:
                els.append(it)

    # ── Title ──────────────────────────────────────────────────────────────
    add(*rect("title", MX - 10, Y["title"], MW + 20, H_TITLE,
              "CODE REVIEW  INGESTION PIPELINE",
              bg=C["title_bg"], stroke=C["title_bg"],
              font_size=17, label_color=C["title_text"],
              sub_label="IngestionPipeline  ·  Steps 1a → 1k"))

    # ── Input ──────────────────────────────────────────────────────────────
    add(*rect("input", MX, Y["input"], MW, H_INPUT,
              "GitHub Repository URL",
              bg=C["input_bg"], stroke=C["input_stroke"],
              font_size=14, label_color="#1a1a2e"))

    # ── Main pipeline steps ────────────────────────────────────────────────
    STEPS = [
        ("1a", "Step 1a  —  Git Clone",
         "GitExecutor.clone_or_pull()  ·  shallow depth=1"),
        ("1b", "Step 1b  —  Workspace Setup",
         "WorkspaceManager.setup()  ·  run_id  +  dir layout"),
        ("1c", "Step 1c  —  Repo Scanner",
         "RepoScanner.scan()  ·  os.walk  →  FileInventory"),
        ("1d", "Step 1d  —  Language Detector",
         "LanguageDetector.detect()  ·  binary / ext / shebang"),
        ("1e", "Step 1e  —  File Filter",
         "FileFilter.run()  ·  vendor / binary / lock / size rules"),
        ("1f", "Step 1f  —  File Parser  ⚡ ThreadPool",
         "FileParser.parse_many()  ·  Python/Swift/Kotlin/Rust/Dart"),
        ("1g", "Step 1g  —  Chunker  ⚡ ThreadPool",
         "HierarchicalChunkBuilder.chunk_many()  ·  3-layer strategy"),
        ("1i", "Step 1i  —  Dependency Extractor",
         "DependencyExtractor.extract()  ·  CALLS/IMPORTS/INHERITS"),
        ("1j", "Step 1j  —  Graph Builder",
         "DependencyGraph.build() + analyse()  ·  NetworkX DiGraph"),
    ]
    for sid, label, sub in STEPS:
        add(*rect(sid, MX, Y[sid], MW, H_MAIN, label,
                  bg=C["main_bg"], stroke=C["main_stroke"],
                  font_size=12, label_color="#1a1a2e",
                  sub_label=sub))

    # Optional steps (dashed)
    OPT = [
        ("1h", "Step 1h  —  Summary Generator  [OPTIONAL]",
         "SummaryGenerator.run()  ·  async · semaphore(5) · batch 20"),
        ("1k", "Step 1k  —  Vector Upsert  [OPTIONAL]",
         "QdrantTool.upsert_chunks()  ·  dual vectors  ·  batch 100"),
    ]
    for sid, label, sub in OPT:
        add(*rect(sid, MX, Y[sid], MW, H_MAIN, label,
                  bg=C["opt_bg"], stroke=C["opt_stroke"],
                  stroke_style="dashed", font_size=12,
                  label_color="#7c4f00", sub_label=sub))

    # ── Analysis sub-block ─────────────────────────────────────────────────
    # Border
    add(*rect("ab", SX - 5, Y["ab"], SW + 10, AB_H,
              "",
              bg=C["ab_bg"], stroke=C["ab_stroke"],
              stroke_style="dashed", stroke_width=2))

    # Label for analysis block
    add(text_el("ab_lbl", SX, Y["ab"] + 2, SW,
                "Semantic Analysis Block",
                font_size=10, color=C["sub_stroke"], align="center"))

    SUB = [
        ("1fst",  "Step 1f-ST  —  Symbol Table",
         "ProjectSymbolTable.build()  ·  O(1) qualified lookup"),
        ("1fla",  "Step 1f-LA  —  Language Analyzer",
         "5 passes: layer / calls / bases / imports / enrichment"),
        ("1fsbr", "Step 1f-SBR  —  Boundary Resolver",
         "SymbolBoundaryResolver.resolve()  ·  split large fns"),
    ]
    for sid, label, sub in SUB:
        add(*rect(sid, SX, Y[sid], SW, H_SUB, label,
                  bg=C["sub_bg"], stroke=C["sub_stroke"],
                  font_size=11, label_color="#1a3a1a", sub_label=sub))

    # ── Output ─────────────────────────────────────────────────────────────
    add(*rect("out", MX, Y["out"], MW, H_OUT,
              "IngestionPipelineResult",
              bg=C["out_bg"], stroke=C["out_stroke"],
              font_size=15, label_color="#1a3a1a",
              sub_label="chunks · chunk_map · dependency_graph · symbol_table"))

    # ── External services (left column) ────────────────────────────────────
    EXT = [
        ("github",  "GitHub.com",          "Remote repository  ·  HTTPS clone",  EXT_Y["github"]),
        ("haiku",   "Anthropic API",        "Claude Haiku  ·  1-2 line summaries", EXT_Y["haiku"]),
        ("embed",   "Voyage AI / OpenAI",   "Embedding API  ·  1024 / 1536 dim",  EXT_Y["embed"]),
        ("qdrant",  "Qdrant Vector DB",     "code_vector + summary_vector",        EXT_Y["qdrant"]),
    ]
    for eid, label, sub, ey in EXT:
        add(*rect("ext_" + eid, LX, ey, LW, H_MAIN, label,
                  bg=C["ext_bg"], stroke=C["ext_stroke"],
                  font_size=12, label_color="#5c2d0a", sub_label=sub))

    # ── Right supporting components ────────────────────────────────────────
    add(*rect("parsers", RX, RC_Y["parsers"], RW, H_MAIN + H_MAIN // 2,
              "Language Parsers",
              bg=C["right_bg"], stroke=C["right_stroke"],
              font_size=12, label_color="#3b0764",
              sub_label="Python(ast) · Swift · Dart\nKotlin · Rust · Tree-sitter"))

    add(*rect("analyzers", RX, RC_Y["analyzers"], RW, H_MAIN,
              "Language Analyzers",
              bg=C["right_bg"], stroke=C["right_stroke"],
              font_size=12, label_color="#3b0764",
              sub_label="Dart · Kotlin · Rust · Swift"))

    # ── Vertical main-flow arrows ──────────────────────────────────────────
    cx = MCX
    VERT = [
        ("a_in_1a",  cx, Y["input"] + H_INPUT,       cx, Y["1a"],        "repo_url"),
        ("a_1a_1b",  cx, Y["1a"] + H_MAIN,           cx, Y["1b"],        "CloneResult"),
        ("a_1b_1c",  cx, Y["1b"] + H_MAIN,           cx, Y["1c"],        "WorkspaceLayout"),
        ("a_1c_1d",  cx, Y["1c"] + H_MAIN,           cx, Y["1d"],        "FileInventory"),
        ("a_1d_1e",  cx, Y["1d"] + H_MAIN,           cx, Y["1e"],        "LanguageDetectionResult"),
        ("a_1e_1f",  cx, Y["1e"] + H_MAIN,           cx, Y["1f"],        "List[FileMeta]"),
        ("a_1f_ab",  cx, Y["1f"] + H_MAIN,           cx, Y["ab"] + 12,   "List[ParsedFile]"),
        ("a_ab_1g",  cx, Y["ab_end"],                 cx, Y["1g"],        "enriched ParsedFile"),
        ("a_1g_1h",  cx, Y["1g"] + H_MAIN,           cx, Y["1h"],        "List[CodeChunk]"),
        ("a_1h_1i",  cx, Y["1h"] + H_MAIN,           cx, Y["1i"],        "summaries + embeddings"),
        ("a_1i_1j",  cx, Y["1i"] + H_MAIN,           cx, Y["1j"],        "List[DependencyEdge]"),
        ("a_1j_1k",  cx, Y["1j"] + H_MAIN,           cx, Y["1k"],        "DependencyGraph"),
        ("a_1k_out", cx, Y["1k"] + H_MAIN,           cx, Y["out"],       "upserted"),
    ]
    for aid, x1, y1, x2, y2, lbl in VERT:
        add(*arrow(aid, x1, y1, x2, y2,
                   label=lbl,
                   color=C["opt_arrow"] if "1h" in aid or "1k" in aid else C["arrow"],
                   dashed=("1h" in aid or "1k" in aid),
                   label_offset_x=18))

    # Sub-analysis vertical arrows (slightly inset at SX + SW//2)
    scx = SX + SW // 2
    add(*arrow("a_1fst_1fla",  scx, Y["1fst"] + H_SUB,  scx, Y["1fla"],  color=C["sub_stroke"]))
    add(*arrow("a_1fla_1fsbr", scx, Y["1fla"] + H_SUB,  scx, Y["1fsbr"], color=C["sub_stroke"]))

    # ── Horizontal arrows — external services ──────────────────────────────
    # GitHub → Step 1a (left to right)
    add(*arrow("a_gh_1a",
               LX + LW, Y["1a"] + H_MAIN // 2,
               MX,      Y["1a"] + H_MAIN // 2,
               color=C["ext_stroke"], label="clone"))

    # Step 1h → Claude Haiku (right to left)
    add(*arrow("a_1h_haiku",
               MX,      Y["1h"] + H_MAIN // 2,
               LX + LW, EXT_Y["haiku"] + H_MAIN // 2,
               color=C["opt_stroke"], dashed=True, label="chunks"))

    # Step 1h → Embedding API
    add(*arrow("a_1h_embed",
               MX,      Y["1h"] + H_MAIN // 2 + 10,
               LX + LW, EXT_Y["embed"] + H_MAIN // 2,
               color=C["opt_stroke"], dashed=True, label="summaries"))

    # Step 1k → Qdrant
    add(*arrow("a_1k_qdrant",
               MX,      Y["1k"] + H_MAIN // 2,
               LX + LW, EXT_Y["qdrant"] + H_MAIN // 2,
               color=C["opt_stroke"], dashed=True, label="vectors"))

    # ── Horizontal arrows — right components ───────────────────────────────
    # Parsers ← Step 1f
    add(*arrow("a_1f_parsers",
               MX + MW, Y["1f"] + H_MAIN // 2,
               RX,      RC_Y["parsers"] + (H_MAIN + H_MAIN // 2) // 2,
               color=C["right_stroke"], label="dispatch"))

    # Analyzers ← Step 1f-LA
    add(*arrow("a_1fla_analyzers",
               SX + SW, Y["1fla"] + H_SUB // 2,
               RX,      RC_Y["analyzers"] + H_MAIN // 2,
               color=C["right_stroke"], label="enrich()"))

    # ── Legend ─────────────────────────────────────────────────────────────
    LGX = RX
    LGY = Y["1k"] + 20
    add(*rect("legend", LGX, LGY, RW, 185,
              "Legend",
              bg="#f8f9fa", stroke="#adb5bd",
              font_size=13, label_color="#343a40"))

    LEGEND_ITEMS = [
        (C["main_bg"],  C["main_stroke"],  "Main pipeline step"),
        (C["opt_bg"],   C["opt_stroke"],   "Optional step (skippable)"),
        (C["sub_bg"],   C["sub_stroke"],   "Semantic analysis sub-step"),
        (C["ext_bg"],   C["ext_stroke"],   "External service / API"),
        (C["right_bg"], C["right_stroke"], "Supporting component"),
        (C["out_bg"],   C["out_stroke"],   "Pipeline output"),
    ]
    for i, (bg, stk, label) in enumerate(LEGEND_ITEMS):
        row_y = LGY + 25 + i * 26
        add(*rect(f"leg_{i}", LGX + 12, row_y, 18, 16, "",
                  bg=bg, stroke=stk, stroke_width=1.5, font_size=1))
        add(text_el(f"leg_{i}_t", LGX + 36, row_y + 1, RW - 44, label,
                    font_size=11, color="#343a40", align="left"))

    # ── Annotation callouts ────────────────────────────────────────────────
    ANN_W = 230  # annotation text width

    # Why parallel on 1f
    add(text_el("ann_1f", MX + MW + 4, Y["1f"] - 1, ANN_W,
                "⚡ ThreadPool (cpu×2, max 16)",
                font_size=10, color="#fd7e14", align="left"))

    # Why parallel on 1g
    add(text_el("ann_1g", MX + MW + 4, Y["1g"] - 1, ANN_W,
                "⚡ ThreadPool (cpu, max 8)",
                font_size=10, color="#fd7e14", align="left"))

    # Arch analysis outputs
    add(text_el("ann_1j", MX + MW + 4, Y["1j"] + 5, ANN_W,
                "→ ARCH001 cycles\n→ ARCH002 layer violations\n→ ARCH003 orphans\n→ ARCH004 high coupling",
                font_size=10, color="#7048e8", align="left"))

    # 3-layer chunking note
    add(text_el("ann_1g_detail", MX + MW + 4, Y["1g"] + 16, ANN_W,
                "L1: MODULE  L2: AST symbols  L3: sliding window",
                font_size=10, color="#2b8a3e", align="left"))

    # 5 passes note
    add(text_el("ann_1fla", SX + SW + 6, Y["1fla"] + 2, ANN_W,
                "① Layer classify  ② Call resolve\n③ Base resolve  ④ Import resolve\n⑤ Lang-specific enrich",
                font_size=10, color="#2b8a3e", align="left"))

    return els


def main():
    os.makedirs("docs", exist_ok=True)
    elements = build()
    diagram = {
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "elements": elements,
        "appState": {
            "gridSize": None,
            "viewBackgroundColor": "#ffffff",
            "exportBackground": True,
            "exportWithDarkMode": False,
        },
        "files": {},
    }
    out = "docs/ingestion_pipeline.excalidraw"
    with open(out, "w") as f:
        json.dump(diagram, f, indent=2)
    print(f"✓ Diagram written to {out}  ({len(elements)} elements)")


if __name__ == "__main__":
    main()
