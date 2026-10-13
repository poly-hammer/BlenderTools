"""Build Chaos Cloth from a verified Send2UE Hair Tool generator packet.

The packet owns topology, guide membership and simulation G. A cloth template
supplies physics settings only. No remeshing, paint generation or render-G mask
is performed here. The final asset imports a validated standard cloth collection.
"""

import hashlib
import importlib.util
import itertools
import json
import math
from pathlib import Path
import re
import sys
import uuid
from collections import defaultdict, Counter

OWNER = "Send2UE.HairGuideCloth.Owner"
CONTENT = "Send2UE.HairGuideCloth.Content"
VERSION = "send2ue.hair_guide_cloth.v1"
RENDER_ONLY_SOURCE_STATUSES = {"no_guide_after_search", "guide_weights_all_zero"}


def _sha(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _path(value):
    value = str(value or "").split(".", 1)[0]
    # Keep the names/mounts already chosen by ordinary Send2UE, including
    # Unicode asset names. Unreal itself owns asset naming and class validation.
    if not value.startswith("/") or "\x00" in value or any(part in ("", ".", "..") for part in value[1:].split("/")):
        raise ValueError("Expected an Unreal asset package path: " + value)
    return value


def _native(result, evidence_path=None):
    def checked(payload, call_failed=False):
        if not isinstance(payload, dict):
            raise RuntimeError("Native cloth operation did not return an object receipt: " + str(result)[:1500])
        if evidence_path is not None:
            Path(evidence_path).write_text(json.dumps(payload, indent=2), encoding='utf8')
        if call_failed or payload.get("success") is False:
            raise RuntimeError("Native cloth operation failed: " + json.dumps(payload, ensure_ascii=False)[:1500])
        return payload

    if isinstance(result, str):
        return checked(json.loads(result))
    for value in result or []:
        if isinstance(value, str) and value.startswith("{"):
            return checked(json.loads(value), call_failed=result[0] is False)
    raise RuntimeError("Native cloth operation did not return its receipt: " + str(result)[:1500])


def _target(record):
    folder, name = record["render_asset_path"].rsplit("/", 1)
    return _path(record.get("cloth_asset_path") or folder + "/CA_" + re.sub(r"^(?:SK|SM)_", "", name) + "_GuideCloth")


def validate_record(record):
    """Validate real file hashes and per-vertex provenance before asset writes."""
    if not isinstance(record, dict) or record.get("version") not in (1, 2):
        raise ValueError("Unsupported hair guide cloth handoff version")
    out = dict(record)
    filename = Path(record["manifest_path"]).resolve(strict=True)
    if _sha(filename) != record.get("manifest_sha256"):
        raise ValueError("Hair generator manifest changed after export")
    packet = json.loads(filename.read_text(encoding="utf8"))
    simulation_enabled = record.get("simulation_enabled", True)
    if record["version"] == 2:
        if (packet.get("version") != 2 or type(simulation_enabled) is not bool or
                packet.get("simulation_enabled") is not simulation_enabled):
            raise ValueError("Hair simulation mode differs from its export manifest")
    elif simulation_enabled is not True:
        raise ValueError("Render-only hair requires the explicit version 2 export contract")
    if any(part.get("provenance") == "render_as_sim_no_guide" for part in packet.get("parts", [])):
        raise ValueError("Legacy render-as-simulation export needs re-export with the render-only policy")
    out["simulation_enabled"] = simulation_enabled
    keys = ["render_asset_path"]
    if simulation_enabled:
        keys += ["sim_asset_path", "cloth_template_asset_path", "body_mesh_asset_path", "physics_asset_path"]
    for key in keys:
        out[key] = _path(record.get(key))
    for role in ("sim", "render"):
        fbx = record.get(role + "_fbx")
        if fbx and _sha(Path(fbx["path"]).resolve(strict=True)) != fbx["sha256"]:
            raise ValueError(role + " FBX changed after export")
        mesh = packet["meshes"][role]
        vertices, triangles, owners = mesh["vertices"], mesh["triangles"], mesh["guide_ids"]
        if role == "sim" and not simulation_enabled:
            if vertices or triangles or owners or mesh.get("weights") or record.get("sim_asset_path") or fbx:
                raise ValueError("Render-only export unexpectedly contains simulation geometry")
            continue
        if not vertices or not triangles or len(owners) != len(vertices):
            raise ValueError(role + " has incomplete topology or guide provenance")
        if any(type(owner) is not int or owner < (-1 if role == "render" else 0) for owner in owners):
            raise ValueError(role + " has invalid generator ownership")
        if any(len(p) != 3 or any(not math.isfinite(v) for v in p) for p in vertices):
            raise ValueError(role + " has invalid positions")
        for tri in triangles:
            if len(tri) != 3 or any(type(i) is not int or not 0 <= i < len(vertices) for i in tri):
                raise ValueError(role + " has invalid triangle indices")
            if len({owners[i] for i in tri}) != 1:
                raise ValueError(role + " triangle crosses generator ownership")
        if role == "sim":
            weights = mesh["weights"]
            if len(weights) != len(vertices) or any(not math.isfinite(w) or not 0 <= w <= 1 for w in weights):
                raise ValueError("Simulation source G is incomplete")
    render = packet["meshes"]["render"]
    render_only = [index for index, owner in enumerate(render["guide_ids"]) if owner == -1]
    if render_only:
        sources = packet.get("render_only_sources", [])
        if (record["version"] != 2 or render.get("render_only_vertex_indices") != render_only or
                not sources or any(source.get("status") not in RENDER_ONLY_SOURCE_STATUSES or
                                   source.get("simulation_enabled") is not False for source in sources)):
            raise ValueError("Render-only vertices require an explicit completed no-guide search or all-zero guide decision")
    elif render.get("render_only_vertex_indices") or packet.get("render_only_sources"):
        raise ValueError("Render-only provenance does not match the exported vertices")
    guided = {owner for owner in render["guide_ids"] if owner != -1}
    if not guided.issubset(set(packet["meshes"]["sim"]["guide_ids"])):
        raise ValueError("Render strands refer to missing simulation guides")
    if not simulation_enabled and guided:
        raise ValueError("Render-only export still contains guided simulation vertices")
    out["manifest_path"] = str(filename)
    return out, packet


def normalize_ownership(built, packet, tolerance_cm=0.0002):
    """Resolve import splits/welds to exact source neighborhoods, never nearest guide.

    A neighborhood with conflicting guide IDs or G is rejected. Geometry matching
    only restores source vertex identity after FBX import; it never chooses which
    guide owns a strand.
    """
    result, receipt = {}, {}
    for role in ("sim", "render"):
        source = packet["meshes"][role]
        positions = [[p[0] * 100, -p[1] * 100, p[2] * 100] for p in source["vertices"]]
        buckets = defaultdict(list)
        cell = lambda p: tuple(math.floor(v / tolerance_cm) for v in p)
        for index, point in enumerate(positions):
            buckets[cell(point)].append(index)
        gids, source_indices, errors, unique_source_identity = [], [], [], []
        for point in built[role + "_positions"]:
            key = cell(point)
            candidates = []
            for delta in itertools.product((-1, 0, 1), repeat=3):
                for index in buckets.get(tuple(key[d] + delta[d] for d in range(3)), ()):
                    error = math.dist(point, positions[index])
                    if error <= tolerance_cm:
                        candidates.append((error, index))
            if not candidates:
                raise ValueError(role + " imported vertex has no exact source identity")
            owners = {source["guide_ids"][i] for _, i in candidates}
            if len(owners) != 1:
                raise ValueError(role + " coincident source vertices have ambiguous guide identity")
            if role == "sim" and len({round(source["weights"][i], 6) for _, i in candidates}) != 1:
                raise ValueError("Coincident simulation vertices have conflicting source G")
            error, index = min(candidates)
            gids.append(source["guide_ids"][index]); source_indices.append(index); errors.append(error)
            # Same-parent coincident strands can have several original vertices.
            # Their parent remains known, but they must not share a position
            # embedding merely because coordinate matching selected one index.
            unique_source_identity.append(len(candidates) == 1)
        result[role + "_positions"] = built[role + "_positions"]
        result[role + "_triangles"] = built[role + "_triangles"]
        result[role + "_guide_ids"] = gids
        result[role + "_source_indices"] = source_indices
        result[role + "_source_identity_unique"] = unique_source_identity
        receipt[role] = {"maximum_import_error_cm": max(errors), "ambiguous": 0, "unmatched": 0}
        if role == "sim":
            expected = [source["weights"][i] for i in source_indices]
            actual = built["sim_weights"]
            if len(actual) != len(expected) or any(abs(a - b) > 1e-6 for a, b in zip(actual, expected)):
                raise ValueError("Chaos MaxDistance map differs from exported simulation G")
            result["sim_weights"] = actual
    for tri in result["sim_triangles"]:
        if len({result["sim_guide_ids"][i] for i in tri}) != 1:
            raise ValueError("Chaos import welded different source guides")
    result["provenance"] = {"weight_source": "simulation_source_own_g", "render_g_used": False,
                             "lineage": "generator_carried_id", "source_matching": receipt,
                             "no_guide_policy": "render_only_skinning"}
    # Imported splits retain the sentinel from exact source identity. The native
    # repair must remove these vertices' initial unrestricted proxy influence.
    result["render_only_vertex_indices"] = [i for i, gid in enumerate(result["render_guide_ids"]) if gid == -1]
    return result


def _render_only_receipt(record, packet):
    """Ordinary imported render geometry remains available without a cloth build.

    This exporter does not assign actor components. A previous generated asset
    is retained as an asset; it is never returned as the current cloth output.
    """
    return {"verified": True, "version": record["version"], "status": "render_only",
            "simulation_enabled": False, "cloth_asset_path": None,
            "render_asset_path": record["render_asset_path"],
            "manifest_sha256": record["manifest_sha256"], "sim_vertices": 0,
            "render_vertices": len(packet["meshes"]["render"]["vertices"]),
            "render_only_vertices": len(packet["meshes"]["render"]["vertices"]),
            "guide_count": 0, "no_guide_policy": "render_only_skinning",
            "static_guide_policy": "omit_whole_zero_weight_source",
            "static_guide_sources": [source for source in packet.get("render_only_sources", [])
                                     if source.get("status") == "guide_weights_all_zero"]}


def _verify_render_only_binding(receipt, expected_count):
    if expected_count and (receipt.get("render_only_vertices") != expected_count or
            receipt.get("render_only_skinning_blend_one") is not True or
            receipt.get("render_only_effective_sim_influences") != 0 or
            receipt.get("render_only_dormant_mapping_indices_valid") is not True or
            receipt.get("simulation_geometry_preserved") is not True):
        raise RuntimeError("Native helper did not verify render-only skinning for unguided geometry")


def _graph(unreal, asset):
    graph = asset.get_editor_property("dataflow_instance").get_editor_property("dataflow_asset")
    if not graph:
        raise ValueError("Cloth physics template requires an editable Dataflow graph")
    tool = unreal.get_default_object(unreal.DataflowAgentToolset)
    call = lambda name, *args: tool.call_method(name, args=args)
    structure = json.loads(call("GetGraphStructure", graph))
    names = {n["guid"].replace("-", "").upper(): n["name"] for n in structure["nodes"]}
    nodes, infos = {}, {}
    for node in unreal.ObjectIterator(unreal.DataflowEdNode):
        if node.get_typed_outer(unreal.Dataflow) == graph:
            info = json.loads(call("GetNodeInfo", node))
            name = names.get(info["guid"].replace("-", "").upper())
            if name:
                nodes[name], infos[name] = node, info
    return graph, call, structure, nodes, infos


def _build_source(unreal, record, path):
    asset = unreal.EditorAssetLibrary.duplicate_asset(record["cloth_template_asset_path"], path)
    if not asset:
        raise RuntimeError("Failed to duplicate the cloth physics template")
    graph, call, structure, nodes, infos = _graph(unreal, asset)
    terminals = [n for n, info in infos.items() if "TerminalNode" in info["type"]]
    if len(terminals) != 1:
        raise ValueError("Physics template must have one cloth terminal")
    terminal = terminals[0]
    incoming = {(e["toNode"], e["toPin"]): e["fromNode"] for e in structure["connections"]
                if e["fromPin"] == "Collection"}
    chain, current, seen = [], incoming.get((terminal, "CollectionLods[0]")), set()
    while current:
        if current in seen:
            raise ValueError("Cloth template contains a cycle")
        seen.add(current); chain.append(current)
        current = incoming.get((current, "Collection"))
    chain.reverse()
    config = [n for n in chain if infos[n]["type"].startswith("FChaosClothAssetSimulation")
              and "Config" in infos[n]["type"]]
    maxima = [n for n in config if "MaxDistanceConfig" in infos[n]["type"]]
    if len(maxima) != 1:
        raise ValueError("Physics template requires one MaxDistance config on LOD0")
    for name, node in list(nodes.items()):
        if name not in config and name != terminal:
            call("RemoveNode", graph, node)
            del nodes[name]
    def add(typ, name, props):
        node = call("AddNode", graph, typ, name, json.dumps(props), -900 + len(nodes) * 220, 0)
        if not node:
            raise RuntimeError("Could not create cloth node " + name)
        nodes[name] = node
        return name
    def link(a, b, output="Collection", input="Collection"):
        if not call("ConnectNodePins", nodes[a], output, nodes[b], input):
            raise RuntimeError("Could not connect " + a + " → " + b)
    for role in ("sim", "render"):
        add("FChaosClothAssetSkeletalMeshImportNode_v2", role.title() + "Import", {
            "SkeletalMesh": record[role + "_asset_path"], "bImportSimMesh": role == "sim",
            "bImportRenderMesh": role == "render", "bImportSingleSection": False,
            "bSetPhysicsAsset": False, "UVChannel": -1})
    add("FChaosClothAssetMergeClothCollectionsNode_v2", "MergeGuideAndRender", {})
    link("SimImport", "MergeGuideAndRender", input="Collections[0]")
    link("RenderImport", "MergeGuideAndRender", input="Collections[1]")
    add("FChaosClothAssetWeightMapNode", "GuideOwnG", {
        "OutputName": {"StringValue": "MaxDistance"}, "MeshTarget": "Simulation", "MapOverrideType": "ReplaceAll"})
    add("FChaosClothAssetSetPhysicsAssetNode", "BodyPhysics", {"PhysicsAsset": record["physics_asset_path"]})
    add("FChaosClothAssetProxyDeformerNode_v3", "EmbeddingInput", {
        "bUseMultipleInfluences": False, "bPreserveRenderTangents": True})
    add("FChaosClothAssetSkinningBlendNode", "SimulationKinematicBlend", {
        "KinematicVertices3D": {"StringValue": "KinematicVertices3D"}, "bUseSmoothTransition": True})
    # Both FBXs carry the exporter's own skeleton and skin weights. A body
    # closest-point transfer here would overwrite them around the jaw/face.
    order = ["MergeGuideAndRender", "GuideOwnG", "BodyPhysics"] + config + ["EmbeddingInput", "SimulationKinematicBlend"]
    for a, b in zip(order, order[1:]):
        link(a, b)
    link("GuideOwnG", maxima[0], "OutputName.StringValue", "MaxDistance.WeightMap")
    link(maxima[0], "SimulationKinematicBlend", "KinematicVertices3D", "KinematicVertices3D.StringValue")
    terminal_inputs = [p["name"] for p in infos[terminal]["inputPins"] if p["name"].startswith("CollectionLods[")]
    for pin in terminal_inputs:
        link(order[-1], terminal, input=pin)
    # The initial unrestricted embedding is a build input only. It is never
    # published: the native repair replaces EVERY vertex using generator IDs.
    baked = _native(unreal.CodexClothToolsLibrary.bake_vertex_color_weight_map(
        path, record["sim_asset_path"], "GuideOwnG", "G", 1., False, True))
    if baked.get("unmatched_sim_vertices", 1) or baked.get("snap_distance_max_cm", 1.) > .001:
        raise ValueError("Simulation G could not be transferred exactly")
    audit = _native(unreal.CodexClothToolsLibrary.dump_cloth_collection_colors(path))
    if not unreal.EditorAssetLibrary.save_loaded_asset(asset):
        raise RuntimeError("Could not save the cloth build source")
    return asset, audit


def _guide_identity_rows(packet, owner):
    sim = packet['meshes']['sim']
    parts = packet['parts']
    by_source = defaultdict(list)
    vertex_owner = {}
    for part in parts:
        gid, source = int(part['gid']), part['source_guide']
        indices, local = part['sim_vertex_indices'], part['source_vertex_indices']
        if not indices or len(indices) != len(local) or len(set(local)) != len(local):
            raise ValueError('Missing or ambiguous source vertex identity')
        for index, local_index in zip(indices, local):
            if index in vertex_owner or sim['guide_ids'][index] != gid:
                raise ValueError('Duplicate or inconsistent packet guide ownership')
            vertex_owner[index] = (gid, source, local_index)
        by_source[source].append(part)
    if set(vertex_owner) != set(range(len(sim['vertices']))):
        raise ValueError('Every packet simulation vertex needs one source guide')

    # Reconstruct native source islands from topology, not the transient exported gid order.
    adjacency = {source: {v: set() for p in ps for v in p['source_vertex_indices']}
                 for source, ps in by_source.items()}
    for triangle in sim['triangles']:
        owners = [vertex_owner[i] for i in triangle]
        if len({v[0] for v in owners}) != 1:
            raise ValueError('Packet triangle crosses parent guides')
        source = owners[0][1]
        local = [v[2] for v in owners]
        if len(set(local)) != 3:
            raise ValueError('Degenerate triangle identity')
        for a, b in ((0, 1), (1, 2), (2, 0)):
            adjacency[source][local[a]].add(local[b])
            adjacency[source][local[b]].add(local[a])

    rows = {}
    for source, source_parts in by_source.items():
        graph = adjacency[source]
        remaining = set(graph)
        islands = []
        while remaining:
            stack = [min(remaining)]
            component = set()
            while stack:
                vertex = stack.pop()
                if vertex not in remaining:
                    continue
                remaining.remove(vertex)
                component.add(vertex)
                stack.extend(graph[vertex] & remaining)
            islands.append(component)
        receipts = [part['simulation_mesh'] for part in source_parts]
        mapping = receipts[0].get('native_island_to_spline')
        if mapping is None:
            # Flat guides keep their authored mesh. Hair Tool Grid explicitly
            # identifies its native source island; there is no ribbon conversion
            # or spline remapping receipt in this export mode.
            if not all(receipt.get('effective_mode') == 'ORIGINAL' and
                       receipt.get('status') == 'flat_preserved' and
                       part.get('provenance') == 'hair_tool_grid_src_island_index'
                       for part, receipt in zip(source_parts, receipts)):
                raise ValueError('Source guide identity mapping is missing')
            mapping = {str(index): index for index in range(len(islands))}
        if set(map(int, mapping)) != set(range(len(islands))) or len(islands) != len(source_parts):
            raise ValueError('Source island-to-spline identity cannot be reconciled')
        source_sets = {frozenset(p['source_vertex_indices']): p for p in source_parts}
        if len(source_sets) != len(source_parts):
            raise ValueError('Duplicate source island')
        for local_island, vertices in enumerate(islands):
            part = source_sets.get(frozenset(vertices))
            if part is None or part['simulation_mesh'].get('native_island_to_spline', mapping) != mapping:
                raise ValueError('Each part must match one complete native island')
            gid, spline = int(part['gid']), int(mapping[str(local_island)])
            if gid in rows:
                raise ValueError('Duplicate exported gid')
            key = json.dumps([source, int(spline)], ensure_ascii=False, separators=(",", ":"))
            weight = (1 + int.from_bytes(hashlib.sha256(b"codex-parent-force-v1\0" + key.encode()).digest()[:8], "big") % (16777216 - 1)) / 16777216
            rows[gid] = {'source': source, 'spline': spline, 'key': key,
                         'native_island': local_island, 'weight': weight}
    if len({r['key'] for r in rows.values()}) != len(rows):
        raise ValueError('Multiple guide islands share one parent-spline identity')

    gids = owner['sim_guide_ids']
    if len(gids) != len(owner['sim_source_indices']) or not all(owner['sim_source_identity_unique']):
        raise ValueError('Native source correspondence is ambiguous')
    if set(gids) != set(rows):
        raise ValueError('Native and packet guide sets differ')
    for gid, source_index in zip(gids, owner['sim_source_indices']):
        if vertex_owner[source_index][0] != gid:
            raise ValueError('Native-to-packet correspondence crosses parent guides')
    weights = [rows[gid]['weight'] for gid in gids]
    return weights, rows



def _transfer_unchanged_simulation_map(old, new, values):
    """Transfer paint only through unchanged positions AND triangle support.

    Import order and omitted whole render-only guides may differ. Coincident
    vertices with conflicting paint are ambiguous and must remain untouched.
    This does not interpolate paint onto edited geometry or search for guides.
    """
    tolerance = 0.0001  # Same centimeter contract as native ownership validation.
    buckets = defaultdict(list)
    def key(position):
        return tuple(math.floor(float(x) / tolerance) for x in position)
    for index, position in enumerate(old['sim_positions']):
        buckets[key(position)].append(index)
    candidates = []
    for position in new['sim_positions']:
        center = key(position)
        matches = set()
        for delta in itertools.product((-1, 0, 1), repeat=3):
            for index in buckets.get(tuple(a+b for a, b in zip(center, delta)), ()):
                if all(abs(float(a)-float(b)) <= tolerance
                       for a, b in zip(position, old['sim_positions'][index])):
                    matches.add(index)
        if not matches:
            raise ValueError('WeightMap source geometry changed; explicit source correspondence required')
        candidates.append(matches)
    incident = defaultdict(list)
    for triangle in old['sim_triangles']:
        for index in triangle:
            incident[index].append(triangle)
    for triangle in new['sim_triangles']:
        valid = [set(), set(), set()]
        for a in candidates[triangle[0]]:
            for previous in incident[a]:
                for ordered in itertools.permutations(previous):
                    if ordered[0] == a and all(ordered[j] in candidates[triangle[j]] for j in (1, 2)):
                        for j in range(3):
                            valid[j].add(ordered[j])
        if any(not support for support in valid):
            raise ValueError('WeightMap simulation triangle support changed')
        for j in range(3):
            candidates[triangle[j]].intersection_update(valid[j])
    transferred = []
    for matches in candidates:
        painted = {values[index] for index in matches}
        if len(painted) != 1:
            raise ValueError('WeightMap source correspondence is ambiguous')
        transferred.append(painted.pop())
    return transferred


def _same_painted_vertex_support(old_faces, new_faces):
    """Allow a triangulator's diagonal flip inside the same four-vertex quad."""
    previous, current = old_faces-new_faces, new_faces-old_faces
    if any(count!=1 or len(set(tri))!=3 for faces in (previous,current) for tri,count in faces.items()):
        return False
    def boundary(a,b):
        return Counter(tuple(sorted(edge)) for tri in (a,b)
                       for edge in ((tri[0],tri[1]),(tri[1],tri[2]),(tri[2],tri[0])))
    while previous:
        a=next(iter(previous));matches=[]
        for b in previous:
            vertices=set(a)|set(b)
            if a==b or len(vertices)!=4 or len(set(a)&set(b))!=2:
                continue
            before=Counter({edge:count for edge,count in boundary(a,b).items() if count==1})
            candidates=[tri for tri in current if set(tri)<=vertices]
            for c,d in itertools.combinations(candidates,2):
                if set(c)|set(d)!=vertices or len(set(c)&set(d))!=2:
                    continue
                after=Counter({edge:count for edge,count in boundary(c,d).items() if count==1})
                if before==after:matches.append((b,c,d))
        if len(matches)!=1:
            return False
        b,c,d=matches[0]
        for tri in (a,b):del previous[tri]
        for tri in (c,d):del current[tri]
    return not current


def _rooted_ribbon_parameters(native, indices, source_ids):
    """Validate the source-ordered two-rail strip, then parameterize its center."""
    indices = sorted(indices, key=lambda index: source_ids[index])
    if len(indices) < 4 or len(indices) % 2:
        raise ValueError('Authored paint transfer requires a complete two-rail ribbon')
    local = {index: rank for rank, index in enumerate(indices)}
    faces = Counter(tuple(sorted(local[i] for i in tri)) for tri in native['sim_triangles']
                    if all(i in local for i in tri))
    expected = Counter(tuple(sorted(tri)) for row in range(len(indices)//2-1)
                       for tri in ((2*row,2*row+1,2*row+3),(2*row,2*row+3,2*row+2)))
    if not _same_painted_vertex_support(faces,expected) or any(any(i in local for i in tri) and not all(i in local for i in tri)
                                for tri in native['sim_triangles']):
        raise ValueError('Authored paint ribbon has changed rails or triangle support')
    kinematic = set(native['kinematic_sim_indices'])
    fixed = [i in kinematic for i in indices]
    if (fixed[:2] != [True, True] or any(fixed[-2:]) or
            any(fixed[i] != fixed[i+1] for i in range(0,len(fixed),2)) or
            any(a < b for a,b in zip(fixed,fixed[1:]))):
        raise ValueError('Authored paint ribbon needs an unambiguous fixed root and dynamic tip')
    positions = native['sim_positions']
    centers = [tuple((positions[a][j]+positions[b][j])*.5 for j in range(3))
               for a,b in zip(indices[::2],indices[1::2])]
    lengths = [0.]
    for a,b in zip(centers,centers[1:]):
        length = math.dist(a,b)
        if not math.isfinite(length) or length <= 1e-7:
            raise ValueError('Authored paint ribbon has a degenerate center span')
        lengths.append(lengths[-1]+length)
    return indices, [value/lengths[-1] for value in lengths], centers, lengths[-1]


def _ribbon_paint_correspondence(old, new, previous, indices, old_ids, new_ids,
                                 preserve_source_rails=False):
    """Explicit same-parent resampling; each rail retains its own painted values."""
    previous, old_t, old_center, old_length = _rooted_ribbon_parameters(old, previous, old_ids)
    indices, new_t, new_center, new_length = _rooted_ribbon_parameters(new, indices, new_ids)
    # A source key alone must not authorize a replacement or reversed curve.
    # Root is the shared attachment; require the same ribbon rail orientation.
    if math.dist(old_center[0],new_center[0]) > .0001:
        raise ValueError('Authored paint ribbon attachment changed')
    if preserve_source_rails and math.dist(old_center[-1],new_center[-1]) > .0001:
        raise ValueError('Authored paint source-rail endpoint changed')
    for native, group in ((old,previous),(new,indices)):
        if any(math.dist(native['sim_positions'][a],native['sim_positions'][b]) <= 1e-7
               for a,b in zip(group[::2],group[1::2])):
            raise ValueError('Authored paint ribbon has a collapsed cross section')
    old_rail = [old['sim_positions'][previous[1]][j]-old['sim_positions'][previous[0]][j] for j in range(3)]
    new_rail = [new['sim_positions'][indices[1]][j]-new['sim_positions'][indices[0]][j] for j in range(3)]
    if not preserve_source_rails and sum(a*b for a,b in zip(old_rail,new_rail)) <= 0:
        raise ValueError('Authored paint ribbon rail orientation changed')
    # A separately authorized source-spline key can retain the generator's
    # ordered rail coordinates when resampling rotates its ribbon frame.
    # Root and tip must still match; never average or guess the authored paint
    # from spatial proximity to another rail/guide.
    # Sampling occurs in source centerline coordinates, with no mesh-space
    # nearest-point or cross-guide search. Endpoints remain authored exactly.
    correspondence = {}
    span = 0
    for sample,t in enumerate(new_t):
        while span < len(old_t)-2 and old_t[span+1] < t:
            span += 1
        alpha = max(0.,min(1.,(t-old_t[span])/(old_t[span+1]-old_t[span])))
        for rail in (0,1):
            correspondence[indices[2*sample+rail]] = (previous[2*span+rail],previous[2*(span+1)+rail],alpha)
    return correspondence


def _weight_map_updates(full_graph, old, new, packet, ownership):
    """Rebuild known generated maps; reject unexplained authored paint changes."""
    updates = {}
    old_native, new_native = old['built_mapping'], new['built_mapping']
    old_count = len(old_native['sim_positions'])
    new_count = len(new_native['sim_positions'])
    identity_mapping = None
    paint_correspondence = None
    key_nodes = [n for n in full_graph['nodes'] if n['type'] == 'FChaosClothAssetWeightMapNode'
                 and n['props'].get('OutputName') == '(StringValue="CodexParentForceKey")']
    authored_nodes = [n for n in full_graph['nodes']
                      if n['type'] == 'FChaosClothAssetWeightMapNode'
                      and n['props'].get('OutputName') != '(StringValue="CodexParentForceKey")']
    if packet and ownership and len(key_nodes) == 1 and authored_nodes:
        previous_keys = [float(v) for v in key_nodes[0]['props']['VertexWeights'].strip('()').split(',') if v]
        current_keys, rows = _guide_identity_rows(packet, ownership)
        # UE ExportText serializes float properties with six decimal places.
        # Recover a hash only when exactly one current parent key falls in that
        # rounding interval; never merge near hashes or infer nearest guides.
        key_matches = {value: [key for key in set(current_keys) if abs(value-key) <= .500001e-6]
                       for value in set(previous_keys)}
        if (all(len(matches) == 1 for matches in key_matches.values())
                and len({matches[0] for matches in key_matches.values()}) == len(key_matches)):
            previous_keys = [key_matches[value][0] for value in previous_keys]
        old_ids = old_native.get('sim_import_vertex_ids_2d', [])
        new_ids = ownership['sim_source_indices']
        if (len(old_ids) == old_count and len(set(old_ids)) == old_count
                and len(previous_keys) == old_count and len(new_ids) == new_count
                and all(ownership['sim_source_identity_unique'])):
            lookup = dict(zip(old_ids, range(old_count)))
            mapping = [lookup.get(index) for index in new_ids]
            previous_counts, current_counts = Counter(previous_keys), Counter(current_keys)
            if not (all(index is not None for index in mapping)
                    and all(previous_keys[index] == current_keys[i] for i, index in enumerate(mapping))
                    and all(previous_counts[parent] == count for parent,count in current_counts.items())):
                # Exporting fewer units changes global source offsets/order.
                # Within each stable parent-spline identity, FBX source IDs retain
                # native local vertex order. Require the complete local topology.
                previous_groups, current_groups = defaultdict(list), defaultdict(list)
                for index, parent in enumerate(previous_keys):
                    previous_groups[parent].append(index)
                for index, parent in enumerate(current_keys):
                    current_groups[parent].append(index)
                mapping = [None] * new_count
                transfer = packet.get('weight_map_transfer', {})
                allowed = set()
                source_rails = set()
                if transfer:
                    if transfer.get('version') != 1 or transfer.get('policy') != 'rooted_ribbon_arclength':
                        raise ValueError('Unsupported explicit authored paint transfer')
                    allowed = {json.dumps(key,ensure_ascii=False,separators=(',',':'))
                               for key in transfer.get('guide_keys',[])}
                    source_rails = {json.dumps(key,ensure_ascii=False,separators=(',',':'))
                                    for key in transfer.get('rotated_source_rail_guide_keys',[])}
                    if not source_rails <= allowed:
                        raise ValueError('Source rail transfer needs an explicitly allowed guide key')
                correspondences = {}
                row_by_weight = {row['weight']: row for row in rows.values()}
                for parent, indices in current_groups.items():
                    previous = previous_groups.get(parent, [])
                    if len(previous) != len(indices):
                        row = row_by_weight.get(parent, {})
                        part = next((p for p in packet.get('parts',[]) if
                                     p.get('source_guide') == row.get('source') and
                                     p['simulation_mesh'].get('effective_mode') == 'RIBBON'), None)
                        if not previous or row.get('key') not in allowed or part is None:
                            break
                        correspondences.update(_ribbon_paint_correspondence(
                            old_native,new_native,previous,indices,old_ids,new_ids,
                            preserve_source_rails=row.get('key') in source_rails))
                        continue
                    previous.sort(key=lambda index: old_ids[index])
                    indices.sort(key=lambda index: new_ids[index])
                    for index, old_index in zip(indices, previous):
                        mapping[index] = old_index
                exact = {i:index for i,index in enumerate(mapping) if index is not None}
                retained = set(exact.values())
                if (correspondences and len(exact)+len(correspondences)==new_count and
                        all(previous_keys[index]==current_keys[i] for i,index in exact.items())):
                    old_faces = Counter(tuple(sorted(tri)) for tri in old_native['sim_triangles']
                                        if all(index in retained for index in tri))
                    new_faces = Counter(tuple(sorted(exact[i] for i in tri)) for tri in new_native['sim_triangles']
                                        if all(i in exact for i in tri))
                    if _same_painted_vertex_support(old_faces,new_faces):
                        paint_correspondence = {i:(index,index,0.) for i,index in exact.items()}
                        paint_correspondence.update(correspondences)
            if (all(index is not None for index in mapping)
                    and all(previous_keys[index] == current_keys[i] for i, index in enumerate(mapping))):
                retained = set(mapping)
                old_faces = Counter(tuple(sorted(tri)) for tri in old_native['sim_triangles']
                                    if all(index in retained for index in tri))
                new_faces = Counter(tuple(sorted(mapping[i] for i in tri)) for tri in new_native['sim_triangles'])
                if _same_painted_vertex_support(old_faces,new_faces):
                    identity_mapping = mapping
    for node in full_graph['nodes']:
        if node['type'] != 'FChaosClothAssetWeightMapNode':
            continue
        props = node['props']
        if (props.get('MeshTarget') != 'Simulation' or props.get('MapOverrideType') != 'ReplaceAll'
                or props.get('Snapshots') != '(ActiveSnapshot=-1)' or props.get('bIsFrozen') != 'False'):
            raise ValueError('Unsupported authored WeightMap state: ' + node['name'])
        values = [float(v) for v in props['VertexWeights'].strip('()').split(',') if v]
        if len(values) != old_count or any(not math.isfinite(v) for v in values):
            raise ValueError('Incomplete authored WeightMap: ' + node['name'])
        output = re.fullmatch(r'\(StringValue="([^"\n]+)"\)', props['OutputName'])
        if not output:
            raise ValueError('WeightMap output name is not explicit')
        name = output.group(1)
        if name == 'CodexParentForceKey':
            if not packet or not ownership:
                raise ValueError('Parent force keys require generator provenance')
            if any(not 0 < value < 1 for value in values):
                raise ValueError('Parent force key contract is invalid')
            for tri in old_native['sim_triangles']:
                if len({values[i] for i in tri}) != 1:
                    raise ValueError('Parent force keys vary within a guide')
            weights, rows = _guide_identity_rows(packet, ownership)
            if len({row['weight'] for row in rows.values()}) != len(rows):
                raise ValueError('Parent force key hash collision')
        elif (old_native['sim_positions'] == new_native['sim_positions'] and
              old_native['sim_triangles'] == new_native['sim_triangles']):
            weights = values
        else:
            old_kin = set(old_native['kinematic_sim_indices'])
            if old_kin and len(old_kin) != old_count and all(
                    value == (0.0 if i in old_kin else 1.0) for i, value in enumerate(values)):
                new_kin = set(new_native['kinematic_sim_indices'])
                weights = [0.0 if i in new_kin else 1.0 for i in range(new_count)]
            else:
                if identity_mapping is not None:
                    # Parent-spline identity + imported source vertex identity +
                    # unchanged triangle support survive positional edits.
                    weights = [values[index] for index in identity_mapping]
                elif paint_correspondence is not None:
                    weights = [values[a]*(1.-t)+values[b]*t
                               for a,b,t in (paint_correspondence[i] for i in range(new_count))]
                else:
                    weights = _transfer_unchanged_simulation_map(old_native, new_native, values)
        if len(weights) != new_count:
            raise ValueError('WeightMap vertex count differs from the rebuilt simulation')
        updates[node['name']] = (name, values, weights)
    return updates


def _publish(unreal, source_asset, binding_path, target, owner, content, packet=None, ownership=None):
    # Verify a new two-node graph before touching a previously published asset.
    stage_path = source_asset.get_path_name().split(".", 1)[0] + "_FinalStage"
    stage = unreal.EditorAssetLibrary.duplicate_asset(source_asset.get_path_name(), stage_path)
    graph, call, _, nodes, infos = _graph(unreal, stage)
    terminal = next(n for n, i in infos.items() if "TerminalNode" in i["type"])
    for name, node in nodes.items():
        if name != terminal:
            call("RemoveNode", graph, node)
    imported = call("AddNode", graph, "FChaosClothAssetImportNode", "GeneratorBindings",
                    json.dumps({"ClothAsset": binding_path, "ImportLod": 0}), -300, 0)
    for pin in infos[terminal]["inputPins"]:
        if pin["name"].startswith("CollectionLods["):
            if not call("ConnectNodePins", imported, "Collection", nodes[terminal], pin["name"]):
                raise RuntimeError("Failed to connect the validated cloth import")
    unreal.DataflowBlueprintLibrary.evaluate_terminal_node_by_name(graph, terminal, stage)
    before = _native(unreal.CodexClothToolsLibrary.dump_cloth_collection_colors(binding_path))
    after = _native(unreal.CodexClothToolsLibrary.dump_cloth_collection_colors(stage_path))
    if before["collections"][0] != after["collections"][0]:
        raise RuntimeError("Final import changed the validated cloth collection")
    binding_key = "Send2UE.HairGuideCloth.BindingData"
    for key, value in ((OWNER, owner), (CONTENT, content), (binding_key, binding_path)):
        unreal.EditorAssetLibrary.set_metadata_tag(stage, key, value)
    if not unreal.EditorAssetLibrary.save_loaded_asset(stage):
        raise RuntimeError("Validated cloth staging asset could not be saved")
    if not unreal.EditorAssetLibrary.does_asset_exist(target):
        final = unreal.EditorAssetLibrary.duplicate_asset(stage_path, target)
        if not final:
            raise RuntimeError("Validated cloth could not be published")
        # Package UMetaData is not guaranteed to follow UObject duplication.
        for key, value in ((OWNER, owner), (CONTENT, content), (binding_key, binding_path)):
            unreal.EditorAssetLibrary.set_metadata_tag(final, key, value)
        if not unreal.EditorAssetLibrary.save_loaded_asset(final):
            raise RuntimeError("Validated cloth could not be published")
    else:
        final = unreal.load_asset(target)
        if unreal.EditorAssetLibrary.get_metadata_tag(final, OWNER) != owner:
            raise ValueError("Cloth output belongs to another asset: " + target)
        old_content = unreal.EditorAssetLibrary.get_metadata_tag(final, CONTENT)
        old_binding = unreal.EditorAssetLibrary.get_metadata_tag(final, binding_key)
        fg, fc, fs, fn, fi = _graph(unreal, final)
        imports = [n for n, info in fi.items() if info["type"] == "FChaosClothAssetImportNode"]
        terminals = [n for n, info in fi.items() if "TerminalNode" in info["type"]]
        if imports != ["GeneratorBindings"] or len(terminals) != 1 or not old_binding:
            raise ValueError("Existing cloth graph has user changes; it remains unchanged")
        # Solver/material-response configuration and the body collider downstream
        # of our owned import belong to the artist. Preserve them when replacing
        # only binding data, including a cheaper collider chosen for this hair.
        # Geometry/weight-map/proxy nodes are not configuration overrides.
        configs = set(fn) - set(imports) - set(terminals)
        if any(fi[n]["type"] not in ("FChaosClothAssetSetPhysicsAssetNode", "FChaosClothAssetWeightMapNode") and
               not re.fullmatch(r"FChaosClothAssetSimulation\w*ConfigNode(?:_v\d+)?", fi[n]["type"])
               for n in configs):
            raise ValueError("Existing cloth graph has user changes; it remains unchanged")
        incoming = {(e["toNode"], e["toPin"]): e["fromNode"] for e in fs["connections"]
                    if e["fromPin"] == "Collection"}
        visited = set()
        for pin in fi[terminals[0]]["inputPins"]:
            if not pin["name"].startswith("CollectionLods["):
                continue
            current = incoming.get((terminals[0], pin["name"]))
            chain = set()
            while current in configs and current not in chain:
                chain.add(current)
                current = incoming.get((current, "Collection"))
            if current != imports[0]:
                raise ValueError("Existing cloth graph has user changes; it remains unchanged")
            visited.update(chain)
        if visited != configs:
            raise ValueError("Existing cloth graph has user changes; it remains unchanged")
        # GetNodeInfo exports actual property values as Unreal text, e.g.
        # /Script/ChaosClothAsset.ChaosClothAsset'/Game/Hair/Data.Data'. Metadata
        # alone cannot prove that the artist left the owned import unchanged.
        previous_import = fi[imports[0]].get("properties", {})
        reference = previous_import.get("ClothAsset")
        if isinstance(reference, str):
            reference = reference.strip()
            typed_reference = re.fullmatch(r"[A-Za-z0-9_./]+'([^']+)'", reference)
            if typed_reference:
                reference = typed_reference.group(1)
        expected_references = (old_binding, old_binding + "." + old_binding.rsplit("/", 1)[-1])
        if (reference not in expected_references or
                str(previous_import.get("ImportLod", "")).strip() != "0"):
            raise ValueError("Existing cloth Import values have user changes; it remains unchanged")
        maps = {}
        if any(fi[n]['type'] == 'FChaosClothAssetWeightMapNode' for n in configs):
            full_graph = _native(unreal.CodexClothToolsLibrary.dump_dataflow_nodes(fg.get_path_name()))
            old_collection = _native(unreal.CodexClothToolsLibrary.dump_cloth_collection_colors(target))
            maps = _weight_map_updates(full_graph, old_collection['collections'][0],
                                       before['collections'][0], packet, ownership)
        try:
            for name, (_, _, weights) in maps.items():
                fc('UpdateNode', fn[name], json.dumps({'VertexWeights': weights}))
            fc("UpdateNode", fn[imports[0]], json.dumps({"ClothAsset": binding_path, "ImportLod": 0}))
            unreal.DataflowBlueprintLibrary.evaluate_terminal_node_by_name(fg, terminals[0], final)
            actual = _native(unreal.CodexClothToolsLibrary.dump_cloth_collection_colors(target))
            # This native audit contains geometry, normals, colours, weight maps
            # and deformer bindings, not solver/config property values. Keeping
            # its full comparison permits physics overrides without accepting
            # changes to the validated simulation/render correspondence.
            compared = json.loads(json.dumps(actual['collections'][0]))
            added_names = {row[0] for row in maps.values()}
            for key in ('weight_maps', 'sim_float_attributes'):
                if key in compared:
                    compared[key] = [item for item in compared[key] if item['name'] not in added_names]
            expected_collection = json.loads(json.dumps(before["collections"][0]))
            # This whole-collection fingerprint also includes artist-owned solver,
            # collider and transferred paint attributes. The explicit audit below
            # still checks geometry, UV/color payloads and every proxy embedding;
            # a bare binding cannot have the same full fingerprint as that graph.
            compared.pop('non_uv_collection_md5', None)
            expected_collection.pop('non_uv_collection_md5', None)
            if compared != expected_collection:
                changed_fields = sorted(key for key in set(compared) | set(expected_collection)
                                        if compared.get(key) != expected_collection.get(key))
                raise RuntimeError("Published cloth differs from its verified collection: " +
                                   ", ".join(changed_fields))
            unreal.EditorAssetLibrary.set_metadata_tag(final, CONTENT, content)
            unreal.EditorAssetLibrary.set_metadata_tag(final, binding_key, binding_path)
            if not unreal.EditorAssetLibrary.save_loaded_asset(final):
                raise RuntimeError("Updated cloth could not be saved")
        except Exception:
            for name, (_, values, _) in maps.items():
                fc('UpdateNode', fn[name], json.dumps({'VertexWeights': values}))
            fc("UpdateNode", fn[imports[0]], json.dumps({"ClothAsset": old_binding, "ImportLod": 0}))
            unreal.DataflowBlueprintLibrary.evaluate_terminal_node_by_name(fg, terminals[0], final)
            unreal.EditorAssetLibrary.set_metadata_tag(final, CONTENT, old_content)
            unreal.EditorAssetLibrary.set_metadata_tag(final, binding_key, old_binding)
            unreal.EditorAssetLibrary.save_loaded_asset(final)
            raise
    return after


def build_in_editor(record, output_directory):
    """Run in a cold native worker, or an editor with the current helper loaded."""
    import unreal
    record, packet = validate_record(record)
    output = Path(output_directory); output.mkdir(parents=True, exist_ok=True)
    if not record["simulation_enabled"]:
        receipt = _render_only_receipt(record, packet)
        (output / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf8")
        return receipt
    if not callable(getattr(unreal.CodexClothToolsLibrary, "repair_cloth_proxy_bindings", None)):
        raise RuntimeError("Current CodexClothTools native binding helper is not loaded")
    unreal.SystemLibrary.execute_console_command(None, "Editor.AsyncSkinnedAssetCompilation 0")
    sim_mesh = unreal.load_asset(record["sim_asset_path"])
    render_mesh = unreal.load_asset(record["render_asset_path"])
    body_mesh = unreal.load_asset(record["body_mesh_asset_path"])
    if (sim_mesh.get_editor_property("skeleton") != render_mesh.get_editor_property("skeleton") or
            sim_mesh.get_editor_property("skeleton") != body_mesh.get_editor_property("skeleton")):
        raise ValueError("Guide and render imports do not share the export skeleton")
    folder, render_name = record["render_asset_path"].rsplit("/", 1)
    stem = re.sub(r"^(?:SK|SM)_", "", render_name)
    target = _target(record)
    owner = VERSION + ":" + record["render_asset_path"]
    if unreal.EditorAssetLibrary.does_asset_exist(target):
        existing = unreal.load_asset(target)
        if unreal.EditorAssetLibrary.get_metadata_tag(existing, OWNER) != owner:
            raise ValueError("Refusing to replace an unrelated cloth asset: " + target)
    content = record["manifest_sha256"]
    revision = content[:12] + "_" + uuid.uuid4().hex[:8]
    build_folder = folder + "/HairGuideCloth/" + stem + "/" + revision
    source_path, binding_path = build_folder + "/CA_BuildSource", build_folder + "/CA_BindingData"
    source, initial = _build_source(unreal, record, source_path)
    ownership = normalize_ownership(initial["collections"][0]["built_mapping"], packet)
    # Sharing a USkeleton does not guarantee identical imported reference-pose
    # scales. The native helper uses the actual leader's reference skeleton,
    # after checking bone identity, without transferring or repainting weights.
    ownership["reference_skeletal_mesh_asset_path"] = record["body_mesh_asset_path"]
    ownership_path = output / "ownership.json"
    ownership_path.write_text(json.dumps(ownership), encoding="utf8")
    preview = _native(unreal.CodexClothToolsLibrary.repair_cloth_proxy_bindings(
        source_path, binding_path, str(ownership_path), False), output / "binding_preview.json")
    render_only_count = len(ownership["render_only_vertex_indices"])
    _verify_render_only_binding(preview, render_only_count)
    repaired = _native(unreal.CodexClothToolsLibrary.repair_cloth_proxy_bindings(
        source_path, binding_path, str(ownership_path), True))
    _verify_render_only_binding(repaired, render_only_count)
    binding = unreal.load_asset(binding_path)
    if not binding or not unreal.EditorAssetLibrary.save_loaded_asset(binding):
        raise RuntimeError("Validated binding collection could not be saved")
    final = _publish(unreal, source, binding_path, target, owner, content, packet, ownership)
    native = final["collections"][0]["built_mapping"]
    receipt = {"verified": True, "version": record["version"], "cloth_asset_path": target,
               "simulation_enabled": True, "no_guide_policy": "render_only_skinning",
               "sim_asset_path": record["sim_asset_path"], "render_asset_path": record["render_asset_path"],
               "binding_asset_path": binding_path, "build_source_asset_path": source_path,
               "manifest_sha256": content, "source_matching": ownership["provenance"]["source_matching"],
               "sim_vertices": len(native["sim_positions"]), "render_vertices": len(native["render_positions"]),
               "guide_count": len(set(ownership["sim_guide_ids"])), "remesh": False,
               "render_only_vertices": render_only_count,
               "weight_source": "simulation_source_own_g",
               "reference_skeletal_mesh_asset_path": record["body_mesh_asset_path"],
               "authored_weight_map_transfer": packet.get('weight_map_transfer'),
               "native_binding": repaired}
    (output / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf8")
    return receipt


_LIVE_STATE_TAG = "Send2UE.HairGuideCloth.LiveState:"
_LIVE_HELPER_TAG = "Send2UE.HairGuideCloth.RenderHelper:"


def _refresh_live_components(record, final=None):
    """Propagate verified output to exact existing owners, retaining scene setup.

    Asset package reload does not rebuild an already-instantiated cloth proxy.
    State tags make render-only transitions reversible across Python reloads.
    No scene actor, unrelated cloth, or imported source asset is replaced.
    """
    import unreal
    expected = VERSION + ":" + record["render_asset_path"]
    target = _target(record)
    enabled = record["simulation_enabled"]
    sub = unreal.get_editor_subsystem(unreal.EditorActorSubsystem)
    render = (final or unreal.load_asset(record["render_asset_path"])) if not enabled else None
    rows = []

    def tags(component):
        return [str(t) for t in component.get_editor_property("component_tags")]

    def visibility(component):
        return {"visible": bool(component.get_editor_property("visible")),
                "hidden_in_game": bool(component.get_editor_property("hidden_in_game"))}

    def set_visibility(component, state):
        component.set_visibility(state["visible"], False)
        component.set_hidden_in_game(state["hidden_in_game"], False)

    def set_flags(component, state):
        component.set_enable_simulation(state["enabled"])
        # bSuspendSimulation shadows the same-named Python method in UE 5.8.
        component.call_method("SuspendSimulation" if state["suspended"] else "ResumeSimulation", args=())

    def flags(component):
        # IsSimulationEnabled also tests proxy validity and p.ClothPhysics;
        # IsSimulationSuspended folds disabled into suspended. Those effective
        # queries cannot preserve the user's actual two independent flags.
        return {"enabled": bool(component.get_editor_property("enable_simulation")),
                "suspended": bool(component.get_editor_property("suspend_simulation"))}

    for actor in sub.get_all_level_actors():
        cloth_names = [c.get_name() for c in actor.get_components_by_class(unreal.ChaosClothComponent)]
        for cloth_name in cloth_names:
            # Native instance authoring may rebuild SCS components. Never keep
            # the original component iterator alive across another helper add.
            cloth = next((c for c in actor.get_components_by_class(unreal.ChaosClothComponent)
                          if c.get_name() == cloth_name), None)
            if cloth is None:
                continue
            asset = cloth.get_asset()
            if not asset:
                continue
            if (_path(asset.get_path_name()) != target and
                    unreal.EditorAssetLibrary.get_metadata_tag(asset, OWNER) != expected):
                continue
            original_tags = tags(cloth)
            helper_tag = _LIVE_HELPER_TAG + expected + ":" + cloth.get_name()
            states = [json.loads(t[len(_LIVE_STATE_TAG):]) for t in original_tags if t.startswith(_LIVE_STATE_TAG)]
            state = next((s for s in states if s.get("owner") == expected), None)
            if enabled:
                current = flags(cloth)
                # SetAsset may be a no-op for an identical UObject. Explicitly
                # rebuild the proxy/config even on the second import of that ID.
                cloth.set_asset(final)
                cloth.reset_config_properties()
                cloth.recreate_cloth_simulation_proxy()
                set_flags(cloth, state or current)
                if state:
                    for component in actor.get_components_by_class(unreal.SkeletalMeshComponent):
                        if component == cloth:
                            continue
                        if helper_tag in tags(component):
                            component.call_method("K2_DestroyComponent", args=(actor,))
                        elif component.get_name() == state.get("renderer"):
                            set_visibility(component, state["renderer_visibility"])
                    cloth.set_editor_property("component_tags", [t for t in original_tags
                        if not (t.startswith(_LIVE_STATE_TAG) and json.loads(t[len(_LIVE_STATE_TAG):]).get("owner") == expected)])
                    # Tag notifications and helper teardown may reregister cloth
                    # and restore its default visibility; authored flags win last.
                    set_flags(cloth, state)
                    set_visibility(cloth, state)
                rows.append({"component": cloth.get_path_name(), "status": "simulation_refreshed",
                             "asset": final.get_path_name(), "enabled": cloth.is_simulation_enabled()})
                continue

            if state is None:
                state = dict(visibility(cloth), owner=expected, **flags(cloth))
            leader = cloth.get_editor_property("leader_pose_component")
            siblings = [c for c in actor.get_components_by_class(unreal.SkeletalMeshComponent) if c != cloth]
            helper = next((c for c in siblings if helper_tag in tags(c)), None)
            renderer = next((c for c in siblings if c.get_name() == state.get("renderer")), None)
            if not renderer:
                matches = [c for c in siblings if c.get_skinned_asset() == render
                           and c.get_editor_property("leader_pose_component") == leader
                           and c.get_attach_parent() == cloth.get_attach_parent()
                           and c.get_relative_transform() == cloth.get_relative_transform()]
                renderer = helper or (matches[0] if len(matches) == 1 else None)
            if renderer is None:
                # The editor subsystem calls native AddInstanceComponent. Its
                # CreationMethod/InstanceComponents fields are protected and
                # cannot be authored through Python property setters in UE 5.8.
                data_sub = unreal.get_engine_subsystem(unreal.SubobjectDataSubsystem)
                data_lib = unreal.SubobjectDataBlueprintFunctionLibrary
                handles = data_sub.k2_gather_subobject_data_for_instance(actor)
                actor_handle = next(h for h in handles
                    if data_lib.get_associated_object(data_lib.get_data(h)) == actor)
                cloth_name = cloth.get_name()
                relative = cloth.get_relative_transform()
                socket = cloth.get_attach_socket_name()
                parent = cloth.get_attach_parent()
                parent_name = parent.get_name() if parent and parent.get_owner() == actor else None
                leader_name = leader.get_name() if leader and leader.get_owner() == actor else None
                tick_option = cloth.get_editor_property("visibility_based_anim_tick_option")
                forced_lod = cloth.get_editor_property("forced_lod_model")
                overrides = list(cloth.get_editor_property("override_materials"))
                handle, failure = data_sub.add_new_subobject(unreal.AddNewSubobjectParams(
                    parent_handle=actor_handle, new_class=unreal.SkeletalMeshComponent,
                    blueprint_context=None, skip_mark_blueprint_modified=True,
                    conform_transform_to_parent=False))
                if not data_lib.is_handle_valid(handle):
                    raise RuntimeError("Could not create render-only hair instance: " + str(failure))
                renderer = data_lib.get_associated_object(data_lib.get_data(handle))
                # Instance authoring can rerun construction. Resolve the current
                # components again before restoring the captured scene setup.
                current_components = {c.get_name(): c for c in actor.get_components_by_class(unreal.ActorComponent)}
                cloth = current_components[cloth_name]
                parent = current_components[parent_name] if parent_name else parent
                leader = current_components[leader_name] if leader_name else leader
                renderer.set_editor_property("component_tags", [helper_tag])
                if parent:
                    renderer.attach_to_component(parent, socket, unreal.AttachmentRule.KEEP_RELATIVE,
                        unreal.AttachmentRule.KEEP_RELATIVE, unreal.AttachmentRule.KEEP_RELATIVE, False)
                renderer.set_relative_transform(relative, False, True)
                renderer.set_leader_pose_component(leader)
                renderer.set_editor_property("visibility_based_anim_tick_option", tick_option)
                renderer.set_editor_property("forced_lod_model", forced_lod)
                renderer.set_skeletal_mesh_asset(render)
                # Assigning a different mesh clears material overrides.
                renderer.set_editor_property("override_materials", overrides)
            if helper_tag in tags(renderer):
                renderer.set_skeletal_mesh_asset(render)
            if "renderer" not in state:
                state.update(renderer=renderer.get_name(), renderer_visibility=visibility(renderer))
            # Write restoration data before modifying the user's component.
            cloth.set_editor_property("component_tags", [t for t in original_tags
                if not (t.startswith(_LIVE_STATE_TAG) and json.loads(t[len(_LIVE_STATE_TAG):]).get("owner") == expected)]
                + [_LIVE_STATE_TAG + json.dumps(state, separators=(",", ":"))])
            set_visibility(renderer, state)
            cloth.set_enable_simulation(False)
            cloth.call_method("SuspendSimulation", args=())
            cloth.set_visibility(False, False)
            cloth.set_hidden_in_game(True, False)
            rows.append({"component": cloth.get_path_name(), "status": "render_only",
                         "renderer": renderer.get_path_name(), "enabled": False})
    return rows


def _apply_hair_guide_cloth(record):
    """Ordinary Send2UE post-import entry. Require a completed verified receipt."""
    import unreal
    record, packet = validate_record(record)
    if not record["simulation_enabled"]:
        render = unreal.load_asset(record["render_asset_path"])
        if not render:
            raise ValueError("Imported render mesh is missing: " + record["render_asset_path"])
        if not unreal.EditorAssetLibrary.save_loaded_asset(render):
            raise RuntimeError("Imported render-only hair mesh could not be saved")
        receipt = _render_only_receipt(record, packet)
        receipt["live_components"] = _refresh_live_components(record, render)
        return receipt
    assets = []
    for key in ("sim_asset_path", "render_asset_path", "cloth_template_asset_path",
                "body_mesh_asset_path", "physics_asset_path"):
        asset = unreal.load_asset(record[key])
        if not asset:
            raise ValueError("Required imported asset is missing: " + record[key])
        assets.append(asset)
    dirty = {p.get_path_name() for p in unreal.EditorLoadingAndSavingUtils.get_dirty_content_packages()}
    target = _target(record)
    if unreal.EditorAssetLibrary.does_asset_exist(target):
        current = unreal.load_asset(target)
        expected_owner = VERSION + ":" + record["render_asset_path"]
        if unreal.EditorAssetLibrary.get_metadata_tag(current, OWNER) != expected_owner:
            raise ValueError("Existing cloth output belongs to another asset and remains unchanged")
        if current.get_outer().get_path_name() in dirty:
            raise ValueError("Existing cloth has unsaved edits and remains unchanged")
        assets.append(current)
    for asset in assets[2:]:
        if asset.get_outer().get_path_name() in dirty:
            raise ValueError("Cloth template/body has unsaved edits; save it before Send to Unreal: " + asset.get_path_name())
    for asset in assets[:2]:
        if not unreal.EditorAssetLibrary.save_loaded_asset(asset):
            raise RuntimeError("Imported hair mesh could not be saved")
    project = Path(unreal.Paths.convert_relative_path_to_full(unreal.Paths.get_project_file_path())).resolve()
    run = project.parent / "Saved" / "HairGuideCloth" / uuid.uuid4().hex
    run.mkdir(parents=True)
    # Build in the admitted visible editor. Never launch a second/headless
    # editor against production packages, and never reload live package state.
    receipt = build_in_editor(record, str(run))
    if not receipt or not receipt.get("verified"):
        raise RuntimeError("Hair guide cloth build did not produce a verified receipt")
    final = unreal.load_asset(receipt["cloth_asset_path"])
    if not final:
        raise RuntimeError("New cloth asset was saved but could not be loaded")
    if (unreal.EditorAssetLibrary.get_metadata_tag(final, CONTENT) != receipt["manifest_sha256"] or
            unreal.EditorAssetLibrary.get_metadata_tag(final, "Send2UE.HairGuideCloth.BindingData") != receipt["binding_asset_path"]):
        raise RuntimeError("The editor still has an older cloth result loaded")
    receipt["live_components"] = _refresh_live_components(record, final)
    return receipt


def apply_hair_guide_cloth(record):
    """Keep the ordinary export successful when optional cloth cannot be built.

    No extra fallback or paint rule is inferred from a failed validation. The
    already imported render mesh and the previous cloth remain available.
    """
    try:
        return _apply_hair_guide_cloth(record)
    except Exception as error:
        receipt = {"version": 1, "verified": False, "status": "cloth_not_updated",
                   "render_asset_path": record.get("render_asset_path") if isinstance(record, dict) else None,
                   "detail": str(error)}
        print("SEND2UE_HAIR_GUIDE_CLOTH_INFO:" + json.dumps(receipt, ensure_ascii=False))
        return receipt
